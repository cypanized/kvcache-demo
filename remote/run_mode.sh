#!/bin/bash
# Full benchmark cycle for one config: restart server in MODE, wait, benchmark.
set -uo pipefail
MODE="${1:?usage: run_mode.sh baseline|cpu|disk|nixl}"

# bracket trick so pkill doesn't match this script's own cmdline
pkill -f "run_serve[r].sh" 2>/dev/null
pkill -f "vllm [s]erve" 2>/dev/null
sleep 5
pkill -9 -f "vllm [s]erve" 2>/dev/null
sleep 3

nohup /root/run_server.sh "$MODE" >/dev/null 2>&1 &
echo "launched $MODE, waiting for readiness..."

for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
    echo "READY after ~$((i*10))s"
    cd /root && exec python3 benchmark.py "$MODE" --passes 3
  fi
  sleep 10
done
echo "SERVER TIMEOUT for mode=$MODE"
tail -40 "/root/server_${MODE}.log"
exit 1
