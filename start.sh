#!/usr/bin/env bash
# 医疗智能导诊 Agent — macOS / Linux 启动脚本
set -euo pipefail
cd "$(dirname "$0")"

PY="python3"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"

echo "[guide-agent] 服务地址 http://${HOST}:${PORT}/kiosk/"
echo "  kiosk  : http://${HOST}:${PORT}/kiosk/"
echo "  admin  : http://${HOST}:${PORT}/admin/   (admin / admin123)"
echo "  doctor : http://${HOST}:${PORT}/doctor/"
echo "  health : http://${HOST}:${PORT}/healthz"
echo ""

exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
