#!/usr/bin/env python3
"""Local control server for the KV-cache tiering demo frontend.

Runs on your Mac. Serves the dashboard, keeps an SSH tunnel to the GPU
instance, switches vLLM server modes over SSH, and measures TTFT by
streaming each request server-side (authoritative, no browser jitter).

Usage:  python3 server.py          then open http://127.0.0.1:7811
Reads instance host/port from instance.json next to this file.
"""
import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "instance.json")))
HOST, PORT = CFG["ssh_host"], int(CFG["ssh_port"])
TUNNEL_PORT = 18000
VLLM = f"http://127.0.0.1:{TUNNEL_PORT}"
SSH_BASE = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
            "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/kvdemo-ssh-%r@%h:%p",
            "-o", "ControlPersist=10m",
            "-p", str(PORT), f"root@{HOST}"]

DOCS = json.load(open(os.path.join(HERE, "docs.json")))
DOC_BY_ID = {d["id"]: d for d in DOCS}

WORDS = ("storage cache latency throughput tensor gpu memory bandwidth inference "
         "prefill decode token model layer attention key value block page pipeline").split()

LOG_PATH = os.path.join(HERE, "log.json")
try:
    _saved_log = json.load(open(LOG_PATH))
except (FileNotFoundError, json.JSONDecodeError):
    _saved_log = []

state = {"mode": "baseline", "switching": False, "switch_started": 0, "log": _saved_log}
state_lock = threading.Lock()


def append_log(entry):
    with state_lock:
        state["log"].append(entry)
        try:
            json.dump(state["log"], open(LOG_PATH, "w"), indent=1)
        except OSError:
            pass


def detect_mode():
    """On startup, infer the live mode from the newest server log on the box."""
    try:
        r = subprocess.run(SSH_BASE + ["ls -t /root/server_*.log 2>/dev/null | head -1"],
                           capture_output=True, text=True, timeout=20)
        name = r.stdout.strip().rsplit("server_", 1)[-1].removesuffix(".log")
        if name in ("baseline", "cpu", "disk", "nixl"):
            with state_lock:
                state["mode"] = name
    except Exception:
        pass


threading.Thread(target=detect_mode, daemon=True).start()


# ---------- ssh tunnel ----------
def tunnel_loop():
    # plain ssh (no ControlMaster) so the tunnel doesn't share a transport
    # with exec channels — one dying must not take the other down
    tunnel_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
                  "-o", "ExitOnForwardFailure=yes", "-N",
                  "-L", f"{TUNNEL_PORT}:127.0.0.1:8000",
                  "-p", str(PORT), f"root@{HOST}"]
    while True:
        p = subprocess.Popen(tunnel_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.wait()
        time.sleep(3)


threading.Thread(target=tunnel_loop, daemon=True).start()


def vllm_ready():
    try:
        with urllib.request.urlopen(f"{VLLM}/v1/models", timeout=2) as r:
            return json.load(r)["data"][0]["id"]
    except Exception:
        return None


def switch_mode(mode):
    with state_lock:
        state["switching"] = True
        state["switch_started"] = time.time()
        state["mode"] = mode
    # fire-and-forget: switch.sh does the kill+relaunch ON the box, so the ssh
    # session doesn't have to survive vLLM's shutdown window
    cmd = f"nohup /root/switch.sh {mode} >/root/switch.log 2>&1 & echo dispatched"
    r = subprocess.run(SSH_BASE + [cmd], capture_output=True, text=True, timeout=30)
    print(f"[switch->{mode}] rc={r.returncode} out={r.stdout.strip()!r} err={r.stderr.strip()[-200:]!r}",
          flush=True)
    def waiter():
        # old server must go down first, else we'd see its /v1/models and
        # declare the switch done before the new one even starts loading
        for _ in range(15):
            if not vllm_ready():
                break
            time.sleep(2)
        for _ in range(120):
            if vllm_ready():
                break
            time.sleep(5)
        with state_lock:
            state["switching"] = False
    threading.Thread(target=waiter, daemon=True).start()


def make_cold_doc():
    seed = random.randint(10_000, 9_999_999)
    rng = random.Random(seed)
    parts = [f"Document {seed}: internal engineering report {seed}.\n"]
    words = 0
    while words < 9000:
        n = rng.randint(8, 18)
        parts.append(" ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + ".")
        words += n
    return seed, " ".join(parts)


def ask(doc_id):
    """Run the TTFT probe ON the instance so timings exclude the WAN/tunnel."""
    if not vllm_ready():
        return {"error": "vLLM server is not ready"}
    doc_arg = "cold" if doc_id == "cold" else str(int(doc_id))
    r = subprocess.run(SSH_BASE + [f"python3 /root/ask.py {doc_arg}"],
                       capture_output=True, text=True, timeout=600)
    line = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return {"error": (r.stderr.strip() or "probe failed")[-300:]}
    entry["mode"] = state["mode"]
    entry["ts"] = time.strftime("%H:%M:%S")
    append_log(entry)
    return entry


_stats_cache = {"t": 0, "data": None}


def gather_stats():
    if time.time() - _stats_cache["t"] < 8 and _stats_cache["data"]:
        return _stats_cache["data"]
    cmd = (
        "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits; "
        "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max 2>/dev/null; "
        "awk '$1==\"anon\"||$1==\"file\"{print $2}' /sys/fs/cgroup/memory.stat 2>/dev/null; "
        "df -B1 --output=avail,size /root | tail -1; "
        "du -sb /root/lmcache_disk 2>/dev/null | cut -f1; "
        "du -sb /root/lmcache_nixl 2>/dev/null | cut -f1; "
        "grep -h 'GPU KV cache size' /root/server_*.log 2>/dev/null | tail -1"
    )
    try:
        r = subprocess.run(SSH_BASE + [cmd], capture_output=True, text=True, timeout=20)
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        gpu_used, gpu_total = [int(x) for x in lines[0].split(",")]
        ram_cur, ram_max = int(lines[1]), int(lines[2])
        ram_anon, ram_file = int(lines[3]), int(lines[4])
        disk_avail, disk_size = [int(x) for x in lines[5].split()]
        disk_kv = int(lines[6]) if len(lines) > 6 and lines[6].isdigit() else 0
        nixl_kv = int(lines[7]) if len(lines) > 7 and lines[7].isdigit() else 0
        kv_tokens = 0
        for l in lines:
            if "GPU KV cache size" in l:
                kv_tokens = int(l.rsplit(":", 1)[-1].replace(",", "").replace("tokens", "").strip())
        data = {"gpu_used_gb": round(gpu_used / 1024, 1), "gpu_total_gb": round(gpu_total / 1024, 1),
                "ram_used_gb": round(ram_cur / 1e9, 1), "ram_total_gb": round(ram_max / 1e9, 1),
                "ram_anon_gb": round(ram_anon / 1e9, 1), "ram_file_gb": round(ram_file / 1e9, 1),
                "disk_avail_gb": round(disk_avail / 1e9, 1), "disk_total_gb": round(disk_size / 1e9, 1),
                "disk_kv_gb": round(disk_kv / 1e9, 1), "nixl_kv_gb": round(nixl_kv / 1e9, 1),
                "gpu_kv_tokens": kv_tokens, "gpu_kv_gb": round(kv_tokens * 196608 / 1e9, 1)}
        _stats_cache.update(t=time.time(), data=data)
        return data
    except Exception as e:
        return {"error": str(e)[-120:]}


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
            data = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/stats":
            self._json(gather_stats())
        elif self.path == "/api/status":
            model = vllm_ready()
            with state_lock:
                self._json({"mode": state["mode"], "switching": state["switching"],
                            "switch_elapsed": round(time.time() - state["switch_started"]) if state["switching"] else 0,
                            "ready": bool(model), "model": model, "log": state["log"][-40:]})
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
            if mode not in ("baseline", "cpu", "disk", "nixl"):
                return self._json({"error": "bad mode"}, 400)
            switch_mode(mode)
            self._json({"ok": True, "mode": mode})
        elif self.path == "/api/ask":
            try:
                self._json(ask(body.get("doc", "cold")))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/chat":
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "empty prompt"}, 400)
            if not vllm_ready():
                return self._json({"error": "vLLM server is not ready"}, 503)
            try:
                r = subprocess.run(SSH_BASE + ["python3 /root/ask.py chat"],
                                   input=prompt, capture_output=True, text=True, timeout=600)
                line = (r.stdout.strip().splitlines() or [""])[-1]
                entry = json.loads(line)
                snippet = prompt[:48].replace("\n", " ")
                entry["label"] = f"“{snippet}…”" if len(prompt) > 48 else f"“{snippet}”"
                entry["mode"] = state["mode"]
                entry["ts"] = time.strftime("%H:%M:%S")
                append_log(entry)
                self._json(entry)
            except json.JSONDecodeError:
                self._json({"error": (r.stderr.strip() or "probe failed")[-300:]}, 500)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/spill":
            if not vllm_ready():
                return self._json({"error": "vLLM server is not ready"}, 503)
            try:
                r = subprocess.run(SSH_BASE + ["python3 /root/spill_test.py"],
                                   capture_output=True, text=True, timeout=900)
                summary = None
                for line in r.stdout.strip().splitlines():
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
                    self._json({"ok": True, **summary})
                else:
                    self._json({"error": (r.stderr.strip() or "spill test produced no summary")[-300:]}, 500)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/clear":
            with state_lock:
                state["log"] = []
                try:
                    json.dump([], open(LOG_PATH, "w"))
                except OSError:
                    pass
            self._json({"ok": True})
        elif self.path == "/api/thrash":
            # push 7 throwaway 12k-token docs through prefill (~85k tokens),
            # evicting everything else from the GPU KV cache
            try:
                for i in range(7):
                    r = subprocess.run(SSH_BASE + ["python3 /root/ask.py cold"],
                                       capture_output=True, text=True, timeout=120)
                append_log({"mode": state["mode"], "label": "GPU cache evicted (7 new docs)",
                            "ttft": 0.0, "total": 0.0, "tokens": 0, "answer": "",
                            "ts": time.strftime("%H:%M:%S")})
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"KV-cache demo: http://127.0.0.1:7811  (instance {HOST}:{PORT})")
    ThreadingHTTPServer(("127.0.0.1", 7811), H).serve_forever()
