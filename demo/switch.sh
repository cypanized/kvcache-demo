#!/bin/bash
# Detached mode switch, runs entirely on the instance: kill old vLLM, launch new mode.
# Invoked as: nohup /root/switch.sh <mode> & — survives the ssh session that starts it.
MODE="${1:?usage: switch.sh baseline|cpu|disk|nixl}"
pkill -f 'run_serve[r].sh' 2>/dev/null
pkill -f 'vllm [s]erve' 2>/dev/null
sleep 6
# engine cores rename themselves VLLM::EngineCore and can orphan-survive,
# holding GPU memory that makes the next launch fail
pkill -9 -f 'vllm [s]erve' 2>/dev/null
pkill -9 -f 'VLLM::' 2>/dev/null
sleep 2
# fresh tiers each switch: the old index dies with the server anyway, and
# stale KV files otherwise accumulate ~2GB/doc until the disk fills
rm -rf /root/lmcache_disk/* /root/lmcache_nixl/*.bin 2>/dev/null
nohup /root/run_server.sh "$MODE" >/dev/null 2>&1 &
echo "switched to $MODE at $(date)"
