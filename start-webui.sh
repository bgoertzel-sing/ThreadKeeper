#!/bin/bash
# Captain's launcher for OmegaClaw webui.
# IMPORTANT: must use the venv's python so lib_llm_ext can import openai/anthropic.
# The CAPTAIN-PATCH in webui.py routes /send through lib_llm_ext directly when
# no swipl runtime is present.
set -e
cd "$(dirname "$0")"
PORT=22333

# Kill any existing webui on PORT (system or venv).
EXISTING=$(ss -ltnp 2>/dev/null | awk -v p="$PORT" '$0 ~ ":"p" "{ split($6,a,"pid="); split(a[2],b,","); print b[1] }')
if [ -n "$EXISTING" ]; then
  echo "Stopping existing webui on :$PORT (pid=$EXISTING)..."
  kill "$EXISTING" 2>/dev/null || true
  sleep 1
fi

setsid nohup ./venv/bin/python3 webui.py > /tmp/oma-webui.log 2>&1 < /dev/null &
sleep 2
echo "webui launched (venv python) — log: /tmp/oma-webui.log"
ss -ltnp 2>/dev/null | grep ":$PORT" || { echo "FAILED to bind :$PORT"; tail -20 /tmp/oma-webui.log; exit 1; }
