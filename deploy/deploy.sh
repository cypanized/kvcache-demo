#!/bin/bash
# One-command deployment of the KV-cache tiering demo onto a vast.ai A100 node.
#
#   ./deploy.sh                 rent the cheapest suitable A100 (~$1/hr) and deploy
#   ./deploy.sh <INSTANCE_ID>   deploy/redeploy onto an existing instance
#
# When it finishes: dashboard at http://127.0.0.1:7811 (SSH tunnel auto-started).
# Tear down with:   vastai destroy instance <ID>
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root (~/kvcache)

# vastai CLI (pip user install lands off-PATH on macOS)
VAST="vastai"
command -v vastai >/dev/null 2>&1 || VAST="$HOME/Library/Python/3.11/bin/vastai"
$VAST show user --raw >/dev/null 2>&1 || { echo "vastai CLI not configured (vastai set api-key ...)"; exit 1; }

QUERY='gpu_name in [A100_SXM4,A100_PCIE] num_gpus=1 gpu_ram>=75 disk_space>=150 reliability>0.98 inet_down>=500 rentable=true verified=true cuda_vers>=12.4'
IMAGE="lmcache/vllm-openai:latest"

if [ $# -ge 1 ]; then
  IID="$1"
  echo "== using existing instance $IID =="
else
  echo "== searching offers =="
  OFFER=$($VAST search offers "$QUERY" -o 'dph' --raw | python3 -c 'import json,sys; o=json.load(sys.stdin)[0]; print(o["id"])')
  PRICE=$($VAST search offers "$QUERY" -o 'dph' --raw | python3 -c 'import json,sys; o=json.load(sys.stdin)[0]; print(round(o["dph_total"],2))')
  echo "   cheapest suitable offer: $OFFER at \$$PRICE/hr"
  IID=$($VAST create instance "$OFFER" --image "$IMAGE" --disk 150 --ssh --direct --label kvcache-demo --raw \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["new_contract"])')
  echo "== rented instance $IID (billing started) =="
fi

echo "== waiting for instance + SSH (vast sometimes parks fresh instances as 'exited' — auto-starting) =="
HOST=""; PORT=""
for i in $(seq 1 80); do
  read -r ST HOST PORT <<<"$($VAST show instance "$IID" --raw 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("actual_status"), d.get("ssh_host"), d.get("ssh_port"))')" || true
  if [ "$ST" = "running" ] && ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -p "$PORT" "root@$HOST" true 2>/dev/null; then
    break
  fi
  [ "$ST" = "exited" ] && $VAST start instance "$IID" >/dev/null 2>&1 || true
  sleep 15
done
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -p "$PORT" "root@$HOST" true 2>/dev/null \
  || { echo "instance never became reachable"; exit 1; }
echo "   up at $HOST:$PORT"

echo "== pushing demo files =="
scp -o StrictHostKeyChecking=no -P "$PORT" \
  remote/run_server.sh remote/gen_docs.py \
  demo/switch.sh demo/ask.py demo/spill_test.py demo/evidence_lookup.py \
  demo/onbox_server.py demo/index.html deploy/bootstrap.sh \
  "root@$HOST:/root/"

echo "== dispatching bootstrap (detached on the box — survives SSH drops) =="
ssh -o StrictHostKeyChecking=no -p "$PORT" "root@$HOST" \
  'chmod +x /root/*.sh; setsid nohup bash /root/bootstrap.sh > /root/bootstrap.log 2>&1 < /dev/null & echo dispatched'

echo "== starting local tunnel: http://127.0.0.1:7811 =="
[ -f /tmp/kvdemo_tunnel.pid ] && kill "$(cat /tmp/kvdemo_tunnel.pid)" 2>/dev/null || true
pkill -f "7811:127.0.0.1:7811" 2>/dev/null || true
nohup bash -c "while true; do ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -o ExitOnForwardFailure=yes -N -L 7811:127.0.0.1:7811 -p $PORT root@$HOST; sleep 3; done" \
  >/dev/null 2>&1 &
echo $! > /tmp/kvdemo_tunnel.pid

echo "== waiting for the demo to come up (model download + vLLM load; 4-8 min on a fresh box) =="
for i in $(seq 1 100); do
  R=$(curl -s -m 3 http://127.0.0.1:7811/api/status 2>/dev/null \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ready"] and not d["switching"])' 2>/dev/null) || true
  [ "$R" = "True" ] && break
  sleep 10
done
if [ "$R" = "True" ]; then
  echo ""
  echo "✔ DEMO READY — open http://127.0.0.1:7811"
  echo "  instance: $IID at $HOST:$PORT (~\$1/hr — destroy when done:)"
  echo "  $VAST destroy instance $IID"
else
  echo "✘ demo not ready yet — check: ssh -p $PORT root@$HOST 'tail /root/bootstrap.log /root/server_full.log'"
  exit 1
fi
