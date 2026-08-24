#!/usr/bin/env bash
# 推送到 GitHub（发布前请确认仓库已创建且为 Public/Private 按你意愿）
# 用法：bash deploy/push_to_github.sh [仓库URL]
# 默认仓库：https://github.com/jia-eython/deviation-monitor.git
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
REPO_URL="${1:-https://github.com/jia-eython/deviation-monitor.git}"
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO_URL"
git branch -M main
git push -u origin main
echo "✅ 已推送: $REPO_URL"
echo "   请到 GitHub 仓库页确认可见性设置无误。"