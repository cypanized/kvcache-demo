#!/usr/bin/env python3
"""Generate synthetic long documents for KV-cache benchmarking.

Each doc is ~TARGET_TOKENS of deterministic pseudo-prose so runs are
reproducible. Docs are unique (no cross-doc prefix overlap beyond the
system prompt) so each one costs a full prefill when not cached.
"""
import json
import random
import sys

NUM_DOCS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
TARGET_WORDS = int(sys.argv[2]) if len(sys.argv) > 2 else 9000  # ~12k tokens

WORDS = (
    "storage cache latency throughput tensor gpu memory bandwidth inference "
    "prefill decode token model layer attention key value block page pipeline "
    "cluster node fabric nvme flash namespace shard replica snapshot ledger "
    "quorum epoch commit journal buffer queue scheduler kernel stream batch"
).split()

def make_doc(seed: int) -> str:
    rng = random.Random(seed)
    parts = [f"Document {seed}: internal engineering report {seed:04d}.\n"]
    words = 0
    section = 1
    while words < TARGET_WORDS:
        sent_len = rng.randint(8, 18)
        sent = " ".join(rng.choice(WORDS) for _ in range(sent_len))
        parts.append(sent.capitalize() + ".")
        words += sent_len
        if words // 800 >= section:
            parts.append(f"\n\nSection {section + 1} of report {seed:04d}.\n")
            section += 1
    return " ".join(parts)

docs = [{"id": i, "text": make_doc(i)} for i in range(NUM_DOCS)]
with open("docs.json", "w") as f:
    json.dump(docs, f)
print(f"wrote {NUM_DOCS} docs, ~{TARGET_WORDS} words each -> docs.json")
