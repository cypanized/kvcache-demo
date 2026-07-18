#!/usr/bin/env python3
"""KV-cache tiering benchmark against a vLLM OpenAI endpoint.

Workload: round-robin over N long documents, PASSES times each, asking a
short question about the doc. The doc set is sized to overflow the GPU KV
cache, so:
  - baseline vLLM: pass 2+ re-prefills almost every doc (GPU cache thrashed)
  - LMCache CPU/disk tiers: pass 2+ hits the offloaded cache -> fast TTFT

Measures per-request TTFT (time to first streamed token) and total latency.
Writes results JSON for offline analysis.

Usage: python3 benchmark.py <config_label> [--passes 3] [--docs docs.json]
"""
import argparse
import json
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("label")
ap.add_argument("--passes", type=int, default=3)
ap.add_argument("--docs", default="docs.json")
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--max-tokens", type=int, default=32)
args = ap.parse_args()

with open(args.docs) as f:
    docs = json.load(f)

model = requests.get(f"{args.base}/v1/models").json()["data"][0]["id"]
print(f"model={model} docs={len(docs)} passes={args.passes} label={args.label}")

results = []
for p in range(args.passes):
    for d in docs:
        prompt = (
            "You are a terse analyst. Read the report and answer in one short sentence.\n\n"
            f"=== REPORT ===\n{d['text']}\n=== END ===\n\n"
            f"Question: summarize section 2 of report {d['id']:04d} in a few words."
        )
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": args.max_tokens,
            "temperature": 0,
            "stream": True,
        }
        t0 = time.perf_counter()
        ttft = None
        ntok = 0
        with requests.post(f"{args.base}/v1/chat/completions", json=body, stream=True, timeout=600) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    ntok += 1
        total = time.perf_counter() - t0
        results.append({"pass": p, "doc": d["id"], "ttft": ttft, "total": total, "tokens": ntok})
        print(f"  pass={p} doc={d['id']:3d} ttft={ttft:6.3f}s total={total:6.3f}s")

out = f"results_{args.label}.json"
with open(out, "w") as f:
    json.dump({"label": args.label, "model": model, "results": results}, f, indent=1)

# quick summary: cold pass vs warm passes
cold = [r["ttft"] for r in results if r["pass"] == 0]
warm = [r["ttft"] for r in results if r["pass"] > 0]
def stats(xs):
    xs = sorted(xs)
    return f"mean={sum(xs)/len(xs):.3f}s p50={xs[len(xs)//2]:.3f}s p95={xs[int(len(xs)*0.95)]:.3f}s"
print(f"\n[{args.label}] cold (pass 0):  {stats(cold)}")
print(f"[{args.label}] warm (pass 1+): {stats(warm)}")
print(f"wrote {out}")
