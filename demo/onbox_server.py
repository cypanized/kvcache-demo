#!/usr/bin/env python3
"""KV-cache demo control server — runs ON the GPU instance.

Everything is local: vLLM at 127.0.0.1:8000, mode switches via
/root/switch.sh, probes via /root/ask.py, spill via /root/spill_test.py.
Your browser reaches it through an SSH tunnel to port 7811.
"""
import glob
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VLLM = "http://127.0.0.1:8000"
LOG_PATH = "/root/demo_log.json"
SPILL_OUT = "/root/spill_out.jsonl"

try:
    _saved = json.load(open(LOG_PATH))
except (FileNotFoundError, json.JSONDecodeError):
    _saved = []

state = {"mode": "baseline", "switching": False, "switch_started": 0, "log": _saved,
         "spill": {"running": False, "started": 0, "summary": None},
         "cpu_evict": {"running": False, "started": 0, "done": 0, "total": 0},
         "workset": {"running": False, "started": 0, "done": 0, "total": 0, "summary": None}}
lock = threading.Lock()

WORKSET_DOCS = 30  # ~60 GB of KV vs the ~44 GB GPU budget — natural overflow

# ---- tier residency model ----
# every request flows through this server, so we can track which docs' KV
# currently lives in each tier (LRU, evicted at the tier's real budget)
from collections import OrderedDict

TOK_PER_DOC = 10400
# tier caps in tokens (0.192 MB per token)
CPU_TIER_TOKENS = {"cpu": 421_000, "disk": 105_000, "full": 312_500}       # 80/20/60 GB
STORAGE_TIER_TOKENS = {"disk": 625_000, "nixl": 375_000, "full": 435_200}  # 120/72/85.6 GB (1700 slots x 256 tok)
STORAGE_LOCATION = {"disk": "LocalDiskBackend", "nixl": "NixlStorageBackend",
                    "full": "NixlStorageBackend"}
RES_PATH = "/root/residency.json"
try:
    _r = json.load(open(RES_PATH))
    residency = {k: OrderedDict(_r.get(k, [])) for k in ("gpu", "cpu", "storage")}
except (FileNotFoundError, json.JSONDecodeError):
    residency = {"gpu": OrderedDict(), "cpu": OrderedDict(), "storage": OrderedDict()}


def save_residency():
    try:
        json.dump({k: list(v.items()) for k, v in residency.items()}, open(RES_PATH, "w"))
    except OSError:
        pass


def gpu_budget_tokens():
    d = _stats_cache.get("data") or {}
    return d.get("gpu_kv_tokens") or 239_872


def _lru_put(od, key, tokens, cap):
    od.pop(key, None)
    od[key] = tokens
    while sum(od.values()) > cap and len(od) > 1:
        od.popitem(last=False)


def track_request(key, tokens=TOK_PER_DOC):
    with lock:
        _lru_put(residency["gpu"], key, tokens, gpu_budget_tokens())
        ccap = CPU_TIER_TOKENS.get(state["mode"])
        if ccap:
            _lru_put(residency["cpu"], key, tokens, ccap)
        scap = STORAGE_TIER_TOKENS.get(state["mode"])
        if scap:
            _lru_put(residency["storage"], key, tokens, scap)
        save_residency()


def residency_snapshot():
    with lock:
        return {"gpu_tok": sum(residency["gpu"].values()), "gpu_docs": len(residency["gpu"]),
                "cpu_tok": sum(residency["cpu"].values()), "cpu_docs": len(residency["cpu"]),
                "storage_tok": sum(residency["storage"].values()), "storage_docs": len(residency["storage"]),
                "gpu_cap_tok": gpu_budget_tokens(),
                "cpu_cap_tok": CPU_TIER_TOKENS.get(state["mode"], 0),
                "storage_cap_tok": STORAGE_TIER_TOKENS.get(state["mode"], 0)}


def start_sweep(set_idx):
    """One manual pass over a 10-doc set (set 0 = docs 1-10, ... set 3 = docs 31-40)."""
    lo, hi = set_idx * 10, set_idx * 10 + 10
    with lock:
        if state["workset"]["running"] or state["spill"]["running"]:
            return False
        state["workset"] = {"running": True, "started": time.time(), "done": 0,
                            "total": 10, "set": set_idx + 1, "summary": None}

    def worker():
        ttfts = []
        try:
            for d in range(lo, hi):
                e = run_probe([str(d)])
                if "error" not in e:
                    track_request(f"doc:{d}")
                    ttfts.append(e["ttft"])
                    e["mode"] = state["mode"]
                    e["label"] = f"set {set_idx+1} · {e['label']}"
                    e["ts"] = time.strftime("%H:%M:%S")
                    append_log(e)
                with lock:
                    state["workset"]["done"] += 1
            mean = round(sum(ttfts) / len(ttfts), 3) if ttfts else 0
            summary = {"set": set_idx + 1, "mean": mean}
            append_log({"mode": state["mode"],
                        "label": f"SWEEP SET {set_idx+1} (docs {lo+1}-{hi}): mean TTFT {mean}s",
                        "ttft": mean, "total": mean, "tokens": 0, "answer": "",
                        "ts": time.strftime("%H:%M:%S")})
            with lock:
                state["workset"]["summary"] = summary
        finally:
            with lock:
                state["workset"]["running"] = False
    threading.Thread(target=worker, daemon=True).start()
    return True

# docs needed to flood the CPU tier past capacity (tier GB / 2GB-per-doc + margin)
CPU_EVICT_DOCS = {"cpu": 44, "disk": 12}


def start_cpu_evict():
    mode = state["mode"]
    if mode not in CPU_EVICT_DOCS:
        return None
    with lock:
        if state["cpu_evict"]["running"] or state["spill"]["running"]:
            return False
        total = CPU_EVICT_DOCS[mode]
        state["cpu_evict"] = {"running": True, "started": time.time(), "done": 0, "total": total}

    def worker():
        try:
            for i in range(total):
                run_probe(["cold"])
                with lock:
                    state["cpu_evict"]["done"] = i + 1
            append_log({"mode": state["mode"], "label": f"CPU tier evicted ({total} new docs, ~{total*2} GB)",
                        "ttft": 0.0, "total": 0.0, "tokens": 0, "answer": "",
                        "ts": time.strftime("%H:%M:%S")})
        finally:
            with lock:
                state["cpu_evict"]["running"] = False
    threading.Thread(target=worker, daemon=True).start()
    return True


def append_log(entry):
    with lock:
        state["log"].append(entry)
        try:
            json.dump(state["log"], open(LOG_PATH, "w"), indent=1)
        except OSError:
            pass


def vllm_ready():
    try:
        with urllib.request.urlopen(f"{VLLM}/v1/models", timeout=2) as r:
            return json.load(r)["data"][0]["id"]
    except Exception:
        return None


def detect_mode():
    logs = sorted(glob.glob("/root/server_*.log"), key=os.path.getmtime, reverse=True)
    if logs:
        name = os.path.basename(logs[0]).removeprefix("server_").removesuffix(".log")
        if name in ("baseline", "cpu", "disk", "nixl", "full"):
            state["mode"] = name


detect_mode()


def switch_mode(mode):
    with lock:
        state["mode"] = mode
        state["switching"] = True
        state["switch_started"] = time.time()
        residency["gpu"].clear(); save_residency()
        residency["cpu"].clear(); save_residency()
        residency["storage"].clear(); save_residency()
    subprocess.Popen(["nohup", "/root/switch.sh", mode],
                     stdout=open("/root/switch.log", "a"), stderr=subprocess.STDOUT)

    def waiter():
        for _ in range(20):
            if not vllm_ready():
                break
            time.sleep(2)
        for _ in range(150):
            if vllm_ready():
                break
            time.sleep(5)
        with lock:
            state["switching"] = False
    threading.Thread(target=waiter, daemon=True).start()


def run_probe(args, stdin_text=None):
    r = subprocess.run(["python3", "/root/ask.py"] + args, input=stdin_text,
                       capture_output=True, text=True, timeout=600)
    line = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"error": (r.stderr.strip() or "probe failed")[-300:]}


def spill_progress():
    prog = {"running": state["spill"]["running"], "cold": 0, "warm": 0,
            "elapsed": round(time.time() - state["spill"]["started"]) if state["spill"]["running"] else 0,
            "summary": state["spill"]["summary"]}
    try:
        for line in open(SPILL_OUT):
            if '"cold"' in line:
                prog["cold"] += 1
            elif '"warm"' in line:
                prog["warm"] += 1
    except OSError:
        pass
    return prog


def start_spill():
    with lock:
        if state["spill"]["running"]:
            return False
        state["spill"] = {"running": True, "started": time.time(), "summary": None}
    out = open(SPILL_OUT, "w")
    proc = subprocess.Popen(["python3", "/root/spill_test.py"], stdout=out,
                            stderr=subprocess.STDOUT, text=True)

    def waiter():
        proc.wait()
        summary = None
        try:
            for line in open(SPILL_OUT):
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("phase") == "warm":
                    append_log({"mode": state["mode"], "label": f"spill doc {ev['doc']} — {ev['gb_nvme']} GB off NVMe",
                                "ttft": ev["ttft"], "total": ev["ttft"], "tokens": 0, "answer": "",
                                "ts": time.strftime("%H:%M:%S")})
                elif ev.get("phase") == "summary":
                    summary = ev
            if summary:
                append_log({"mode": state["mode"],
                            "label": f"SPILL SUMMARY: {summary['docs']} docs, {summary['total_gb_nvme']} GB read from NVMe",
                            "ttft": summary["warm_ttft_mean"], "total": summary["warm_ttft_mean"],
                            "tokens": 0, "answer": "", "ts": time.strftime("%H:%M:%S")})
        finally:
            with lock:
                state["spill"]["running"] = False
                state["spill"]["summary"] = summary
    threading.Thread(target=waiter, daemon=True).start()
    return True


def read_cgroup_mem():
    base = "/sys/fs/cgroup"
    try:  # cgroup v2
        cur = int(open(f"{base}/memory.current").read())
        mx_raw = open(f"{base}/memory.max").read().strip()
        mx = 0 if mx_raw == "max" else int(mx_raw)
        anon = filec = 0
        for line in open(f"{base}/memory.stat"):
            k, v = line.split()
            if k == "anon":
                anon = int(v)
            elif k == "file":
                filec = int(v)
    except FileNotFoundError:  # cgroup v1
        m = f"{base}/memory"
        cur = int(open(f"{m}/memory.usage_in_bytes").read())
        mx = int(open(f"{m}/memory.limit_in_bytes").read())
        anon = filec = 0
        for line in open(f"{m}/memory.stat"):
            k, v = line.split()
            if k == "total_rss":
                anon = int(v)
            elif k == "total_cache":
                filec = int(v)
    if mx <= 0 or mx > 1 << 58:  # "unlimited" — use host MemTotal
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                mx = int(line.split()[1]) * 1024
                break
    return cur, mx, anon, filec


_stats_cache = {"t": 0, "data": None}


def gather_stats():
    if time.time() - _stats_cache["t"] < 3 and _stats_cache["data"]:
        return _stats_cache["data"]
    try:
        smi = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        gpu_used, gpu_total = [int(x) for x in smi.stdout.strip().split(",")]
        ram_cur, ram_max, ram_anon, ram_file = read_cgroup_mem()
        st = os.statvfs("/root")
        disk_avail, disk_size = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize

        def du(path):
            total = 0
            for f in glob.glob(os.path.join(path, "*")):
                try:
                    total += os.path.getsize(f)
                except OSError:
                    pass
            return total
        kv_tokens = 0
        logs = sorted(glob.glob("/root/server_*.log"), key=os.path.getmtime, reverse=True)
        if logs:
            for line in open(logs[0], errors="ignore"):
                if "GPU KV cache size" in line:
                    kv_tokens = int(line.rsplit(":", 1)[-1].replace(",", "").replace("tokens", "").strip())
        gpu_cache_pct = None
        pc_queries = pc_hits = None
        try:
            with urllib.request.urlopen(f"{VLLM}/metrics", timeout=3) as r:
                for line in r.read().decode().splitlines():
                    if line.startswith("vllm:kv_cache_usage_perc"):
                        gpu_cache_pct = round(float(line.rsplit(None, 1)[-1]) * 100, 1)
                    elif line.startswith("vllm:prefix_cache_queries_total"):
                        pc_queries = int(float(line.rsplit(None, 1)[-1]))
                    elif line.startswith("vllm:prefix_cache_hits_total"):
                        pc_hits = int(float(line.rsplit(None, 1)[-1]))
        except Exception:
            pass
        data = {"gpu_cache_pct": gpu_cache_pct, "pc_queries": pc_queries, "pc_hits": pc_hits,
                "gpu_used_gb": round(gpu_used / 1024, 1), "gpu_total_gb": round(gpu_total / 1024, 1),
                "ram_used_gb": round(ram_cur / 1e9, 1), "ram_total_gb": round(ram_max / 1e9, 1),
                "ram_anon_gb": round(ram_anon / 1e9, 1), "ram_file_gb": round(ram_file / 1e9, 1),
                "disk_avail_gb": round(disk_avail / 1e9, 1), "disk_total_gb": round(disk_size / 1e9, 1),
                "disk_kv_gb": round(du("/root/lmcache_disk") / 1e9, 1),
                "nixl_kv_gb": round(du("/root/lmcache_nixl") / 1e9, 1),
                "gpu_kv_tokens": kv_tokens, "gpu_kv_gb": round(kv_tokens * 196608 / 1e9, 1)}
        _stats_cache.update(t=time.time(), data=data)
        return data
    except Exception as e:
        return {"error": str(e)[-120:]}


def newest_log():
    logs = sorted(glob.glob("/root/server_*.log"), key=os.path.getmtime, reverse=True)
    return logs[0] if logs else "/dev/null"


EVIDENCE_CMDS = {
    "gpu": [
        "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv",
        "grep -a 'GPU KV cache size' {log} | tail -1",
        "curl -s http://127.0.0.1:8000/metrics | grep -E 'kv_cache_usage_perc|prefix_cache_(queries|hits)_total' | grep -v '^#'",
    ],
    "cpu": [
        "python3 /root/evidence_lookup.py LocalCPUBackend",
        "grep -E 'local_cpu|max_local_cpu_size' /root/lmcache_full.yaml 2>/dev/null || grep -E 'local_cpu' /root/lmcache_*.yaml",
        "grep -a 'Retrieved' {log} | tail -2",
    ],
    "storage": [
        "python3 /root/evidence_lookup.py",
        "grep -a 'Retrieved' {log} | tail -3",
        "grep -A6 'extra_config' /root/lmcache_full.yaml 2>/dev/null || grep -A6 'extra_config' /root/lmcache_nixl.yaml",
        "echo \"pool files: $(ls /root/lmcache_nixl 2>/dev/null | wc -l)  ·  cumulative bytes ever written: $(du -sh /root/lmcache_nixl 2>/dev/null | cut -f1) (files are reused slots — this only grows)\"",
    ],
}


def gather_evidence(tier):
    cmds = EVIDENCE_CMDS.get(tier)
    if not cmds:
        return {"error": "unknown tier"}
    log = newest_log()
    rs = residency_snapshot()
    gauge = {"gpu": f"{rs['gpu_tok']*0.000192:.1f} GB · {rs['gpu_docs']} requests",
             "cpu": f"{rs['cpu_tok']*0.000192:.1f} GB · {rs['cpu_docs']} requests",
             "storage": f"{rs['storage_tok']*0.000192:.1f} GB · {rs['storage_docs']} requests"}[tier]
    results = [{"cmd": f"# captured {time.strftime('%H:%M:%S')} — re-run on every click",
                "out": f"dashboard gauge for this tier right now: {gauge}\n"
                       f"(compare with the totals below — the gauge is the dashboard's model, "
                       f"the audit below is LMCache's own index)"}]
    for c in cmds:
        cmd = c.replace("{log}", log)
        try:
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=15)
            out = (r.stdout or r.stderr).strip() or "(no output)"
        except Exception as e:
            out = f"(failed: {e})"
        if len(out) > 6000:
            out = out[:6000] + "\n… (output truncated)"
        results.append({"cmd": cmd, "out": out})
    return {"tier": tier, "results": results}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = open("/root/index.html", "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/stats":
            self._json(gather_stats())
        elif self.path.startswith("/api/evidence"):
            tier = self.path.split("tier=")[-1] if "tier=" in self.path else ""
            self._json(gather_evidence(tier))
        elif self.path == "/api/status":
            model = vllm_ready()
            if not state["switching"]:
                detect_mode()  # self-heal if the UI started before the server's log existed
            rs = residency_snapshot()
            with lock:
                ce = dict(state["cpu_evict"])
                ce["elapsed"] = round(time.time() - ce["started"]) if ce["running"] else 0
                ws = dict(state["workset"])
                ws["elapsed"] = round(time.time() - ws["started"]) if ws["running"] else 0
                self._json({"mode": state["mode"], "switching": state["switching"],
                            "switch_elapsed": round(time.time() - state["switch_started"]) if state["switching"] else 0,
                            "ready": bool(model), "model": model, "log": state["log"][-40:],
                            "spill": spill_progress(), "cpu_evict": ce, "workset": ws,
                            "residency": rs})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/mode":
            mode = body.get("mode")
            if mode not in ("baseline", "cpu", "disk", "nixl", "full"):
                return self._json({"error": "bad mode"}, 400)
            switch_mode(mode)
            self._json({"ok": True, "mode": mode})
        elif self.path == "/api/ask":
            doc = str(body.get("doc", "cold"))
            entry = run_probe([doc])
            if "error" not in entry:
                track_request(f"cold:{time.time():.0f}" if doc == "cold" else f"doc:{doc}")
                entry["mode"] = state["mode"]
                entry["ts"] = time.strftime("%H:%M:%S")
                append_log(entry)
            self._json(entry)
        elif self.path == "/api/chat":
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "empty prompt"}, 400)
            entry = run_probe(["chat"], stdin_text=prompt)
            if "error" not in entry:
                track_request(f"chat:{abs(hash(prompt))}", max(64, len(prompt) // 4))
                snippet = prompt[:48].replace("\n", " ")
                entry["label"] = f"“{snippet}…”" if len(prompt) > 48 else f"“{snippet}”"
                entry["mode"] = state["mode"]
                entry["ts"] = time.strftime("%H:%M:%S")
                append_log(entry)
            self._json(entry)
        elif self.path == "/api/thrash":
            # pure eviction: vLLM frees every prefix-cache block, nothing runs
            try:
                req = urllib.request.Request(f"{VLLM}/reset_prefix_cache", data=b"", method="POST")
                urllib.request.urlopen(req, timeout=30)
                with lock:
                    residency["gpu"].clear(); save_residency()
                append_log({"mode": state["mode"], "label": "GPU cache evicted (reset_prefix_cache)",
                            "ttft": 0.0, "total": 0.0, "tokens": 0, "answer": "",
                            "ts": time.strftime("%H:%M:%S")})
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/evict_all":
            detail = []
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"{VLLM}/reset_prefix_cache", data=b"", method="POST"), timeout=30)
                detail.append("GPU reset")
            except Exception as e:
                detail.append(f"GPU reset failed: {str(e)[-60:]}")
            for name, loc_key in (("CPU", CPU_TIER_TOKENS), ("NIXL", STORAGE_TIER_TOKENS)):
                loc = ("LocalCPUBackend" if name == "CPU" else STORAGE_LOCATION.get(state["mode"])) \
                    if state["mode"] in loc_key else None
                if not loc:
                    continue
                try:
                    payload = json.dumps({"instance_id": "demo", "location": loc}).encode()
                    req = urllib.request.Request("http://127.0.0.1:9050/clear", data=payload,
                                                 headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=30) as r:
                        res = json.load(r)
                    detail.append(f"{name}: {res.get('num_tokens', 0):,} tok dropped")
                except Exception as e:
                    detail.append(f"{name} clear failed: {str(e)[-60:]}")
            with lock:
                residency["gpu"].clear()
                residency["cpu"].clear()
                residency["storage"].clear()
                save_residency()
            summary = " · ".join(detail)
            append_log({"mode": state["mode"], "label": f"ALL TIERS CLEARED — {summary}",
                        "ttft": 0.0, "total": 0.0, "tokens": 0, "answer": "",
                        "ts": time.strftime("%H:%M:%S")})
            self._json({"ok": True, "detail": summary})
        elif self.path == "/api/evict_storage":
            loc = STORAGE_LOCATION.get(state["mode"])
            if not loc:
                return self._json({"error": f"no storage tier exists in '{state['mode']}' mode"}, 400)
            try:
                payload = json.dumps({"instance_id": "demo", "location": loc}).encode()
                req = urllib.request.Request("http://127.0.0.1:9050/clear", data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.load(r)
                ntok = res.get("num_tokens", 0)
                with lock:
                    residency["storage"].clear(); save_residency()
                append_log({"mode": state["mode"],
                            "label": f"NIXL/storage tier evicted via controller ({ntok:,} tokens dropped)",
                            "ttft": 0.0, "total": 0.0, "tokens": 0, "answer": "",
                            "ts": time.strftime("%H:%M:%S")})
                self._json({"ok": True, "num_tokens": ntok})
            except Exception as e:
                self._json({"error": f"controller clear failed: {str(e)[-200:]}"}, 500)
        elif self.path == "/api/evict_cpu":
            # pure eviction via the LMCache controller — drops the tier's entries
            if state["mode"] not in ("cpu", "disk", "full"):
                return self._json({"error": f"no CPU tier exists in '{state['mode']}' mode"}, 400)
            try:
                payload = json.dumps({"instance_id": "demo", "location": "LocalCPUBackend"}).encode()
                req = urllib.request.Request("http://127.0.0.1:9050/clear", data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.load(r)
                ntok = res.get("num_tokens", 0)
                with lock:
                    residency["cpu"].clear(); save_residency()
                append_log({"mode": state["mode"],
                            "label": f"CPU tier evicted via controller ({ntok:,} tokens dropped)",
                            "ttft": 0.0, "total": 0.0, "tokens": 0, "answer": "",
                            "ts": time.strftime("%H:%M:%S")})
                self._json({"ok": True, "num_tokens": ntok})
            except Exception as e:
                self._json({"error": f"controller clear failed: {str(e)[-200:]}"}, 500)
        elif self.path == "/api/sweep":
            if not vllm_ready():
                return self._json({"error": "vLLM server is not ready"}, 503)
            s = int(body.get("set", 0))
            if s not in (0, 1, 2, 3):
                return self._json({"error": "set must be 0-3"}, 400)
            self._json({"started": start_sweep(s)})
        elif self.path == "/api/spill":
            if not vllm_ready():
                return self._json({"error": "vLLM server is not ready"}, 503)
            self._json({"started": start_spill()})
        elif self.path == "/api/clear":
            with lock:
                state["log"] = []
                try:
                    json.dump([], open(LOG_PATH, "w"))
                except OSError:
                    pass
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print("on-box demo server on :7811")
    ThreadingHTTPServer(("127.0.0.1", 7811), H).serve_forever()
