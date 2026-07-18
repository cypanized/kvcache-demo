#!/bin/bash
# Runs ON the GPU instance. Idempotent: safe to re-run on a prepared box.
# Expects these files already in /root (pushed by deploy.sh):
#   run_server.sh switch.sh ask.py gen_docs.py spill_test.py
#   evidence_lookup.py onbox_server.py index.html
set -uo pipefail
cd /root
echo "== bootstrap $(date) =="

echo "-- NIXL into the serving venv"
python3 -c "import nixl" 2>/dev/null || uv pip install --python /opt/venv/bin/python3 nixl

echo "-- model download (skips if cached)"
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen2.5-14B-Instruct')"

echo "-- 40-doc corpus"
python3 -c "import json; assert len(json.load(open('docs.json'))) >= 40" 2>/dev/null \
  || python3 gen_docs.py 40 9000

echo "-- LMCache 0.5.1 patch: add clear() to the NIXL backend (upstream lacks it)"
python3 - <<'PYEOF'
path = "/opt/venv/lib/python3.12/site-packages/lmcache/v1/storage_backend/nixl_storage_backend.py"
src = open(path).read()
if "def clear(self) -> int:" in src:
    print("   already patched")
else:
    anchor = "    async def mem_to_storage("
    patch = """    def clear(self) -> int:
        \"\"\"Demo patch: pure eviction for the NIXL tier (no upstream clear()).\"\"\"
        with self.key_lock:
            num_chunks = len(self.key_dict)
            for meta in self.key_dict.values():
                try:
                    self.pool.push(meta.index)
                except Exception:
                    pass
            self.key_dict.clear()
        return num_chunks * 256

"""
    assert anchor in src, "patch anchor not found — lmcache version changed?"
    open(path, "w").write(src.replace(anchor, patch + anchor, 1))
    print("   patched")
PYEOF

echo "-- helper launchers"
cat > /root/start_controller.sh <<'EOF'
#!/bin/bash
export PYTHONHASHSEED=0
pkill -f "lmcache.v1.api_serve[r]" 2>/dev/null
sleep 1
cd /root && exec python3 -m lmcache.v1.api_server --host 127.0.0.1 --port 9050 \
  --monitor-ports '{"pull": 8300, "reply": 8400}' > /root/controller.log 2>&1
EOF
cat > /root/restart_ui.sh <<'EOF'
#!/bin/bash
pkill -f "onbox_serve[r].py" 2>/dev/null
sleep 2
cd /root && exec python3 onbox_server.py > /root/onbox_server.log 2>&1
EOF
chmod +x /root/*.sh

echo "-- starting controller + dashboard (detached)"
setsid nohup bash /root/start_controller.sh > /dev/null 2>&1 < /dev/null &
setsid nohup bash /root/restart_ui.sh > /dev/null 2>&1 < /dev/null &
sleep 2

echo "-- launching vLLM in full-hierarchy mode (detached; ~3 min load)"
setsid nohup /root/switch.sh full > /dev/null 2>&1 < /dev/null &

echo "== bootstrap dispatched OK =="
