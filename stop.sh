#!/usr/bin/env bash
# 停止占用端口的服务（macOS / Linux）
set -uo pipefail
PORT="${PORT:-8787}"

PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
if [ -z "$PIDS" ]; then
  echo "端口 ${PORT} 上没有监听进程"
  exit 0
fi

echo "停止进程: $PIDS"
kill $PIDS 2>/dev/null || true
sleep 1
PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo "强制结束: $PIDS"
  kill -9 $PIDS 2>/dev/null || true
fi
echo "已停止"
