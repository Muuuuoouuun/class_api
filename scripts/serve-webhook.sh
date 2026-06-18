#!/usr/bin/env bash
# ClassIn Webhook 수신기 기동 (FastAPI/uvicorn). config.yaml 의 webhook.host/port 사용.
# 기본 포트 8787. 종료: Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x ".venv/bin/classin-webhook" ]; then
  exec .venv/bin/classin-webhook
fi
exec classin-webhook
