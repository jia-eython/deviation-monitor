#!/usr/bin/env bash
# ============================================================
#  DeviationMonitor 安装脚本（自包含）
#  分工：
#    · 配置文件 .env —— 由你手动填写（token/标的/市场/参数），本脚本绝不修改它
#    · 本脚本负责 —— 装 Python → 建 venv → 装依赖 → 校验 .env → dry-run 验证 →
#                    可选装 crontab → 可选测试微信推送
#  用法：
#      tar xzf deviation_monitor_linux_deploy.tar.gz
#      cd DeviationMonitor
#      先编辑 .env（没有会自动生成模板，填完再重跑本脚本）
#      bash deploy/install_deviation_monitor.sh
#  说明：只推送微信买入提示，绝不自动下单；默认 dry-run，显式 --push 才推微信、才读写状态文件。
# ============================================================
set -euo pipefail

# ── 定位工程根目录（本脚本位于 <工程>/deploy/ 下）──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$DIR"

log()  { echo -e "\033[1;32m[安装]\033[0m $*"; }
warn() { echo -e "\033[1;33m[警告]\033[0m $*"; }
die()  { echo -e "\033[1;31m[错误]\033[0m $*" >&2; exit 1; }

# ── 系统信息 ──
if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
log "系统: ${PRETTY_NAME:-未知} | 内核: $(uname -r)"

# ══════════════════════════════════════════════════════════
# 第 1 步：确保 Python 3.8+（Alibaba Cloud Linux 3 默认 3.6）
# ══════════════════════════════════════════════════════════
find_python() {
  local c v major minor
  for c in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null || echo "0 0")"
      major="${v%% *}"; minor="${v##* }"
      if [ "$major" -ge 3 ] && [ "$minor" -ge 8 ]; then
        PY="$c"; return 0
      fi
    fi
  done
  return 1
}

if ! find_python; then
  log "未找到 Python 3.8+，开始安装..."
  if command -v dnf >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
      dnf install -y python38 python38-pip python38-devel
    else
      sudo dnf install -y python38 python38-pip python38-devel
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
      apt-get update && apt-get install -y python3 python3-pip python3-venv
    else
      sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv
    fi
  else
    die "无法自动安装 Python，请手动安装 Python 3.8+ 后重试"
  fi
  find_python || die "Python 3.8+ 安装失败，请手动安装后重试"
fi
log "使用 Python: $PY（$("$PY" --version)）"

# ══════════════════════════════════════════════════════════
# 第 2 步：虚拟环境（venv 不可用则直接用系统 Python）
# ══════════════════════════════════════════════════════════
VENV_PY=""
if [ ! -x "$DIR/.venv/bin/python" ]; then
  if "$PY" -m venv "$DIR/.venv" >/dev/null 2>&1 && [ -x "$DIR/.venv/bin/python" ]; then
    VENV_PY="$DIR/.venv/bin/python"
    log "虚拟环境已创建: $DIR/.venv"
  else
    warn "python3-venv 不可用，改用系统 Python 直接安装依赖"
    VENV_PY="$PY"
  fi
else
  VENV_PY="$DIR/.venv/bin/python"
  log "复用已有虚拟环境: $DIR/.venv"
fi

# ══════════════════════════════════════════════════════════
# 第 3 步：安装依赖（requests/pandas/yfinance/akshare + 微牛 SDK）
# ══════════════════════════════════════════════════════════
log "升级 pip 并安装依赖，可能需要几分钟..."
"$VENV_PY" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
"$VENV_PY" -m pip install -r "$DIR/deploy/requirements.txt"
if [ -f "$DIR/deploy/webull_openapi_python_sdk-2.0.17-py3-none-any.whl" ]; then
  "$VENV_PY" -m pip install "$DIR/deploy/webull_openapi_python_sdk-2.0.17-py3-none-any.whl" >/dev/null 2>&1 \
    && log "微牛 SDK 已安装" \
    || warn "微牛 SDK 安装失败（不影响：美股行情会自动回退 yfinance）"
fi

# ══════════════════════════════════════════════════════════
# 第 4 步：.env —— 部署包已预填（token/标的/阈值），脚本绝不改写
#   缺失 → 报错退出；存在 → 只做只读校验
# ══════════════════════════════════════════════════════════
if [ ! -f "$DIR/.env" ]; then
  warn "未找到 $DIR/.env（部署包应已自带预填配置）"
  echo "    若确实缺失，请从项目根目录复制 .env.example 为 .env 并填写后重跑"
  exit 1
fi
log "检测到配置文件: $DIR/.env（预填版，本脚本不会修改它）"

# ── 只读校验（不改文件）──
SYMBOLS_VAL="$(grep -E '^DEVIATION_SYMBOLS=' "$DIR/.env" | tail -1 | cut -d= -f2- || true)"
if [ -z "$SYMBOLS_VAL" ]; then
  warn "DEVIATION_SYMBOLS 未填写或为空，请编辑 $DIR/.env 后重跑"
  exit 1
fi
log "监控标的: $SYMBOLS_VAL"
TOKEN_VAL="$(grep -E '^PUSHPLUS_TOKEN=' "$DIR/.env" | tail -1 | cut -d= -f2- || true)"
if [ -z "$TOKEN_VAL" ]; then
  warn "PUSHPLUS_TOKEN 未填写：可继续安装，但 --push 不会真的发微信（之后补上即可）"
else
  log "PUSHPLUS_TOKEN 已填写（长度 ${#TOKEN_VAL}，不打印明文）"
fi

# ══════════════════════════════════════════════════════════
# 第 5 步：dry-run 验证（读取 .env 里的标的，只拉行情计算，
#          不推送微信、不读写状态文件）
# ══════════════════════════════════════════════════════════
log "dry-run 验证（读取 $DIR/.env，不推送、不碰状态文件）..."
if "$VENV_PY" "$DIR/deviation_monitor.py" --dry-run; then
  log "dry-run 通过 ✅"
else
  warn "dry-run 有部分标的失败（常见原因：服务器无法访问行情源/网络受限），正式推送时请复查"
fi

# ══════════════════════════════════════════════════════════
# 第 6 步：可选安装 crontab（每 2 小时整点 --push，带进程锁）
# ══════════════════════════════════════════════════════════
do_cron=0
if [ "${AUTO_INSTALL_CRON:-}" = "1" ]; then
  do_cron=1
elif [ -t 0 ]; then
  read -rp "是否安装 crontab 定时任务（每 2 小时整点 --push）？[y/N] " _ans
  case "$_ans" in y|Y|yes|YES) do_cron=1;; esac
fi
if [ "$do_cron" = "1" ]; then
  if ! command -v crontab >/dev/null 2>&1; then
    warn "系统未安装 crontab 命令，跳过（可稍后手动配置）"
  else
    CRON_LINE="0 */2 * * * cd $DIR && $VENV_PY $DIR/deviation_monitor.py --push >> $DIR/results/cron_deviation.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v "deviation_monitor.py" || true; echo "$CRON_LINE" ) | crontab -
    log "crontab 已安装（幂等：先清旧行再写入）:"
    echo "    $CRON_LINE"
  fi
else
  warn "跳过 crontab 安装（可稍后参考 $DIR/crontab.example 手动配置）"
fi

# ══════════════════════════════════════════════════════════
# 第 7 步：可选测试微信推送（会真发一条，走内嵌消息模块）
# ══════════════════════════════════════════════════════════
do_test=0
if [ "${TEST_PUSH:-}" = "1" ]; then
  do_test=1
elif [ -t 0 ]; then
  read -rp "是否现在测试微信推送（会真发一条消息到你的微信）？[y/N] " _ans
  case "$_ans" in y|Y|yes|YES) do_test=1;; esac
fi
if [ "$do_test" = "1" ]; then
  if [ -z "$TOKEN_VAL" ]; then
    warn "PUSHPLUS_TOKEN 未填写，跳过测试"
  else
    log "发送测试消息..."
    "$VENV_PY" "$DIR/notify.py" || warn "测试推送失败，请检查 token 与网络"
  fi
fi

# ══════════════════════════════════════════════════════════
# 完成
# ══════════════════════════════════════════════════════════
echo ""
log "✅ 安装完成！"
echo "    手动检测（不推送）: cd $DIR && $VENV_PY deviation_monitor.py --dry-run"
echo "    手动推送          : cd $DIR && $VENV_PY deviation_monitor.py --push"
echo "    查看检测日志      : tail -f $DIR/results/deviation.log"
echo "    查看 cron 日志    : tail -f $DIR/results/cron_deviation.log"
echo "    查看定时任务      : crontab -l"
echo "    修改监控配置      : vim $DIR/.env   （改完无需重装，直接重跑脚本或手动执行）"
echo "    测试微信推送      : $VENV_PY $DIR/notify.py"
echo ""
echo "    注意：本程序只推送微信买入提示，绝不自动下单。"
echo ""