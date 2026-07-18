# Rapid deploy — KV cache tiering demo (vLLM + LMCache + NIXL)

One command stands up the complete interactive demo on any vast.ai A100 80GB node.

## Prerequisites (once per workstation)

```bash
pip install --user vastai
vastai set api-key <YOUR_KEY>          # cloud.vast.ai → Account → API Keys
vastai create ssh-key "$(cat ~/.ssh/id_ed25519.pub)"   # register your SSH key
```

## Deploy

```bash
cd ~/kvcache
./deploy/deploy.sh                # rents the cheapest suitable A100 and deploys (~$1/hr)
# or
./deploy/deploy.sh 45219078      # (re)deploys onto an existing instance — idempotent
```

Takes 4–8 minutes on a fresh box (docker image pull + 28 GB model + vLLM load).
Ends with the dashboard live at **http://127.0.0.1:7811** through an
auto-restarting SSH tunnel.

## What it deploys

| Piece | Purpose |
|---|---|
| `run_server.sh` | vLLM launcher, modes `baseline` and `full` (GPU 47 GB → CPU 60 GB → NIXL 85.6 GB), `PYTHONHASHSEED=0`, dev-mode reset endpoint, fd limits |
| `switch.sh` | crash-proof detached mode switching (kills orphaned `VLLM::` engine cores, wipes stale tier files) |
| `onbox_server.py` + `index.html` | the dashboard (port 7811 on the box): sweeps, per-tier clears, residency gauges, evidence terminal, chat |
| `ask.py` / `spill_test.py` / `evidence_lookup.py` | on-box TTFT probe · physical-NVMe-read proof · LMCache index audit |
| `bootstrap.sh` | idempotent box setup: nixl wheel, model, 40-doc corpus, **LMCache clear() patch**, controller, services |

## Baked-in fixes you'd otherwise rediscover the hard way

- fresh vast instances sometimes boot to `exited` → auto `vastai start`
- `nixl_pool_size` is a slot **count** (1,700 × 48 MiB), never bytes
- POSIX NIXL backend needs `nixl_buffer_device: cpu` + raised `ulimit -n`
- `PYTHONHASHSEED` pinned in engine **and** controller or `/lookup` never matches
- LMCache 0.5.1 NIXL backend lacks `clear()` (patched) and never reports to the
  controller registry (documented blind spot in the evidence panel)
- `use_direct_io: true` deadlocks 0.5.1 at startup — don't enable it

## Tear down (billing stops immediately)

```bash
vastai destroy instance <ID>
kill "$(cat /tmp/kvdemo_tunnel.pid)"   # stop the local tunnel
```
