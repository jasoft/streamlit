#!/bin/bash
# 本地开发启动脚本: 一键清理孤儿进程 + 启动 fdata + FastAPI + Next.js
#
# 常规用法:
#   ./dev.sh                     主工作树前台启动: fdata(:9701) + 后端(:8000) + 前端(:3001), Ctrl-C 全停
#   NO_FDATA=1 ./dev.sh          跳过 fdata (后端自动回退 CLI 模式)
#
# worktree 多开 (多 worktree 并行开发, 各自随机端口互不冲突):
#   ./dev.sh worktree new <name> [branch] [base]
#                                创建 ../streamlit-<name> + 装依赖(uv sync/npm ci) + 后台启动(随机端口)
#   ./dev.sh worktree up [name]  后台启动已有 worktree 全套服务 (name 缺省 = 当前所在 worktree)
#   ./dev.sh worktree down [name] 停止该 worktree 全套服务
#   ./dev.sh worktree ls         查看所有注册栈 (含 已合并/已删除 标记)
#   ./dev.sh worktree clean [name|-a]
#                                清理"已删除/已合并回主干"worktree 的残留进程与注册信息
#   ./dev.sh stop-all            关闭本系统全部服务 (主树 / 任意 worktree, 不限端口)
#
# 生命周期约定: worktree 被 rm / git worktree remove, 或其分支合并回主干后,
# 下次任意一处运行 ./dev.sh 时会自动 kill 该 worktree 的残留服务并回收注册信息,
# 不会留下占用端口的僵尸进程。
#
# 通用环境变量:
#   NO_FDATA=1                              不启动 fdata
#   BACKEND_PORT/FRONTEND_PORT/FDATA_PORT   手动指定端口 (worktree 模式缺省随机分配)
#   SKIP_SETUP=1                            worktree new 跳过 uv sync / npm ci
#   MAIN_BRANCH=main                        "已合并"判定的主干分支名
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# 用户显式指定的端口 (可为空); worktree 模式下未指定的走随机分配
USER_BACKEND_PORT="${BACKEND_PORT:-}"
USER_FRONTEND_PORT="${FRONTEND_PORT:-}"
USER_FDATA_PORT="${FDATA_PORT:-}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"
FDATA_PORT="${FDATA_PORT:-9701}"
MAIN_BRANCH="${MAIN_BRANCH:-main}"
NO_FDATA="${NO_FDATA:-}"
SKIP_SETUP="${SKIP_SETUP:-}"

GIT_COMMON="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
GIT_LOCAL="$(cd "$(git rev-parse --git-dir)" && pwd)"
# linked worktree 的 .git 是一个文件，主树是目录 —— 用这个区分
if [ -f "$ROOT/.git" ]; then
  IS_MAIN_TREE=0
elif [ "$GIT_COMMON" = "$GIT_LOCAL" ]; then
  IS_MAIN_TREE=1
else
  IS_MAIN_TREE=0
fi
# 注册表放主 .git 下: 所有 worktree 共享，且 worktree 被删后仍能查到该回收哪些端口
REG_DIR="$GIT_COMMON/dev-worktrees"

FDATA_PID=""
FDATA_OWNED=0
UVICORN_PID=""
FRONTEND_PID=""
_CLEANUP_DONE=0
_REG_NAME=""

die() {
  echo "错误: $*" >&2
  exit 1
}

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
    if [ -z "$still" ]; then
      return 0
    fi
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

port_listeners() {
  lsof -iTCP:"$1" -sTCP:LISTEN -P -t 2>/dev/null || true
}

port_busy() {
  [ -n "$(port_listeners "$1")" ]
}

# 端口是否已被其他注册 worktree 声明 (防止并发启动时撞车)
port_claimed() {
  local p="$1" f
  if [ ! -d "$REG_DIR" ]; then
    return 1
  fi
  for f in "$REG_DIR"/*.env; do
    if [ -f "$f" ] && grep -qxE "(BACKEND_PORT|FRONTEND_PORT|FDATA_PORT)=$p" "$f"; then
      return 0
    fi
  done
  return 1
}

# 在 [base, base+span) 内随机挑一个空闲且未被声明的端口
rand_free_port() {
  local base="$1" span="$2" i p
  for i in $(seq 1 200); do
    p=$((base + RANDOM % span))
    if ! port_busy "$p" && ! port_claimed "$p"; then
      echo "$p"
      return 0
    fi
  done
  die "随机端口分配失败: $base+ 段没有可用端口"
}

# --------------------------
# 进程清理: 本项目的三种服务进程, 按 cwd 精准归属到某个 worktree
# (cmdline 全项目同名, 只有 cwd 能区分是哪棵树启动的, 避免 pkill -f 误杀其他 worktree)
# --------------------------
pgrep_stack_pids() {
  pgrep -f "uvicorn backend[.]main:app|trading/fdata[.]py serve|next dev|next-server" 2>/dev/null || true
}

proc_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

# 杀掉 cwd 位于 $1 目录树下的服务进程, $2=TERM/KILL
kill_cwd_procs() {
  local root="$1" sig="$2" pid cwd
  for pid in $(pgrep_stack_pids); do
    if [ "$pid" != "$$" ]; then
      cwd="$(proc_cwd "$pid")"
      case "$cwd" in
        "$root"|"$root"/*) kill -"$sig" "$pid" 2>/dev/null || true ;;
      esac
    fi
  done
  return 0
}

# 某个 worktree 栈仍存活的进程 (cwd 命中 + 端口监听者), 空格分隔
survivors() {
  local path="$1"
  shift
  local out="" pid cwd p
  for pid in $(pgrep_stack_pids); do
    if [ "$pid" != "$$" ]; then
      cwd="$(proc_cwd "$pid")"
      case "$cwd" in
        "$path"|"$path"/*) out="$out $pid" ;;
      esac
    fi
  done
  for p in "$@"; do
    if [ -n "$p" ]; then
      out="$out $(port_listeners "$p")"
    fi
  done
  echo "$out"
}

# --------------------------
# 注册表: $REG_DIR/<worktree目录名>.env, KEY=VALUE 格式
# --------------------------
reg_load() {
  local f="$REG_DIR/$1.env" line k v
  if [ ! -f "$f" ]; then
    return 1
  fi
  E_PATH=""; E_BRANCH=""; E_BASE=""
  E_BACKEND_PORT=""; E_FRONTEND_PORT=""; E_FDATA_PORT=""
  E_FDATA_PID=""; E_UVICORN_PID=""; E_FRONTEND_PID=""; E_STARTED_AT=""
  while IFS= read -r line; do
    case "$line" in
      [A-Z_]*=*)
        k="${line%%=*}"
        v="${line#*=}"
        eval "E_$k=\"\$v\""
        ;;
    esac
  done < "$f"
  return 0
}

reg_write() { # $1=key $2=path $3=branch $4=base
  mkdir -p "$REG_DIR"
  cat > "$REG_DIR/$1.env" <<EOF
# dev.sh worktree 注册信息 (自动生成, 请勿手改)
PATH=$2
BRANCH=$3
BASE=$4
BACKEND_PORT=$BACKEND_PORT
FRONTEND_PORT=$FRONTEND_PORT
FDATA_PORT=$FDATA_PORT
FDATA_PID=$FDATA_PID
UVICORN_PID=$UVICORN_PID
FRONTEND_PID=$FRONTEND_PID
STARTED_AT=$(date '+%F %T')
EOF
}

reg_rm() {
  rm -f "$REG_DIR/$1.env"
}

wt_in_git_list() {
  git worktree list --porcelain 2>/dev/null | grep -qx "worktree $1"
}

# 分支已合并回主干: tip 是 main 祖先且 tip != main tip
# (排除"刚从 main 建出来还没提交"的分支被误判成已合并)
wt_merged() {
  local b="$1"
  if [ -z "$b" ] || [ "$b" = "$MAIN_BRANCH" ]; then
    return 1
  fi
  git show-ref --verify -q "refs/heads/$b" || return 1
  git merge-base --is-ancestor "$b" "$MAIN_BRANCH" || return 1
  if [ "$(git rev-parse "$b")" = "$(git rev-parse "$MAIN_BRANCH")" ]; then
    return 1
  fi
  return 0
}

entry_status() { # 需先 reg_load; 输出 deleted/merged/running/stopped
  if [ ! -e "$E_PATH/.git" ] || ! wt_in_git_list "$E_PATH"; then
    echo "deleted"
    return 0
  fi
  if wt_merged "$E_BRANCH"; then
    echo "merged"
    return 0
  fi
  if [ -n "$(survivors "$E_PATH" "$E_BACKEND_PORT" "$E_FRONTEND_PORT" "$E_FDATA_PORT")" ]; then
    echo "running"
    return 0
  fi
  echo "stopped"
}

# --------------------------
# 栈停止: 记录 PID → 端口监听者 → cwd 兜底, TERM 后升级 KILL
# --------------------------
stop_stack() { # $1=path $2=后端端口 $3=前端端口 $4=fdata端口 (均可为空)
  local path="$1" b="$2" f="$3" d="$4" pid
  # 注册的 PID 连同直接子进程 (reload worker / npm 子进程) 一起 TERM
  for pid in "$E_UVICORN_PID" "$E_FRONTEND_PID" "$E_FDATA_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
    fi
  done
  # 端口监听者 + cwd 命中的进程 TERM 兜底
  for pid in $(survivors "$path" "$b" "$f" "$d"); do
    kill "$pid" 2>/dev/null || true
  done
  sleep 1.5
  # 仍存活则 KILL, 最后端口级清扫
  for pid in $(survivors "$path" "$b" "$f" "$d"); do
    kill -9 "$pid" 2>/dev/null || true
  done
  if [ -n "$b" ]; then kill_port "$b" || true; fi
  if [ -n "$f" ]; then kill_port "$f" || true; fi
  if [ -n "$d" ]; then kill_port "$d" || true; fi
  return 0
}

# 自动回收已删除 / 已合并 worktree 的资源 (每次 ./dev.sh 运行都先扫一遍)
auto_clean() {
  if [ ! -d "$REG_DIR" ]; then
    return 0
  fi
  local f name status
  for f in "$REG_DIR"/*.env; do
    if [ ! -f "$f" ]; then
      continue
    fi
    name="$(basename "$f" .env)"
    if ! reg_load "$name"; then
      rm -f "$f"
      continue
    fi
    status="$(entry_status)"
    if [ "$status" = "deleted" ]; then
      echo ">>> [自动清理] worktree '$name' 目录已删除 → 回收残留服务与注册信息"
      stop_stack "$E_PATH" "$E_BACKEND_PORT" "$E_FRONTEND_PORT" "$E_FDATA_PORT"
      rm -f "$f"
      git worktree prune >/dev/null 2>&1 || true
    elif [ "$status" = "merged" ]; then
      echo ">>> [自动清理] worktree '$name' 分支 '$E_BRANCH' 已合并回 $MAIN_BRANCH → 回收残留服务与注册信息"
      stop_stack "$E_PATH" "$E_BACKEND_PORT" "$E_FRONTEND_PORT" "$E_FDATA_PORT"
      rm -f "$f"
    fi
  done
  return 0
}

# --------------------------
# 栈启动: 在 worktree 内后台拉起 fdata / 后端 / 前端, 日志落 .dev-logs/
# --------------------------
start_stack() { # $1=worktree 根目录; 使用全局 BACKEND_PORT/FRONTEND_PORT/FDATA_PORT
  local wt="$1"
  local logdir="$wt/.dev-logs"
  mkdir -p "$logdir"
  if [ -z "$NO_FDATA" ]; then
    echo ">>> 启动 fdata 数据网关 (端口 $FDATA_PORT)"
    ( cd "$wt" && exec nohup uv run python trading/fdata.py serve --port "$FDATA_PORT" ) >>"$logdir/fdata.log" 2>&1 </dev/null &
    FDATA_PID=$!
    # 给 fdata 一点时间启动，避免后端刚起来时立刻回退到 CLI
    sleep 1
  fi
  echo ">>> 启动后端 FastAPI (端口 $BACKEND_PORT)"
  ( cd "$wt" && exec nohup env FDATA_PORT="$FDATA_PORT" uv run uvicorn backend.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload --reload-dir backend --reload-dir strategy --reload-dir trading ) >>"$logdir/backend.log" 2>&1 </dev/null &
  UVICORN_PID=$!
  echo ">>> 启动前端 Next.js (端口 $FRONTEND_PORT)"
  ( cd "$wt/frontend" && exec nohup env PORT="$FRONTEND_PORT" BACKEND_ORIGIN="http://localhost:$BACKEND_PORT" npm run dev ) >>"$logdir/frontend.log" 2>&1 </dev/null &
  FRONTEND_PID=$!
}

print_stack_summary() { # $1=key $2=path
  echo "=============================================="
  echo ">>> worktree 栈 '$1' 已拉起"
  echo "    前端  http://localhost:$FRONTEND_PORT"
  echo "    后端  http://localhost:$BACKEND_PORT  (Swagger: /docs)"
  if [ -z "$NO_FDATA" ]; then
    echo "    fdata http://localhost:$FDATA_PORT"
  else
    echo "    fdata 未启动 (NO_FDATA=1, 后端走 CLI 回退)"
  fi
  echo "    日志  $2/.dev-logs/"
  echo "    停止  ./dev.sh worktree down $1"
  echo "=============================================="
}

wait_and_report() { # $1=key $2=path
  local key="$1" path="$2" i be_ok=0 fe_ok=0
  for i in $(seq 1 30); do
    if port_busy "$BACKEND_PORT"; then be_ok=1; break; fi
    sleep 0.5
  done
  for i in $(seq 1 30); do
    if port_busy "$FRONTEND_PORT"; then fe_ok=1; break; fi
    sleep 0.5
  done
  print_stack_summary "$key" "$path"
  if [ "$be_ok" = "1" ]; then
    echo ">>> 后端已就绪"
  else
    echo ">>> 后端仍在启动 (首次 uv 解析环境可能需 1-2 分钟): tail -f $path/.dev-logs/backend.log"
  fi
  if [ "$fe_ok" = "1" ]; then
    echo ">>> 前端已就绪"
  else
    echo ">>> 前端仍在启动: tail -f $path/.dev-logs/frontend.log"
  fi
}

# worktree 模式端口: 用户指定的必须空闲, 未指定的随机分配
pick_worktree_ports() {
  if [ -n "$USER_BACKEND_PORT" ]; then
    BACKEND_PORT="$USER_BACKEND_PORT"
    if port_busy "$BACKEND_PORT"; then die "BACKEND_PORT=$BACKEND_PORT 已被占用, 换一个或取消指定"; fi
  else
    BACKEND_PORT="$(rand_free_port 20000 900)"
  fi
  if [ -n "$USER_FRONTEND_PORT" ]; then
    FRONTEND_PORT="$USER_FRONTEND_PORT"
    if port_busy "$FRONTEND_PORT"; then die "FRONTEND_PORT=$FRONTEND_PORT 已被占用, 换一个或取消指定"; fi
  else
    FRONTEND_PORT="$(rand_free_port 21000 900)"
  fi
  if [ -n "$USER_FDATA_PORT" ]; then
    FDATA_PORT="$USER_FDATA_PORT"
    if [ -z "$NO_FDATA" ] && port_busy "$FDATA_PORT"; then die "FDATA_PORT=$FDATA_PORT 已被占用, 换一个或取消指定"; fi
  elif [ -z "$NO_FDATA" ]; then
    FDATA_PORT="$(rand_free_port 22000 500)"
  else
    FDATA_PORT="9701" # NO_FDATA 时后端尝试连主树共享的 fdata
  fi
}

# --------------------------
# 前台模式 (Ctrl-C 全停): 主树固定端口; worktree 内随机端口并注册
# --------------------------
cleanup() {
  if [ "$_CLEANUP_DONE" = "1" ]; then
    return
  fi
  _CLEANUP_DONE=1
  set +e
  for pid in "$FDATA_PID" "$UVICORN_PID" "$FRONTEND_PID"; do
    if [ -n "$pid" ]; then
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
    fi
  done
  # 不用 kill 0 (会误伤同进程组的无关任务), 改为端口 + cwd 精准清扫
  kill_port "$BACKEND_PORT"
  kill_port "$FRONTEND_PORT"
  # 只回收自己启动的 fdata; 复用的共享 fdata (其他 worktree/主栈在用) 不动
  if [ "$FDATA_OWNED" = "1" ]; then
    kill_port "$FDATA_PORT"
  fi
  kill_cwd_procs "$ROOT" TERM
  sleep 0.5
  kill_cwd_procs "$ROOT" KILL
  if [ -n "$_REG_NAME" ]; then
    reg_rm "$_REG_NAME"
  fi
  exit 0
}

mode_main() {
  auto_clean
  echo "=============================================="
  if [ "$IS_MAIN_TREE" = "1" ]; then
    echo ">>> [前置清理] 释放端口 $FDATA_PORT (fdata) / $BACKEND_PORT (后端) / $FRONTEND_PORT (前端)"
    kill_port "$FDATA_PORT"
    kill_port "$BACKEND_PORT"
    kill_port "$FRONTEND_PORT"
  else
    echo ">>> [worktree 模式] 检测到在 linked worktree 内启动, 使用随机端口并注册"
    local key
    key="$(basename "$(git rev-parse --show-toplevel)")"
    if reg_load "$key"; then
      echo ">>> 发现 '$key' 的历史运行记录, 先回收旧进程/端口"
      stop_stack "$E_PATH" "$E_BACKEND_PORT" "$E_FRONTEND_PORT" "$E_FDATA_PORT"
      reg_rm "$key"
    fi
    pick_worktree_ports
    _REG_NAME="$key"
  fi
  kill_cwd_procs "$ROOT" TERM
  sleep 0.5
  kill_cwd_procs "$ROOT" KILL
  echo ">>> [前置清理] 完成"
  echo "=============================================="
  trap cleanup INT TERM EXIT
  start_stack "$ROOT"
  if [ -n "$_REG_NAME" ]; then
    reg_write "$_REG_NAME" "$ROOT" "$(git branch --show-current)" ""
    print_stack_summary "$_REG_NAME" "$ROOT"
  fi
  echo ">>> 全部服务已拉起, Ctrl-C 停止"
  wait
}

# --------------------------
# worktree 子命令
# --------------------------
usage_wt() {
  cat <<'EOF'
worktree 子命令:
  ./dev.sh worktree new <name> [branch] [base]   创建 ../streamlit-<name> + 装依赖 + 后台启动
  ./dev.sh worktree up [name]                    后台启动 (name 缺省 = 当前 worktree)
  ./dev.sh worktree down [name]                  停止
  ./dev.sh worktree ls                           查看注册栈
  ./dev.sh worktree clean [name|-a]              回收已删除/已合并栈的资源
  ./dev.sh stop-all                              关闭本系统全部服务
EOF
}

cmd_wt_new() {
  local name="${1:-}" branch base path key parent
  if [ -z "$name" ]; then
    usage_wt
    die "缺少 <name>"
  fi
  case "$name" in
    *[!A-Za-z0-9._-]*) die "name 只能包含字母/数字/./_/-: $name" ;;
  esac
  branch="${2:-$name}"
  base="${3:-$MAIN_BRANCH}"
  parent="$(dirname "$ROOT")"
  path="$parent/streamlit-$name"
  key="streamlit-$name"
  git show-ref --verify -q "refs/heads/$base" || die "base 分支不存在: $base"
  auto_clean
  if [ -e "$path" ]; then
    if ! wt_in_git_list "$path"; then
      die "$path 已存在且不是 git worktree, 换个名字或先手动处理"
    fi
    echo ">>> worktree 已存在, 复用: $path"
  elif git show-ref --verify -q "refs/heads/$branch"; then
    echo ">>> 挂载已有分支 $branch → $path"
    git worktree add "$path" "$branch" || die "git worktree add 失败"
  else
    echo ">>> 创建 worktree: $path (新分支 $branch 基于 $base)"
    git worktree add -b "$branch" "$path" "$base" || die "git worktree add 失败"
  fi
  # .env / dev.sh 等被 gitignore 或需同步的文件从当前树补齐
  if [ -f "$ROOT/.env" ] && [ ! -f "$path/.env" ]; then
    cp "$ROOT/.env" "$path/.env"
    echo ">>> 已复制 .env → 新 worktree"
  fi
  if [ -f "$ROOT/dev.sh" ] && [ -f "$path/dev.sh" ]; then
    cp "$ROOT/dev.sh" "$path/dev.sh"
    echo ">>> 已同步最新 dev.sh → 新 worktree"
  fi
  if [ -z "$SKIP_SETUP" ]; then
    echo ">>> 安装 Python 依赖 (uv sync, 首次可能 1-2 分钟)..."
    ( cd "$path" && uv sync ) || die "uv sync 失败"
    echo ">>> 安装前端依赖 (npm ci)..."
    ( cd "$path/frontend" && npm ci ) || die "npm ci 失败"
  else
    echo ">>> SKIP_SETUP=1, 跳过依赖安装"
  fi
  cmd_wt_up "$key"
}

cmd_wt_up() {
  local key="${1:-}" path b f d
  if [ -z "$key" ]; then
    if [ "$IS_MAIN_TREE" = "1" ]; then
      usage_wt
      die "在主工作树需指定: ./dev.sh worktree up <name>"
    fi
    key="$(basename "$(git rev-parse --show-toplevel)")"
  fi
  auto_clean
  if reg_load "$key"; then
    path="$E_PATH"; b="$E_BACKEND_PORT"; f="$E_FRONTEND_PORT"; d="$E_FDATA_PORT"
  else
    path="$(dirname "$ROOT")/$key"
    if [ ! -e "$path/.git" ]; then
      die "未找到 worktree '$key' (注册表无记录, 且 $path 不是 git worktree)"
    fi
    b=""; f=""; d=""
  fi
  if [ ! -d "$path/frontend" ]; then
    die "$path/frontend 不存在, 先在主树提交前端代码"
  fi
  if [ -n "$(survivors "$path" "$b" "$f" "$d")" ]; then
    die "worktree '$key' 似乎已在运行; 如需重启先执行 ./dev.sh worktree down $key"
  fi
  reg_rm "$key" # 清掉旧注册 (含旧端口声明), 重新分配
  pick_worktree_ports
  start_stack "$path"
  reg_write "$key" "$path" "$(git -C "$path" branch --show-current)" ""
  wait_and_report "$key" "$path"
}

cmd_wt_down() {
  local key="${1:-}" path b f d
  if [ -z "$key" ]; then
    if [ "$IS_MAIN_TREE" = "1" ]; then
      usage_wt
      die "在主工作树需指定: ./dev.sh worktree down <name>"
    fi
    key="$(basename "$(git rev-parse --show-toplevel)")"
  fi
  auto_clean
  if reg_load "$key"; then
    path="$E_PATH"; b="$E_BACKEND_PORT"; f="$E_FRONTEND_PORT"; d="$E_FDATA_PORT"
  else
    path="$(dirname "$ROOT")/$key"
    if [ ! -e "$path/.git" ]; then
      die "未找到 worktree '$key' 的运行记录或目录"
    fi
    b=""; f=""; d=""
  fi
  echo ">>> 停止 worktree '$key' ($path) 全套服务..."
  stop_stack "$path" "$b" "$f" "$d"
  reg_rm "$key"
  echo ">>> 已停止并注销"
}

cmd_wt_ls() {
  echo "WORKTREE                      BRANCH                 STATUS          PORTS(后端/前端/fdata)"
  echo "----------------------------  ---------------------  --------------  ---------------------"
  local f name status n=0
  for f in "$REG_DIR"/*.env; do
    if [ -f "$f" ]; then
      name="$(basename "$f" .env)"
      if ! reg_load "$name"; then
        continue
      fi
      status="$(entry_status)"
      case "$status" in
        running)  status="● 运行中" ;;
        stopped)  status="○ 已停止" ;;
        merged)   status="⚠ 已合并" ;;
        deleted)  status="✗ 已删除" ;;
      esac
      echo "$(printf '%-28s %-21s %-14s %s' "$name" "${E_BRANCH:-?}" "$status" "$E_BACKEND_PORT/$E_FRONTEND_PORT/$E_FDATA_PORT")"
      n=$((n + 1))
    fi
  done
  if [ "$n" = "0" ]; then
    echo "(无注册的 worktree 栈)"
  fi
  echo
  echo "回收已删除/已合并栈: ./dev.sh worktree clean    连运行中的全部停掉: ./dev.sh worktree clean -a"
}

cmd_wt_clean() {
  local arg="${1:-}" f name key path
  if [ -z "$arg" ]; then
    auto_clean
    echo ">>> 清理完成"
    return 0
  fi
  if [ "$arg" = "-a" ] || [ "$arg" = "--all" ]; then
    auto_clean
    for f in "$REG_DIR"/*.env; do
      if [ -f "$f" ]; then
        name="$(basename "$f" .env)"
        if reg_load "$name"; then
          echo ">>> [clean -a] 停止 '$name' ($E_PATH)"
          stop_stack "$E_PATH" "$E_BACKEND_PORT" "$E_FRONTEND_PORT" "$E_FDATA_PORT"
          reg_rm "$name"
        fi
      fi
    done
    echo ">>> 清理完成"
    return 0
  fi
  key="$arg"
  if reg_load "$key"; then
    echo ">>> [clean] 强制停止 '$key' ($E_PATH)"
    stop_stack "$E_PATH" "$E_BACKEND_PORT" "$E_FRONTEND_PORT" "$E_FDATA_PORT"
    reg_rm "$key"
  else
    path="$(dirname "$ROOT")/$key"
    if [ ! -e "$path/.git" ]; then
      die "worktree '$key' 无运行记录且目录不存在"
    fi
    echo ">>> [clean] 无注册记录, 按 cwd 扫尾 '$key' ($path)"
    stop_stack "$path" "" "" ""
  fi
  echo ">>> 清理完成"
}

# --------------------------
# 全局清理: 关闭本系统全部服务 (主树 / 任意 worktree / 任意位置启动的进程)
# 按命令特征匹配 (uvicorn 后端 / fdata / 前端 npm), 不限定端口与注册表
# --------------------------
cmd_stop_all() {
  local pids
  pids="$(pgrep_stack_pids)"
  if [ -z "$pids" ]; then
    echo ">>> 本系统无运行中的服务"
    return 0
  fi
  echo ">>> 将停止以下服务进程 (PID: $(echo "$pids" | tr '\n' ' '))"
  echo "    (uvicorn 后端 / fdata / 前端 Next.js)"
  # TERM
  for pid in $pids; do
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
  sleep 1.5
  # KILL 残留
  for pid in $pids; do
    kill -9 "$pid" 2>/dev/null || true
  done
  # 兜底: 若仍有本系统的监听端口 (命令模式可能没匹配全), 按 cwd 在本系统目录树内才清理
  local port pid2 cwd leftover=0
  for port in $(lsof -iTCP -sTCP:LISTEN -P -t 2>/dev/null | sort -u); do
    for pid2 in $(port_listeners "$port"); do
      cwd="$(proc_cwd "$pid2")"
      case "$cwd" in
        "$ROOT"|"$ROOT"/*|"$parent"|"$parent"/streamlit-*)
          echo "    兜底关闭端口 $port (PID: $pid2, cwd: $cwd)"
          kill_port "$port"
          leftover=$((leftover + 1))
          ;;
      esac
    done
  done
  echo ">>> 全局清理完成 (兜底关闭端口: $leftover)"
  return 0
}

# --------------------------
# 完整帮助
# --------------------------
usage_all() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "命令:"
  echo "  ./dev.sh                              启动主工作树前台 (fdata + 后端 + 前端, Ctrl-C 全停)"
  echo "  NO_FDATA=1 ./dev.sh                   跳过 fdata 启动 (后端回退 CLI)"
  echo
  echo "worktree 子命令:"
  echo "  ./dev.sh worktree new <name> [branch] [base]"
  echo "      创建 ../streamlit-<name> + uv sync/npm ci + 后台启动 (随机端口, 自动注册)"
  echo "  ./dev.sh worktree up [name]            后台启动已有 worktree (缺省 = 当前 worktree)"
  echo "  ./dev.sh worktree down [name]          停止该 worktree 全套服务"
  echo "  ./dev.sh worktree ls                   查看已注册栈 (●运行中 / ○已停止 / ⚠已合并 / ✗已删除)"
  echo "  ./dev.sh worktree clean [name|-a]      回收已删除/已合并栈的资源 (-a = 全部停掉)"
  echo
  echo "全局:"
  echo "  ./dev.sh stop-all|stopall|kill-all     关闭本系统全部服务 (主树/任意 worktree, 不限端口)"
  echo "  ./dev.sh -h|--help|help                显示本帮助"
  echo
  echo "说明:"
  echo "  - 主树与每个 worktree 各自使用随机端口 (后端 20000+ / 前端 21000+ / fdata 22000+),互不冲突"
  echo "  - 注册表放在主 .git/dev-worktrees/,worktree 被删后仍可自动回收残留进程"
  echo "  - 每次运行 ./dev.sh 都会先自动扫描并回收已删除 / 已合并回主干的工作树"
  echo "  - 前端经 BACKEND_ORIGIN 环境变量适配随机后端端口 (缺省 http://localhost:8000)"
  echo "  - worktree 内直接 ./dev.sh 即自动进入 worktree 模式"
  echo "  - stop-all 按命令特征匹配 (uvicorn/fdata/next) 精准终止, 再兜底扫尾端口"
  echo
  echo "环境变量:"
  echo "  NO_FDATA=1  SKIP_SETUP=1  BACKEND_PORT=  FRONTEND_PORT=  FDATA_PORT=  MAIN_BRANCH="
  echo
  echo "示例:"
  echo "  ./dev.sh worktree new fix-demo"
  echo "  ./dev.sh worktree up"
  echo "  ./dev.sh worktree ls"
  echo "  ./dev.sh stop-all"
}

# --------------------------
# stop-all 详细帮助
# --------------------------
usage_stop_all() {
  echo "用法: ./dev.sh stop-all"
  echo
  echo "关闭本系统全部服务 (主树 / 任意 worktree / 任意位置启动的进程), 不限端口."
  echo
  echo "匹配原理:"
  echo "  按命令特征扫描所有进程: uvicorn backend.main:app / trading/fdata.py serve / next dev / next-server"
  echo "  (不限定端口, 不依赖注册表, 因此能覆盖任何 worktree 或异常启动的服务)"
  echo
  echo "终止流程:"
  echo "  1) 向所有匹配进程发送 TERM (让 uvicorn reload/npm 子进程正确退出)"
  echo "  2) 等待 1.5 秒"
  echo "  3) 对仍存活的发 KILL"
  echo "  4) 兜底: 扫尾所有监听端口, 仅当进程 cwd 位于本项目目录树 (主树或 streamlit-* worktree) 时才关闭"
  echo
  echo "说明:"
  echo "  - 此命令不会误杀其他项目/系统的进程 (基于命令特征 + cwd 双重校验)"
  echo "  - 不会清理注册表 (仅杀进程); 如需回收注册项请用 ./dev.sh worktree clean"
  echo
  echo "示例:"
  echo "  ./dev.sh stop-all          关闭全部服务"
  echo "  ./dev.sh stop-all --help   显示本帮助"
}

# --------------------------
# 入口分发
# --------------------------
case "${1:-}" in
  "")
    mode_main
    ;;
  worktree|wt)
    shift
    case "${1:-}" in
      new)   shift; cmd_wt_new "$@" ;;
      up)    shift; cmd_wt_up "${1:-}" ;;
      down)  shift; cmd_wt_down "${1:-}" ;;
      ls|list|ps) cmd_wt_ls ;;
      clean) shift; cmd_wt_clean "${1:-}" ;;
      ""|-h|--help|help) usage_wt ;;
      *) die "未知 worktree 子命令: $1 (可选: new/up/down/ls/clean)" ;;
    esac
    ;;
  stop-all|stopall|kill-all)
    if [ "${2:-}" = "-h" ] || [ "${2:-}" = "--help" ]; then usage_stop_all; else cmd_stop_all; fi
    ;;
  -h|--help|help)
    usage_all
    ;;
  *)
    die "未知参数: $1 — 直接 ./dev.sh 启动, 或 ./dev.sh worktree <new|up|down|ls|clean>"
    ;;
esac
