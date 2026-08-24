# -*- coding: utf-8 -*-
"""
回撤偏离监控 → 微信「买入提示」（复位式去重）
============================================
核心逻辑（唯一检测项：K线价格回撤，与账户/持仓/成本/净值完全无关）：
  对每个监控标的，只用【该标的的K线】计算回撤：
    基准价   = 近 N 日K线最高价（--ref-high / DEVIATION_REF_HIGH，默认 20 日）
    回撤幅度 = (基准价 - 现价) / 基准价
    现价     = 最新K线收盘价（日线最后一根，盘中即最新价，可配 DEVIATION_PRICE_FIELD）
  触发：回撤 >= 触发阈值（--dd-threshold / DEVIATION_DD_THRESHOLD，默认 0.05 即 5%）
  复位：回撤 <= 复位阈值（--reset-threshold / DEVIATION_RESET_THRESHOLD，默认 0.03 即 3%）

复位式去重（状态机，每个标的独立一个 channel，互不影响）：
  armed     : 未触发。回撤 >= 触发阈值 → 微信推送一次，并把状态置为 triggered
  triggered : 本波已推送过。回撤继续变大/持续多轮也不重复推送；
              回撤 <= 复位阈值 → 修复，置回 armed（重新武装），下一波再触发才再推
  即：每波回撤只提醒一次；必须等价格反弹、回撤收窄到复位线以内，才允许下一波提醒。

状态文件写入口径（重要）：
  --push     : 唯一会「更新状态文件 + 推送微信」的入口（并加进程锁防并发）
  --dry-run  : 默认。只做纯计算、打印、写日志；【不读也不写状态文件】，
               试配置/验行情时绝不污染正式的去重状态。

安全边界（硬性）：
  1. 只提示，绝不自动下单（本程序没有任何下单/资金接口）。
  2. 默认 --dry-run，显式 --push 才推微信、才读/写状态文件。
  3. 行情接口全部只读（Webull 沙盒 / yfinance / 腾讯直连 / akshare）。

内嵌消息模块（自包含，直接 import，不重写）：
  - notify.send(title, content) -> (ok, info)  微信推送（PushPlus）
  - notify.rotate_file(path, max_lines)        日志轮转
  - config                                     读取本工程 .env（密钥/环境）
  - webull_client.WebullClient                 微牛沙盒只读行情（懒加载，A股模式无需 SDK）
本工程自带 notify.py / config.py / webull_client.py 内嵌副本（部署包即用）；
若这些文件缺失，自动回退 ../AutoFolio/scripts（本地旧版布局兼容）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# ── Windows 控制台强制 UTF-8（中文不乱码）──────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════
# 内嵌模块优先（自包含部署），AutoFolio 仅作回退
# 本工程自带 notify.py / config.py / webull_client.py（内嵌消息模块等）；
# 若本工程缺少这些文件（旧版/被删除），自动回退 ../AutoFolio/scripts
# ══════════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent            # 工程根目录（DeviationMonitor）


def _find_autofolio_scripts():
    """AutoFolio 回退路径：本工程内嵌 AutoFolio/ 或同级 ../AutoFolio/。"""
    for cand in (PROJECT_ROOT / "AutoFolio", PROJECT_ROOT.parent / "AutoFolio"):
        p = cand / "scripts"
        if p.is_dir():
            return p
    return None


_AF_SCRIPTS = _find_autofolio_scripts()
# 本工程根目录必须排最前，保证 import 命中内嵌的 notify/config/webull_client
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if _AF_SCRIPTS is not None:
    AUTOFOLIO_SCRIPTS = _AF_SCRIPTS
    # AutoFolio 路径追加在末尾（仅作回退），内嵌模块永远优先
    for _p in (str(AUTOFOLIO_SCRIPTS), str(AUTOFOLIO_SCRIPTS.parent)):
        if _p not in sys.path:
            sys.path.append(_p)

# 内嵌模块优先；缺失时回退 AutoFolio
if (PROJECT_ROOT / "notify.py").is_file() and (PROJECT_ROOT / "config.py").is_file():
    import config                              # noqa: E402  内嵌配置（本工程 .env）
    from notify import send, rotate_file       # noqa: E402  内嵌微信推送 + 日志轮转
    MODULE_SOURCE = "内嵌模块（DeviationMonitor 自包含）"
elif _AF_SCRIPTS is not None:
    import config                              # noqa: E402  AutoFolio 配置
    from notify import send, rotate_file       # noqa: E402  AutoFolio 微信推送
    MODULE_SOURCE = f"AutoFolio 回退（{AUTOFOLIO_SCRIPTS}）"
else:
    print("❌ 找不到消息/配置模块：本工程缺少 notify.py/config.py，且 ../AutoFolio/scripts 也不存在",
          file=sys.stderr)
    sys.exit(2)

RESULTS_DIR = PROJECT_ROOT / "results"
STATE_FILE = RESULTS_DIR / "deviation_state.json"   # 复位式去重状态（仅 --push 读写）
LOG_FILE = RESULTS_DIR / "deviation.log"            # 每次运行一行日志（dry-run 也写）
LOCK_FILE = RESULTS_DIR / "deviation_monitor.lock"  # 进程锁（仅 --push 使用）

TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


# ══════════════════════════════════════════════════════════════════
# .env 读取：本工程根目录 .env（已由内嵌 config 载入；此处再做一次覆盖合并，
# 保证命令行/环境变量之外的单文件配置入口只有一个：DeviationMonitor/.env）
# ══════════════════════════════════════════════════════════════════
def load_local_env() -> None:
    """读取本工程 .env；其中的键覆盖已加载的同名配置。"""
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        print(f"⚠️ 读取本工程 .env 失败: {e}", file=sys.stderr)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


def sync_secrets_to_config() -> None:
    """把 .env 中的密钥/环境同步给已加载的 config 模块（本工程 .env 为唯一配置入口）。"""
    for key in ("PUSHPLUS_TOKEN", "SANDBOX_APP_KEY", "SANDBOX_APP_SECRET",
                "PROD_APP_KEY", "PROD_APP_SECRET", "ENVIRONMENT"):
        if key in os.environ:
            setattr(config, key, os.environ[key])


load_local_env()
sync_secrets_to_config()


# ══════════════════════════════════════════════════════════════════
# 通用小工具
# ══════════════════════════════════════════════════════════════════
def pct(x: float) -> str:
    """百分比统一保留 2 位小数打印。"""
    return f"{x * 100:.2f}%"


def env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_float(key: str, default: float) -> float:
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        print(f"⚠️ .env 中 {key}={v!r} 不是合法数字，使用默认值 {default}")
        return default


def env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        print(f"⚠️ .env 中 {key}={v!r} 不是合法整数，使用默认值 {default}")
        return default


def parse_map(raw: str, cast):
    """解析 'SPY:30,QQQ:10' → {'SPY': 30, 'QQQ': 10}（单标的覆盖配置）。"""
    result = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            print(f"⚠️ 忽略格式错误的单标的覆盖项 {pair!r}（应为 符号:值）")
            continue
        sym, val = pair.split(":", 1)
        try:
            result[sym.strip().upper()] = cast(val.strip())
        except ValueError:
            print(f"⚠️ 忽略无法解析的单标的覆盖项 {pair!r}")
    return result


def _candidates(symbol: str) -> list[str]:
    """给出一个标的的所有等价写法（供单标的覆盖配置查找）：
    600519 ↔ SH600519 ↔ sh600519 都视为同一标的。"""
    s = symbol.strip()
    out = [s.upper()]
    low = s.lower()
    if low.isdigit() and len(low) == 6:
        for p in ("sh", "sz", "bj"):
            out.append((p + low).upper())
    elif len(low) == 8 and low[:2] in ("sh", "sz", "bj") and low[2:].isdigit():
        out.append(low[2:].upper())
    return out


def lookup(overrides: dict, symbol: str, default):
    for c in _candidates(symbol):
        if c in overrides:
            return overrides[c]
    return default


def parse_inline_symbol(raw: str) -> tuple[str, str | None]:
    """解析带市场前缀的标的写法（两种都支持）：
    'us:ASHR' / 'a:510900'（市场:代码）或 '600519:a' / 'ASHR:us'（代码:市场）
    解析失败则原样返回 (raw, None)。"""
    parts = raw.split(":", 1)
    if len(parts) == 2:
        left, right = parts[0].strip().lower(), parts[1].strip()
        if left in ("us", "a"):
            return right, left
        if right.lower() in ("us", "a"):
            return left, right.lower()
    return raw, None


def a_share_code(symbol: str) -> str:
    """A股6位代码 → 带交易所前缀的代码（sh/sz/bj）；已带前缀的按原样返回。

    前缀规则：6/9/5 开头→sh（含 5 开头沪市基金/ETF）；0/1/2/3 开头→sz；
              4/8 开头→bj（北交所）。"""
    s = symbol.strip().lower()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj") and s[2:].isdigit():
        return s
    if s.isdigit() and len(s) == 6:
        if s[0] in ("6", "9", "5"):
            return "sh" + s
        if s[0] in ("0", "1", "2", "3"):
            return "sz" + s
        if s[0] in ("4", "8"):
            return "bj" + s
    raise ValueError(f"A股代码格式不正确（应为6位数字或带 sh/sz/bj 前缀）: {symbol!r}")


def normalize_symbol(symbol: str, market: str) -> str:
    """状态文件里的键：美股统一大写；A股统一为带前缀小写代码（sh600519）。"""
    if market == "a":
        return a_share_code(symbol)
    return symbol.strip().upper()


def now_iso() -> str:
    """带时区的时间串（尽量用北京时间）。"""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = None
    return datetime.now(tz).isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════
# 行情数据源（全部只读；美股 Webull 沙盒→yfinance，A股 腾讯→akshare）
# ══════════════════════════════════════════════════════════════════
def _standardize(df):
    """统一为 index=date, 列 open/high/low/close/volume 的 DataFrame。"""
    import pandas as pd
    out = df.copy()
    out.columns = [str(c).lower().strip() for c in out.columns]
    if isinstance(out.index, pd.DatetimeIndex):
        out = out.reset_index()
    date_col = next((c for c in ("date", "datetime", "index") if c in out.columns), None)
    if date_col is None:
        raise RuntimeError(f"K线数据缺少日期列（实际列: {list(out.columns)}）")
    out = out.rename(columns={date_col: "date"})
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            raise RuntimeError(f"K线数据缺少列: {col}（实际列: {list(out.columns)}）")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date", "close", "high"])
    out = out.set_index("date").sort_index()
    return out[~out.index.duplicated(keep="last")]


def _date_range(n: int) -> tuple[str, str]:
    """按窗口 N 日反推拉取区间（至少 90 天，避免周末/节假日K线不足）。"""
    end = datetime.now() + timedelta(days=1)
    start = datetime.now() - timedelta(days=max(90, n * 5))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def fetch_tencent(code: str, n: int):
    """腾讯直连日K（前复权）。返回列序 [date, open, close, high, low, volume]。"""
    import pandas as pd
    import requests
    start, end = _date_range(n)
    r = requests.get(TENCENT_URL, params={
        "param": f"{code},day,{start},{end},640,qfq",
    }, timeout=15)
    r.raise_for_status()
    body = r.json()
    node = (body.get("data") or {}).get(code) or {}
    rows = None
    for key in ("qfqday", "day", "hfqday"):
        if isinstance(node.get(key), list) and node[key]:
            rows = node[key]
            break
    if not rows:
        raise RuntimeError(f"腾讯返回无K线: {str(body)[:200]}")
    records = [row[:6] for row in rows
               if isinstance(row, (list, tuple)) and len(row) >= 6]
    if not records:
        raise RuntimeError("腾讯K线无有效数据行")
    df = pd.DataFrame(records, columns=["date", "open", "close", "high", "low", "volume"])
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date", "close", "high"]).set_index("date").sort_index()


def _finalize_akshare(raw):
    import pandas as pd
    out = raw.copy()
    for col in ("date", "open", "high", "low", "close", "volume"):
        if col not in out.columns:
            raise RuntimeError(f"akshare 返回缺少列: {col}（实际列: {list(out.columns)}）")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    return out.dropna(subset=["date", "close", "high"]).set_index("date").sort_index()


def fetch_akshare(code: str, digits: str, n: int):
    """akshare 回退：先东方财富，再新浪（前复权）。"""
    import akshare as ak
    start, end = _date_range(n)
    s, e = start.replace("-", ""), end.replace("-", "")
    errors = []
    # ① 东方财富（stock_zh_a_hist）
    try:
        raw = ak.stock_zh_a_hist(symbol=digits, period="daily",
                                 start_date=s, end_date=e, adjust="qfq")
        if raw is not None and not raw.empty:
            raw = raw.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                      "最高": "high", "最低": "low", "成交量": "volume"})
            return _finalize_akshare(raw)
        errors.append("东方财富返回空")
    except Exception as ex:
        errors.append(f"东方财富: {ex}")
    # ② 新浪（stock_zh_a_daily，列序 date/open/high/low/close/volume）
    try:
        raw = ak.stock_zh_a_daily(symbol=code, start_date=s, end_date=e, adjust="qfq")
        if raw is not None and not raw.empty:
            return _finalize_akshare(raw)
        errors.append("新浪返回空")
    except Exception as ex:
        errors.append(f"新浪: {ex}")
    raise RuntimeError("akshare 两源均失败: " + "; ".join(errors))


def fetch_a(code: str, n: int):
    """A股：腾讯直连优先（北交所直接走 akshare），失败回退 akshare。"""
    digits = code[-6:] if code[:2].isalpha() else code
    if not code.startswith("bj"):
        try:
            return fetch_tencent(code, n), "腾讯直连"
        except Exception as e:
            print(f"  [a] 腾讯直连失败: {e} → 回退 akshare（东方财富/新浪）")
    return fetch_akshare(code, digits, n), "akshare(东财/新浪)"


_us_client_cache: dict = {}


def fetch_us(symbol: str, n: int):
    """美股：Webull 沙盒（只读行情）优先，自动回退 yfinance。"""
    # 防网络卡死：给全局 socket 设默认超时（Webull/yfinance/requests 都受控）
    try:
        import socket
        socket.setdefaulttimeout(15)
    except Exception:
        pass
    count = max(60, min(1200, n + 60))
    try:
        client = _us_client_cache.get("client")
        if client is None:
            from webull_client import WebullClient  # 懒加载：A股-only 环境无需安装 SDK
            client = WebullClient(environment=(config.ENVIRONMENT or "sandbox"))
            _us_client_cache["client"] = client
        df = client.get_history_bars(symbol, count=count)
        if df is not None and not df.empty:
            return _standardize(df), "Webull沙盒(只读行情)"
        raise RuntimeError("Webull 返回空K线")
    except Exception as e:
        print(f"  [us] Webull 失败: {e} → 回退 yfinance")
    try:
        import yfinance as yf
        period = "1y" if n <= 220 else "5y"
        df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
        if df is None or df.empty:
            raise RuntimeError("yfinance 返回空K线")
        return _standardize(df), "yfinance"
    except Exception as e:
        raise RuntimeError(f"美股行情双源均失败: {e}")


# ══════════════════════════════════════════════════════════════════
# 配置组装（命令行 > 单标的覆盖 > .env 全局 > 内置默认）
# ══════════════════════════════════════════════════════════════════
class MonitorSpec:
    """单个监控标的的完整参数。"""

    def __init__(self, symbol: str, market: str, key: str, ref_high: int,
                 dd_threshold: float, reset_threshold: float, price_field: str):
        self.symbol = symbol              # 用户原始输入
        self.market = market              # us / a
        self.key = key                    # 状态文件键（规范化后的代码）
        self.ref_high = ref_high          # 近 N 日窗口
        self.dd_threshold = dd_threshold  # 触发阈值
        self.reset_threshold = reset_threshold  # 复位阈值
        self.price_field = price_field    # close / last（均为最后一根K线收盘价口径）

    @property
    def market_name(self) -> str:
        return "美股" if self.market == "us" else "A股"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="K线回撤偏离监控：回撤超阈值 → 微信买入提示（只提示，绝不下单；复位式去重）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--market", choices=["us", "a"], default=None,
                   help="市场：us=美股 a=A股。默认读 DEVIATION_MARKET，再默认 us")
    p.add_argument("--symbols", default=None,
                   help="监控标的，逗号分隔（覆盖 .env 的 DEVIATION_SYMBOLS）。支持混合市场前缀，"
                        "如 us:ASHR,us:FXI,a:510900（等价写法 510900:a）")
    p.add_argument("--ref-high", type=int, default=None,
                   help="基准价窗口：近 N 日K线最高价（命令行提供时覆盖 .env 全局及单标的配置）")
    p.add_argument("--dd-threshold", type=float, default=None,
                   help="触发阈值（回撤 >= 该值触发），如 0.05 表示 5%%")
    p.add_argument("--reset-threshold", type=float, default=None,
                   help="复位阈值（回撤 <= 该值重新武装），如 0.03 表示 3%%；必须小于触发阈值")
    p.add_argument("--price-field", choices=["close", "last"], default=None,
                   help="现价口径：close=最新K线收盘价（默认）；last=最后一根K线收盘价（盘中即最新价）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="push", action="store_false",
                   help="默认模式：只计算打印+写日志，不推微信、不读不写状态文件")
    g.add_argument("--push", dest="push", action="store_true",
                   help="真正推送微信（仅在触发时），读/写状态文件，并加进程锁防并发")
    p.set_defaults(push=False)   # 默认 dry-run
    return p.parse_args(argv)


def build_specs(args: argparse.Namespace) -> list[MonitorSpec]:
    """把命令行 + .env 组装成监控标的清单，并打印供人工核对。"""
    # ── 标的列表 ──
    raw_symbols = args.symbols or env_str("DEVIATION_SYMBOLS")
    if not raw_symbols:
        print("❌ 未配置监控标的：请在 .env 里设置 DEVIATION_SYMBOLS（逗号分隔），"
              "或命令行传 --symbols SPY,QQQ,600519（也支持 us:ASHR,a:510900 混合写法）")
        sys.exit(2)
    sym_list = [s.strip() for s in raw_symbols.split(",") if s.strip()]
    if not sym_list:
        print("❌ DEVIATION_SYMBOLS 解析后为空")
        sys.exit(2)

    # ── 市场（命令行 > 单标的覆盖 > 全局 > us）──
    market_overrides = {k: v.lower() for k, v in
                        parse_map(env_str("DEVIATION_MARKETS"), str).items()}
    if args.market:
        global_market = args.market
        market_overrides = {}           # 命令行市场覆盖一切单标的市场配置
    else:
        global_market = (env_str("DEVIATION_MARKET") or "us").lower()
    if global_market not in ("us", "a"):
        print(f"❌ 市场配置无效: {global_market!r}（只能 us 或 a）")
        sys.exit(2)

    # ── N日窗口 / 触发阈值 / 复位阈值（命令行优先，且命令行提供时忽略单标的覆盖）──
    n_overrides = {} if args.ref_high is not None else         parse_map(env_str("DEVIATION_REF_HIGH_OVERRIDES"), int)
    dd_overrides = {} if args.dd_threshold is not None else         parse_map(env_str("DEVIATION_DD_THRESHOLD_OVERRIDES"), float)
    reset_overrides = {} if args.reset_threshold is not None else         parse_map(env_str("DEVIATION_RESET_THRESHOLD_OVERRIDES"), float)

    global_n = args.ref_high if args.ref_high is not None else env_int("DEVIATION_REF_HIGH", 20)
    global_dd = args.dd_threshold if args.dd_threshold is not None else         env_float("DEVIATION_DD_THRESHOLD", 0.05)
    global_reset = args.reset_threshold if args.reset_threshold is not None else         env_float("DEVIATION_RESET_THRESHOLD", 0.03)

    price_field = (args.price_field or env_str("DEVIATION_PRICE_FIELD", "close")).lower()
    if price_field not in ("close", "last"):
        print(f"❌ DEVIATION_PRICE_FIELD 无效: {price_field!r}（只能 close 或 last）")
        sys.exit(2)

    # ── 组装并校验 ──
    specs: list[MonitorSpec] = []
    seen: set[str] = set()
    for raw in sym_list:
        sym, inline_market = parse_inline_symbol(raw)
        market = global_market
        if args.market is None:   # 命令行 --market 优先于一切单标的市场设置
            if inline_market:
                market = inline_market
            for cand in _candidates(sym):
                if cand in market_overrides:
                    market = market_overrides[cand].lower()
                    break
        if market not in ("us", "a"):
            print(f"❌ 标的 {raw} 的市场配置无效: {market!r}")
            sys.exit(2)
        try:
            key = normalize_symbol(sym, market)
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(2)
        if key in seen:
            print(f"⚠️ 标的 {sym} 重复配置，已跳过")
            continue
        seen.add(key)

        n = lookup(n_overrides, sym, global_n)
        dd_th = lookup(dd_overrides, sym, global_dd)
        reset_th = lookup(reset_overrides, sym, global_reset)

        # 参数合法性校验（复位阈值必须严格小于触发阈值，否则会变成每轮都推的"振荡器"）
        if not (2 <= n <= 1200):
            print(f"❌ 标的 {sym}: N日窗口 {n} 不合法（2 <= N <= 1200）")
            sys.exit(2)
        if not (0 < dd_th <= 1):
            print(f"❌ 标的 {sym}: 触发阈值 {dd_th} 不合法（0 < 触发阈值 <= 1）")
            sys.exit(2)
        if not (0 <= reset_th < dd_th):
            print(f"❌ 标的 {sym}: 复位阈值 {reset_th} 必须满足 0 <= 复位阈值 < 触发阈值 {dd_th}")
            sys.exit(2)

        specs.append(MonitorSpec(sym, market, key, n, dd_th, reset_th, price_field))
    return specs


# ══════════════════════════════════════════════════════════════════
# 复位式状态机（每标的一个 channel；状态文件仅 --push 模式读/写）
# ══════════════════════════════════════════════════════════════════
def load_state(path: Path) -> dict:
    """读取状态文件。缺失→空字典；损坏→告警并当作空字典重建（不崩溃）。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 状态文件损坏（{e}），按空状态重建（默认全部 armed）")
        return {}
    if not isinstance(raw, dict):
        print("⚠️ 状态文件格式错误（非对象），按空状态重建（默认全部 armed）")
        return {}
    return raw


def normalize_symbol_state(raw) -> dict:
    """单个标的的状态条目：字段缺失/损坏 → 自动按 armed 重建（保留有效的历史提醒计数）。"""
    base = {"state": "armed", "last_trigger_dd": None, "last_push_time": None,
            "last_reset_time": None, "push_count": 0}
    if not isinstance(raw, dict):
        return dict(base)
    st = dict(raw)
    if st.get("state") not in ("armed", "triggered"):
        rebuilt = dict(base)
        pc = st.get("push_count")
        if isinstance(pc, int) and pc > 0:
            rebuilt["push_count"] = pc
        return rebuilt
    return {**base, **st}


def atomic_write_json(path: Path, data: dict) -> None:
    """原子写状态文件（先写临时文件再 rename，避免写一半损坏）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_content(spec: MonitorSpec, current: float, ref_high: float, n_used: int,
                  dd: float, push_count: int, detect_time: str) -> str:
    """PushPlus 正文（纯文本）。"""
    return "\n".join([
        f"【标的】{spec.key}（{spec.market_name}）",
        f"【现价】{current:.2f}",
        f"【基准价】{ref_high:.2f}（近{n_used}日K线最高价）",
        f"【回撤幅度】{pct(dd)}（触发线 {pct(spec.dd_threshold)} / 复位线 {pct(spec.reset_threshold)}）",
        "【建议】可分批买入（仅价格回撤提示，非投资建议）",
        f"【提醒】本轮为第 {push_count} 次提醒（复位式）",
        f"【检测时间】{detect_time}",
        "——",
        "说明：本提示仅基于K线价格回撤生成，监控程序绝不自动下单。",
    ])


def decide_push_mode(spec: MonitorSpec, dd: float, det: dict, states: dict, now: str) -> dict:
    """--push 模式的状态机推进。返回 {note, pushed, attempted, extra, mutated}。

    - armed + 回撤>=触发线 → 推送一次并置 triggered（推送成功才置，失败保持 armed 下轮重试）
    - triggered + 回撤<=复位线 → 置回 armed（重新武装，不推送）
    - triggered + 其余情况 → 不重复推送，等待修复
    """
    st = normalize_symbol_state(states.get(spec.key))
    result = {"note": "", "pushed": False, "attempted": False, "extra": "", "mutated": False}

    if st["state"] == "armed" and dd >= spec.dd_threshold:
        push_count = int(st.get("push_count") or 0) + 1
        title = f"[买入提示] {spec.key} 回撤 {pct(dd)}"
        # 用本次检测到的真实现价/基准价组装正文
        detect_time = now[:19].replace("T", " ") if "T" in now else now
        content = build_content(spec, current=det["current"], ref_high=det["ref_high"],
                                n_used=det["n_used"], dd=dd,
                                push_count=push_count, detect_time=detect_time)
        ok, info = send(title, content)
        result["attempted"] = True
        if ok:
            st.update({
                "state": "triggered",
                "last_trigger_dd": round(dd, 6),
                "last_push_time": now,
                "push_count": push_count,
                "market": spec.market,
            })
            states[spec.key] = st
            result.update(note=f"triggered（新触发，第{push_count}次提醒已推送）",
                          pushed=True, extra=info, mutated=True)
        else:
            result.update(
                note=f"armed（推送失败，保持武装下次重试；第{push_count}次尝试）",
                extra=f"推送失败: {info}")
        return result

    if st["state"] == "triggered":
        if dd <= spec.reset_threshold:
            st["state"] = "armed"
            st["last_reset_time"] = now
            st["market"] = spec.market
            states[spec.key] = st
            result.update(note="triggered→armed（回撤已修复，重新武装）",
                          extra="下一波回撤再触发时才会再次提醒", mutated=True)
        else:
            result.update(note="triggered（等待修复，不重复推送）")
        return result

    result.update(note="armed（回撤未达触发线）")
    return result


def decide_dry_run(dd: float, spec: MonitorSpec) -> str:
    """--dry-run 模式：只给出假设判断，绝不读写状态文件。"""
    if dd >= spec.dd_threshold:
        return f"⚠️ 触发（dry-run 不推送、不写状态；--push 下若该标的为 armed 会推送一次）"
    if dd <= spec.reset_threshold:
        return "✅ 修复（--push 下若该标的为 triggered 会复位为 armed）"
    return "—  区间内（--push 下维持原状态不变）"


# ══════════════════════════════════════════════════════════════════
# 检测主流程
# ══════════════════════════════════════════════════════════════════
def detect(spec: MonitorSpec) -> dict:
    """拉取K线并计算回撤。返回 {current, ref_high, dd, source, n_used}。"""
    if spec.market == "us":
        df, source = fetch_us(spec.symbol, spec.ref_high)
    else:
        df, source = fetch_a(a_share_code(spec.symbol), spec.ref_high)

    if df is None or len(df) < 2:
        raise RuntimeError("K线数据不足（少于2根），无法计算回撤")

    tail = df.tail(spec.ref_high)
    if len(tail) < spec.ref_high:
        print(f"  ⚠️ {spec.key}: 仅有 {len(tail)} 根K线（不足 {spec.ref_high} 日），"
              f"按现有K线计算基准价")

    ref_high = float(tail["high"].max())
    if ref_high <= 0:
        raise RuntimeError("基准价（N日最高价）为 0 或缺失")
    current = float(df["close"].iloc[-1])   # 现价 = 最新K线收盘价（盘中即最新价）
    if current <= 0:
        raise RuntimeError("现价（最新K线收盘价）缺失或为 0")

    dd = (ref_high - current) / ref_high
    return {"current": current, "ref_high": ref_high, "dd": dd,
            "source": source, "n_used": len(tail)}


def setup_logger() -> logging.Logger:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("deviation")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def acquire_lock(path: Path):
    """进程锁：仅 --push 模式调用，防 cron 并发重复推送/重复写状态。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w", encoding="utf-8")
    try:
        import fcntl  # 仅 Linux/Unix 可用
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            print("❌ 已有另一个 --push 实例正在运行（进程锁被占用），本次退出。", file=sys.stderr)
            sys.exit(3)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except ImportError:
        print("⚠️ 当前系统无 fcntl（非 Linux），跳过进程锁。部署到 Linux cron 后自动生效。")
        return fh


def release_lock(fh) -> None:
    if fh is None:
        return
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def print_config(args: argparse.Namespace, specs: list[MonitorSpec]) -> None:
    """所有配置打印出来供人工核对。"""
    mode = "PUSH（推送微信 + 读写状态文件 + 进程锁）" if args.push else "dry-run（只计算+写日志，不推送、不读不写状态文件）"
    token_ok = bool(getattr(config, "PUSHPLUS_TOKEN", ""))
    print("╔══════════════════ 回撤偏离监控 · 配置核对 ══════════════════╗")
    print(f"  运行模式    : {mode}")
    print(f"  消息模块    : {MODULE_SOURCE}")
    print(f"  状态文件    : {STATE_FILE}（本模式{'读+写' if args.push else '不读不写'}）")
    print(f"  日志文件    : {LOG_FILE}（超过2000行自动轮转）")
    print(f"  微信推送    : {'✅ 已配置 PUSHPLUS_TOKEN' if token_ok else '⚠️ 未配置（.env 无 PUSHPLUS_TOKEN，--push 也不会真发微信）'}")
    print(f"  监控标的    : 共 {len(specs)} 个")
    for sp in specs:
        print(f"    • {sp.key}（{sp.market_name}） N={sp.ref_high}日 "
              f"触发≥{pct(sp.dd_threshold)} 复位≤{pct(sp.reset_threshold)} 现价口径={sp.price_field}")
    print("╚═══════════════════════════════════════════════════════════════╝")


def run(argv=None) -> int:
    args = parse_args(argv)
    specs = build_specs(args)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    logger = setup_logger()
    print_config(args, specs)

    mode = "push" if args.push else "dry-run"
    lock_fh = None
    if args.push:
        lock_fh = acquire_lock(LOCK_FILE)

    try:
        # 只有 --push 才读状态文件；dry-run 完全不碰它
        states = load_state(STATE_FILE) if args.push else None
        state_changed = False
        ok_symbols = fail_symbols = push_ok = push_fail = 0
        segments: list[str] = []
        now = now_iso()

        for spec in specs:
            label = f"{spec.key}[{spec.market_name}]"
            print(f"  ⏳ {label} 正在拉取行情（N={spec.ref_high}日）...", flush=True)
            try:
                det = detect(spec)
            except Exception as e:
                fail_symbols += 1
                msg = f"{label} 数据获取失败: {e}"
                print(f"  ❌ {msg}")
                logger.warning(msg)
                segments.append(f"{spec.key} 数据失败")
                continue
            ok_symbols += 1
            dd = det["dd"]
            print(f"  {label} 现价={det['current']:.2f} 基准={det['ref_high']:.2f} "
                  f"回撤={pct(dd)} 触发线={pct(spec.dd_threshold)} "
                  f"复位线={pct(spec.reset_threshold)} 来源={det['source']}")

            if args.push:
                # 触发推送前，先组装含现价/基准价的完整正文（覆盖 decide 里的占位正文）
                decision = decide_push_mode(spec, dd, det, states, now)
                if decision["attempted"] and decision["pushed"]:
                    push_ok += 1
                    state_changed = True
                elif decision["attempted"]:
                    push_fail += 1
                if decision["mutated"]:
                    state_changed = True
                print(f"      → {decision['note']}")
                if decision["extra"] and decision["pushed"]:
                    print(f"        微信返回: {decision['extra']}")
                elif decision["extra"] and decision["attempted"]:
                    print(f"        {decision['extra']}")
                seg = (f"{spec.key} 回撤={pct(dd)} "
                       f"状态={decision['note'].split('（')[0]} "
                       f"推送={'是' if decision['pushed'] else '否'}")
                segments.append(seg)
                if decision["pushed"]:
                    logger.info(f"📤 已推送微信 {spec.key}: 回撤={pct(dd)} 第{states[spec.key].get('push_count')}次")
                elif decision["attempted"]:
                    logger.warning(f"⚠️ 推送失败 {spec.key}: {decision['extra']}")
            else:
                note = decide_dry_run(dd, spec)
                print(f"      → {note}")
                segments.append(f"{spec.key} 回撤={pct(dd)} 状态=dry-run未读 推送=否")

        # 每次运行写一行汇总日志（dry-run 也写；状态文件只有 --push 才会写）
        summary = f"mode={mode} 检测成功={ok_symbols} 失败={fail_symbols} 推送成功={push_ok} 推送失败={push_fail}"
        logger.info(f"{summary} | " + "; ".join(segments) if segments else summary)

        if args.push:
            if state_changed:
                atomic_write_json(STATE_FILE, states)
                print(f"  💾 状态文件已更新: {STATE_FILE}")
                logger.info(f"💾 状态文件已写入（{len(states)} 个标的状态）")
            else:
                print(f"  💾 状态无变化，本次不重写状态文件: {STATE_FILE}")
            if push_ok == 0 and push_fail == 0:
                print("  🔕 本次无标的达到触发条件，不推送微信")
        else:
            print(f"  ℹ️  dry-run：状态文件未读未写（保持原样）: {STATE_FILE}")

        print(f"══ 运行结束: mode={mode} 检测成功={ok_symbols} 失败={fail_symbols} "
              f"推送成功={push_ok} 推送失败={push_fail} ══")
        return 0 if fail_symbols == 0 else 1
    finally:
        release_lock(lock_fh)
        try:
            rotate_file(str(LOG_FILE), 2000, keep_header=False)  # 防日志无限增长
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(run())
