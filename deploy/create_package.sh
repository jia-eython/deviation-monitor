#!/usr/bin/env bash
# ============================================================
#  生成 Linux 一键部署包（在任意本机执行：Windows Git Bash / Linux / macOS）
#  产物：DeviationMonitor/deviation_monitor_linux_deploy.tar.gz
#  用法：bash deploy/create_package.sh
# ============================================================
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
PKG="deviation_monitor_linux_deploy.tar.gz"
STAGE_ROOT="$(mktemp -d)"
STAGE="$STAGE_ROOT/DeviationMonitor"
mkdir -p "$STAGE/deploy"

# 主程序 + 内嵌消息/配置/行情模块（自包含）
cp deviation_monitor.py notify.py config.py webull_client.py "$STAGE/"
cp .env crontab.example README_deviation.md "$STAGE/"   # 预填好配置的 .env，直接可跑
cp deploy/install_deviation_monitor.sh deploy/requirements.txt "$STAGE/deploy/"

# 微牛 SDK（可选：离线安装；失败不影响，美股自动回退 yfinance）
if [ -f "../AutoFolio/deploy/webull_openapi_python_sdk-2.0.17-py3-none-any.whl" ]; then
  cp "../AutoFolio/deploy/webull_openapi_python_sdk-2.0.17-py3-none-any.whl" "$STAGE/deploy/"
  echo "  ✓ 已附带微牛 SDK wheel"
else
  echo "  ⚠ 未找到微牛 SDK wheel，部署时自动跳过（美股回退 yfinance）"
fi

rm -f "$PKG"
tar -C "$STAGE_ROOT" -czf "$DIR/$PKG" DeviationMonitor
rm -rf "$STAGE_ROOT"

echo ""
echo "✅ 已生成一键部署包: $DIR/$PKG"
echo "   大小: $(du -h "$DIR/$PKG" | cut -f1)"
if command -v sha256sum >/dev/null 2>&1; then
  echo "   SHA256: $(sha256sum "$DIR/$PKG" | awk '{print $1}')"
fi
echo ""
echo "服务器部署步骤："
echo "  scp $PKG root@你的服务器IP:~/"
echo "  ssh root@你的服务器IP"
echo "  tar xzf $PKG && cd DeviationMonitor && bash deploy/install_deviation_monitor.sh"
echo ""