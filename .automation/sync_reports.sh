#!/bin/zsh
# 自动将云端生成的每日 AI 早报同步到本地「文稿/每日报告」文件夹
set -euo pipefail

REPO_DIR="/Users/ccjiatonghao/Documents/每日报告"
cd "$REPO_DIR"

if ! git remote get-url origin >/dev/null 2>&1; then
  exit 0
fi

git fetch origin main --quiet 2>/dev/null || exit 0
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")

if [[ -n "$REMOTE_HEAD" && "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  git pull --rebase --autostash origin main --quiet 2>/dev/null || true
fi
