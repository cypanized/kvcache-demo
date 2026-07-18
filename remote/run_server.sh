#!/bin/bash
# Launch vLLM in one of three KV-cache configurations.
#   ./run_server.sh baseline   - vanilla vLLM, GPU KV cache only
#   ./run_server.sh cpu        - LMCache tier: GPU -> CPU RAM
#   ./run_server.sh disk       - LMCache tier: GPU -> CPU -> NVMe (external storage stand-in)
# The GPU KV cache is deliberately capped (gpu-memory-utilization) so the
# benchmark working set overflows it -- that's what makes tiering matter.
set -euo pipefail
MODE="${1:?usage: run_server.sh baseline|cpu|disk|nixl|full}"
# NIXL file pool keeps one fd per descriptor slot
ulimit -n 65536 2>/dev/null || true
# expose /reset_prefix_cache (pure cache eviction for the demo's evict buttons)
export VLLM_SERVER_DEV_MODE=1
# consistent chunk hashing across worker & controller processes (needed for /lookup)
export PYTHONHASHSEED=0
MODEL="${MODEL:-Qwen/Qwen2.5-14B-Instruct}"
GPU_UTIL="${GPU_UTIL:-0.92}"
LOG="/root/server_${MODE}.log"

COMMON_ARGS=(
  "$MODEL"
  --host 127.0.0.1 --port 8000
  --gpu-memory-utilization "$GPU_UTIL"
  --max-model-len 16384
)

mkdir -p /root/lmcache_disk

case "$MODE" in
  baseline)
    exec vllm serve "${COMMON_ARGS[@]}" >"$LOG" 2>&1
    ;;
  cpu)
    cat > /root/lmcache_cpu.yaml <<EOF
chunk_size: 256
local_cpu: true
max_local_cpu_size: 80
enable_controller: true
lmcache_instance_id: "demo"
controller_pull_url: "localhost:8300"
controller_reply_url: "localhost:8400"
lmcache_worker_ports: [8500]
EOF
    LMCACHE_CONFIG_FILE=/root/lmcache_cpu.yaml \
    exec vllm serve "${COMMON_ARGS[@]}" \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
      >"$LOG" 2>&1
    ;;
  disk)
    cat > /root/lmcache_disk.yaml <<EOF
chunk_size: 256
local_cpu: true
max_local_cpu_size: 20
local_disk: "file:///root/lmcache_disk/"
max_local_disk_size: 120
enable_controller: true
lmcache_instance_id: "demo"
controller_pull_url: "localhost:8300"
controller_reply_url: "localhost:8400"
lmcache_worker_ports: [8500]
EOF
    LMCACHE_CONFIG_FILE=/root/lmcache_disk.yaml \
    exec vllm serve "${COMMON_ARGS[@]}" \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
      >"$LOG" 2>&1
    ;;
  full)
    # the production topology: GPU -> pinned CPU RAM -> NIXL pool on NVMe
    # tier pyramid: GPU ~47 GB (hw) < CPU 60 GB (config) < NIXL 81.6 GB (1700 slots)
    mkdir -p /root/lmcache_nixl
    cat > /root/lmcache_full.yaml <<EOF
chunk_size: 256
local_cpu: true
max_local_cpu_size: 60
nixl_buffer_device: "cpu"
enable_controller: true
lmcache_instance_id: "demo"
controller_pull_url: "localhost:8300"
controller_reply_url: "localhost:8400"
lmcache_worker_ports: [8500]
extra_config:
  enable_nixl_storage: true
  nixl_backend: "POSIX"
  nixl_pool_size: 1700
  nixl_path: "/root/lmcache_nixl/"
EOF
    LMCACHE_CONFIG_FILE=/root/lmcache_full.yaml \
    exec vllm serve "${COMMON_ARGS[@]}" \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
      >"$LOG" 2>&1
    ;;
  nixl)
    # KV blocks move GPU -> NIXL POSIX plugin -> NVMe file pool.
    # Small CPU tier only as staging; the persistent tier is the NIXL pool.
    mkdir -p /root/lmcache_nixl
    cat > /root/lmcache_nixl.yaml <<EOF
chunk_size: 256
local_cpu: false
max_local_cpu_size: 10
nixl_buffer_device: "cpu"
enable_controller: true
lmcache_instance_id: "demo"
controller_pull_url: "localhost:8300"
controller_reply_url: "localhost:8400"
lmcache_worker_ports: [8500]
extra_config:
  enable_nixl_storage: true
  nixl_backend: "POSIX"
  # pool size is a DESCRIPTOR COUNT (one ~48MB chunk file per slot), not bytes
  nixl_pool_size: 1500
  nixl_path: "/root/lmcache_nixl/"
EOF
    LMCACHE_CONFIG_FILE=/root/lmcache_nixl.yaml \
    exec vllm serve "${COMMON_ARGS[@]}" \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
      >"$LOG" 2>&1
    ;;
  *)
    echo "unknown mode: $MODE" >&2; exit 1;;
esac
