#!/usr/bin/env python3
"""Disk-spill proof, run ON the instance.

Sweeps 16 synthetic 12k-token docs (~36 GB KV) — 2x the disk-mode CPU tier —
then drops the OS page cache for the KV files and sweeps again, measuring
per-doc TTFT and PHYSICAL NVMe bytes read by the engine (/proc/<pid>/io).

Prints one JSON line per event, then a final summary line.
"""
import glob
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

N_DOCS = 16
DOCS_PATH = "/root/docs_spill.json"
KV_DIRS = ["/root/lmcache_disk", "/root/lmcache_nixl"]

WORDS = ("storage cache latency throughput tensor gpu memory bandwidth inference "
         "prefill decode token model layer attention key value block page pipeline").split()


def make_doc(seed):
    rng = random.Random(seed)
    parts = [f"Document {seed}: internal engineering report {seed}.\n"]
    n = 0
    while n < 9000:
        k = rng.randint(8, 18)
        parts.append(" ".join(rng.choice(WORDS) for _ in range(k)).capitalize() + ".")
        n += k
    return " ".join(parts)


if os.path.exists(DOCS_PATH):
    docs = json.load(open(DOCS_PATH))
else:
    docs = [{"id": 1000 + i, "text": make_doc(1000 + i)} for i in range(N_DOCS)]
    json.dump(docs, open(DOCS_PATH, "w"))

model = json.load(urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5))["data"][0]["id"]


def engine_pid():
    r = subprocess.run(["pgrep", "-f", "VLLM::EngineCore"], capture_output=True, text=True)
    pids = r.stdout.split()
    return int(pids[0]) if pids else None


def read_bytes(pid):
    try:
        for line in open(f"/proc/{pid}/io"):
            if line.startswith("read_bytes"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def ask(text, doc_id):
    prompt = ("You are a terse analyst. Read the report and answer in one short sentence.\n\n"
              f"=== REPORT ===\n{text}\n=== END ===\n\n"
              "Question: summarize the report in a few words.")
    body = json.dumps({"model": model, "max_tokens": 16, "temperature": 0, "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    ttft = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            if not raw.startswith(b"data:"):
                continue
            payload = raw[5:].strip()
            if payload == b"[DONE]":
                break
            if json.loads(payload)["choices"][0]["delta"].get("content") and ttft is None:
                ttft = time.perf_counter() - t0
    return ttft


def drop_page_cache():
    os.sync()
    n = 0
    for d in KV_DIRS:
        for f in glob.glob(os.path.join(d, "*")):
            try:
                fd = os.open(f, os.O_RDONLY)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.close(fd)
                n += 1
            except OSError:
                pass
    return n


pid = engine_pid()

# pass 1: cold sweep — writes KV through the tiers
for d in docs:
    t = ask(d["text"], d["id"])
    print(json.dumps({"phase": "cold", "doc": d["id"], "ttft": round(t, 3)}), flush=True)

dropped = drop_page_cache()
# also reset the GPU prefix cache: with gpu-memory-utilization 0.92 the GPU
# could hold all 16 docs itself and pass 2 would never touch storage
try:
    urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:8000/reset_prefix_cache", data=b"", method="POST"), timeout=30)
except Exception:
    pass
print(json.dumps({"phase": "cachedrop", "files": dropped}), flush=True)

# pass 2: warm sweep with physical read attribution per doc
total_gb = 0.0
warm = []
for d in docs:
    b0 = read_bytes(pid)
    t = ask(d["text"], d["id"])
    gb = (read_bytes(pid) - b0) / 1e9
    total_gb += gb
    warm.append(t)
    print(json.dumps({"phase": "warm", "doc": d["id"], "ttft": round(t, 3),
                      "gb_nvme": round(gb, 2)}), flush=True)

print(json.dumps({"phase": "summary", "docs": len(docs),
                  "warm_ttft_mean": round(sum(warm) / len(warm), 3),
                  "total_gb_nvme": round(total_gb, 1)}), flush=True)
