#!/bin/bash
# 本地开发启动脚本：一键清理孤儿进程 + 启动 fdata(:9701) + FastAPI(:8000) + Next.js(:3001)
# 用法: ./dev.sh   |   停止: Ctrl-C
# 可选: NO_FDATA=1 ./dev.sh   跳过启动 fdata（仍会清理端口）
# 多 worktree 隔离: 存在 .env.worktree 时加载其中的 BACKEND_PORT/FRONTEND_PORT/FDATA_PORT,
# 且 fdata 若已在监听则直接复用不重启 (清理/退出都不会杀共享 fdata), 清理动作按端口收窄,
# 不会误杀其他 worktree 的后端/前端。
set -e
cd "$(dirname "$0")"   # 切到项目根目录，保证路径正确

# worktree 本地端口覆盖 (gitignore, 不入库); 存在则优先生效
if [ -f .env.worktree ]; then
  # shellcheck disable=SC1091
  set -a; source .env.worktree; set +a
fi

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"
FDATA_PORT="${FDATA_PORT:-9701}"
export BACKEND_PORT FRONTEND_PORT FDATA_PORT   # next.config.ts 读 BACKEND_PORT 配 rewrite
FDATA_PID=""
FDATA_OWNED=0
UVICORN_PID=""
_CLEANUP_DONE=0

fdata_alive() { nc -z 127.0.0.1 "$FDATA_PORT" >/dev/null 2>&1; }

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
  # 只回收自己启动的 fdata; 复用的共享 fdata (其他 worktree/主栈在用) 不动
  if [ "$FDATA_OWNED" = "1" ]; then
    kill_port "$FDATA_PORT"
  fi
  # 按端口精确匹配, 只清自己的 uvicorn (不误杀其他 worktree 的 backend.main:app)
  pkill -f "uvicorn backend.main:app.*--port ${BACKEND_PORT}" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM EXIT

# ================
# 启动前：清理所有历史孤儿进程
# ================
echo "=============================================="
echo ">>> [前置清理] 释放端口 $FDATA_PORT (fdata) / $BACKEND_PORT (后端) / $FRONTEND_PORT (前端)"
# 共享 fdata 已在监听 -> 复用, 不清理; 否则清掉残留再启动自己的
if fdata_alive; then
  echo ">>> fdata 已在端口 $FDATA_PORT 运行, 直接复用 (不重启)"
else
  kill_port "$FDATA_PORT"
fi
kill_port "$BACKEND_PORT"
kill_port "$FRONTEND_PORT"
# 彻底清理可能残留的孤儿 worker (按端口匹配, 不影响其他 worktree)
pkill -f "uvicorn backend.main:app.*--port ${BACKEND_PORT}" 2>/dev/null || true
echo ">>> [前置清理] 完成"
echo "=============================================="

# ================
# 1) fdata 数据网关（默认启动; 已有共享实例则复用; NO_FDATA=1 时跳过）
# ================
if [ -z "$NO_FDATA" ]; then
  if fdata_alive; then
    echo ">>> 复用已有 fdata 数据网关 (端口 $FDATA_PORT)"
  else
    echo ">>> 启动 fdata 数据网关 (端口 $FDATA_PORT)"
    uv run python trading/fdata.py serve --port "$FDATA_PORT" &
    FDATA_PID=$!
    FDATA_OWNED=1
    # 给 fdata 一点时间启动，避免后端刚起来时立刻回退到 CLI
    sleep 1
  fi
fi

# ================
# 2) FastAPI 后端
# ================
echo ">>> 启动后端 FastAPI (端口 $BACKEND_PORT)"
uv run uvicorn backend.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload --reload-dir backend --reload-dir strategy &
UVICORN_PID=$!

# ================
# 3) Next.js 前端
# ================
echo ">>> 启动前端 Next.js (端口 $FRONTEND_PORT)"
cd frontend
PORT="$FRONTEND_PORT" npm run dev &
cd ..

wait
