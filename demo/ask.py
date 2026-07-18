#!/usr/bin/env python3
"""One-shot TTFT probe, run ON the GPU instance so timing excludes the WAN.

Usage: python3 ask.py <doc_id|cold>   -> prints one JSON line
"""
import json
import random
import sys
import time
import urllib.request

WORDS = ("storage cache latency throughput tensor gpu memory bandwidth inference "
         "prefill decode token model layer attention key value block page pipeline").split()

arg = sys.argv[1] if len(sys.argv) > 1 else "cold"
MAX_TOKENS = 32
if arg == "chat":
    # free-form prompt supplied on stdin; used verbatim
    text, label = None, "custom prompt"
    prompt_override = sys.stdin.read()
    MAX_TOKENS = 256
elif arg == "cold":
    seed = random.randint(10_000, 9_999_999)
    rng = random.Random(seed)
    parts = [f"Document {seed}: internal engineering report {seed}.\n"]
    n = 0
    while n < 9000:
        k = rng.randint(8, 18)
        parts.append(" ".join(rng.choice(WORDS) for _ in range(k)).capitalize() + ".")
        n += k
    text, label = " ".join(parts), f"new doc {seed}"
else:
    docs = json.load(open("/root/docs.json"))
    d = next(x for x in docs if x["id"] == int(arg))
    text, label = d["text"], f"doc {d['id']}"

model = json.load(urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5))["data"][0]["id"]
if arg == "chat":
    prompt = prompt_override
else:
    prompt = ("You are a terse analyst. Read the report and answer in one short sentence.\n\n"
              f"=== REPORT ===\n{text}\n=== END ===\n\n"
              "Question: summarize the report in a few words.")
body = json.dumps({"model": model, "max_tokens": MAX_TOKENS, "temperature": 0, "stream": True,
                   "messages": [{"role": "user", "content": prompt}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions", data=body,
                             headers={"Content-Type": "application/json"})
t0 = time.perf_counter()
ttft = None
ntok = 0
answer = []
with urllib.request.urlopen(req, timeout=600) as r:
    for raw in r:
        if not raw.startswith(b"data:"):
            continue
        payload = raw[5:].strip()
        if payload == b"[DONE]":
            break
        delta = json.loads(payload)["choices"][0]["delta"].get("content")
        if delta:
            if ttft is None:
                ttft = time.perf_counter() - t0
            ntok += 1
            answer.append(delta)
total = time.perf_counter() - t0
print(json.dumps({"label": label, "ttft": round(ttft, 3), "total": round(total, 3),
                  "tokens": ntok, "answer": "".join(answer).strip()}))
