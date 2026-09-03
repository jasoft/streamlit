#!/bin/bash
# 本地开发启动脚本：一键清理孤儿进程 + 启动 fdata(:9701) + FastAPI(:8000) + Next.js(:3001)
# 用法: ./dev.sh   |   停止: Ctrl-C
# 可选: NO_FDATA=1 ./dev.sh   跳过启动 fdata（仍会清理端口）
set -e
cd "$(dirname "$0")"   # 切到项目根目录，保证路径正确

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"
FDATA_PORT="${FDATA_PORT:-9701}"
FDATA_PID=""
UVICORN_PID=""
_CLEANUP_DONE=0

# --------------------------
# 端口清理：精准 lsof → PID → kill（TERM 优先，残留再 KILL）
# 不使用 pkill -f 避免误杀同关键字的无关进程
# --------------------------
kill_port() {
  local port="$1"
  local pids
  pids=$(lsof -iTCP:"$port" -sTCP:LISTEN -P -t 2>/dev/null || true)
  if [ -z "$pids" ]; then
    return 0
  fi
  echo ">>> 清理端口 $port 占用进程 (PID: $pids)"
  kill $pids 2>/dev/null || true
  # 等进程退出，最多等 2s
  local waited=0
  while [ $waited -lt 20 ]; do
    local still
    still=$(lsof -iTCP:"$port" -sTCP:LISTEN -P -t 2>/dev/null || true)
    [ -z "$still" ] && return 0
    sleep 0.1
    waited=$((waited + 1))
  done
  # 还没退出，强制 KILL
  local remain
  remain=$(lsof -iTCP:"$port" -sTCP:LISTEN -P -t 2>/dev/null || true)
  if [ -n "$remain" ]; then
    echo ">>> 强制终止端口 $port 进程 (PID: $remain)"
    kill -9 $remain 2>/dev/null || true
  fi
}

# Ctrl-C / 退出时同时停掉 fdata/后端/前端进程（防重入：INT/TERM 触发 exit→EXIT 会二次进入）
cleanup() {
  [ "$_CLEANUP_DONE" = "1" ] && return
  _CLEANUP_DONE=1
  set +e
  [ -n "$FDATA_PID" ]    && { pkill -P "$FDATA_PID" 2>/dev/null || true; kill "$FDATA_PID" 2>/dev/null || true; }
  [ -n "$UVICORN_PID" ]  && { pkill -P "$UVICORN_PID" 2>/dev/null || true; kill "$UVICORN_PID" 2>/dev/null || true; }
  # 停掉进程组里所有子进程（npm/node 多进程模型）
  kill 0 2>/dev/null || true
  # 收尾再清理一次端口和孤儿进程，防止还有残留
  kill_port "$BACKEND_PORT"
  kill_port "$FRONTEND_PORT"
  if [ -z "$NO_FDATA" ]; then
    kill_port "$FDATA_PORT"
  fi
  pkill -f "uvicorn backend.main:app" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM EXIT

# ================
# 启动前：清理所有历史孤儿进程
# ================
echo "=============================================="
echo ">>> [前置清理] 释放端口 $FDATA_PORT (fdata) / $BACKEND_PORT (后端) / $FRONTEND_PORT (前端)"
kill_port "$FDATA_PORT"
kill_port "$BACKEND_PORT"
kill_port "$FRONTEND_PORT"
# 彻底清理可能残留的孤儿 worker (不监听端口的 multiprocessing.spawn 子进程)
pkill -f "uvicorn backend.main:app" 2>/dev/null || true
echo ">>> [前置清理] 完成"
echo "=============================================="

# ================
# 1) fdata 数据网关（默认启动，NO_FDATA=1 时跳过）
# ================
if [ -z "$NO_FDATA" ]; then
  echo ">>> 启动 fdata 数据网关 (端口 $FDATA_PORT)"
  uv run python scripts/fdata.py serve --port "$FDATA_PORT" &
  FDATA_PID=$!
  # 给 fdata 一点时间启动，避免后端刚起来时立刻回退到 CLI
  sleep 1
fi

# ================
# 2) FastAPI 后端
# ================
echo ">>> 启动后端 FastAPI (端口 $BACKEND_PORT)"
uv run uvicorn backend.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload &
UVICORN_PID=$!

# ================
# 3) Next.js 前端
# ================
echo ">>> 启动前端 Next.js (端口 $FRONTEND_PORT)"
cd frontend
PORT="$FRONTEND_PORT" npm run dev &
cd ..

wait
