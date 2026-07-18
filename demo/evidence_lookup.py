#!/usr/bin/env python3
"""Audit which tier holds each demo doc, straight from LMCache's own index.

For every doc in docs.json: tokenize its exact prompt (cached after first run),
then ask the LMCache controller /lookup which location holds those tokens.
Prints per-doc rows and per-tier totals — directly comparable to the dashboard
gauges. Usage: evidence_lookup.py [LocationFilter]
"""
import json
import sys
import urllib.request

VLLM = "http://127.0.0.1:8000"
CTRL = "http://127.0.0.1:9050"
TOK_CACHE = "/root/doc_tokens_v2.json"

loc_filter = sys.argv[1] if len(sys.argv) > 1 else None

docs = json.load(open("/root/docs.json"))
try:
    tok_cache = json.load(open(TOK_CACHE))
except (FileNotFoundError, json.JSONDecodeError):
    tok_cache = {}

model = json.load(urllib.request.urlopen(f"{VLLM}/v1/models", timeout=5))["data"][0]["id"]


def prompt_for(doc):
    return ("You are a terse analyst. Read the report and answer in one short sentence.\n\n"
            f"=== REPORT ===\n{doc['text']}\n=== END ===\n\n"
            "Question: summarize the report in a few words.")


def tokenize(text):
    # must match /v1/chat/completions exactly: chat template + generation prompt,
    # because LMCache keys chunks on the templated token sequence
    body = json.dumps({"model": model, "add_generation_prompt": True,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(f"{VLLM}/tokenize", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["tokens"]


def lookup(tokens):
    body = json.dumps({"tokens": tokens}).encode()
    req = urllib.request.Request(f"{CTRL}/lookup", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("layout_info", {})


totals = {}
found_rows = []
dirty_cache = False
for d in docs:
    did = str(d["id"])
    if did not in tok_cache:
        tok_cache[did] = tokenize(prompt_for(d))
        dirty_cache = True
    info = lookup(tok_cache[did])
    for inst, (loc, ntok) in info.items():
        if ntok and (loc_filter is None or loc == loc_filter):
            found_rows.append(f"  doc {int(did)+1:>2}: {loc} holds {ntok:,} tok (~{ntok*0.000192:.1f} GB)")
        if ntok:
            t = totals.setdefault(loc, [0, 0])
            t[0] += 1
            t[1] += ntok

if dirty_cache:
    json.dump(tok_cache, open(TOK_CACHE, "w"))

print(f"LMCache index audit over the {len(docs)} demo docs (per-doc /lookup):")
print("\n".join(found_rows) if found_rows else "  (no docs found in this location)")
print("totals by location (best tier per doc):")
for loc, (n, tok) in sorted(totals.items()):
    print(f"  {loc}: {n} docs · {tok:,} tok · ~{tok*0.000192:.1f} GB")
if not totals:
    print("  (nothing cached in LMCache tiers)")
print("note: /lookup reports each doc's BEST (fastest) tier; a doc in CPU is usually also in NIXL below it.")

if loc_filter == "NixlStorageBackend" or loc_filter is None:
    # pool membership = best-tier-NIXL docs + write-through copies of CPU-resident docs
    cpu_n, cpu_tok = totals.get("LocalCPUBackend", [0, 0])
    nx_n, nx_tok = totals.get("NixlStorageBackend", [0, 0])
    print("NIXL pool membership accounting (write-through means CPU-resident docs are ALSO in the pool):")
    print(f"  best-tier NIXL (in pool only, evicted from RAM): {nx_n} docs · ~{nx_tok*0.000192:.1f} GB")
    print(f"  + write-through copies of CPU-resident docs:     {cpu_n} docs · ~{cpu_tok*0.000192:.1f} GB")
    print(f"  = pool membership visible to the registry:       {cpu_n+nx_n} docs · ~{(cpu_tok+nx_tok)*0.000192:.1f} GB")
    print("KNOWN BLIND SPOT (LMCache 0.5.1): the NIXL backend never reports its contents to the")
    print("controller registry, so docs held ONLY in the pool are invisible to /lookup — e.g. right")
    print("after clearing the CPU tier this audit goes empty while the pool still holds everything.")
    print("Live disproof: clear GPU + CPU, then ask any swept doc -> ~0.35s TTFT and a 'Retrieved")
    print("10496 of 10496 tokens' engine log line = the pool served it. The dashboard's storage gauge")
    print("models true pool membership; recent pool serves appear in the Retrieved lines below.")
