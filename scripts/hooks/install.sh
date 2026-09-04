#!/usr/bin/env bash
# 安装 worktree 初始化钩子：把 scripts/hooks/post-checkout 软链到 .git/hooks/post-checkout。
# 用相对路径软链，仓库整体移动后仍有效；脚本本体在仓库内，改动即时生效。
# 重装/换机器 clone 后重跑一次本脚本即可。
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook_src="scripts/hooks/post-checkout"
hook_dst="$repo_root/.git/hooks/post-checkout"

chmod +x "$repo_root/$hook_src"
# 相对软链是相对于链接所在目录 (.git/hooks/) 解析的，所以要向上两级
ln -sfn "../../$hook_src" "$hook_dst"
echo "installed: .git/hooks/post-checkout -> $hook_src"
