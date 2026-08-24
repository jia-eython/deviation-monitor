# -*- coding: utf-8 -*-
"""
微牛 Webull OpenAPI 客户端封装（DeviationMonitor 内嵌版）
==============================
- 密钥从根目录 config.py 按环境读取（sandbox 用沙盒key+端点，prod 用实盘key+端点）
- 修正原 标普500.py 中错误的 import 路径（应为 webull.data.data_client.DataClient）
- 提供历史K线拉取（分页，count 上限 1200），解析为 pandas DataFrame
- ⚠️ 本模块只做【行情查询】，不含任何下单逻辑；下单前请确认使用沙盒/模拟账户
"""
from __future__ import annotations

import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))   # 内嵌版：config.py 与本文件同级
sys.path.insert(0, ROOT)

import config  # noqa: E402

# 微牛 OpenAPI SDK（已安装: webull-openapi-python-sdk）
from webull.core.client import ApiClient
from webull.core.common.api_type import DEFAULT as HTTP_API_TYPE
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.data_client import DataClient

# 沙盒（测试）环境端点，见 https://developer.webull.com/apis/docs/sdk/#api-environments
SANDBOX_HOST = "api.sandbox.webull.com"


def load_credentials(environment: str = "sandbox") -> tuple[str, str, str]:
    """按环境从 config.py 读取 app_key / app_secret（region 固定 us）"""
    if environment == "prod":
        app_key, app_secret = config.PROD_APP_KEY, config.PROD_APP_SECRET
    else:
        app_key, app_secret = config.SANDBOX_APP_KEY, config.SANDBOX_APP_SECRET
    if not app_key or not app_secret:
        raise ValueError(
            f"config.py 里 {environment} 环境的密钥为空，请填写 "
            f"{'PROD_APP_KEY/PROD_APP_SECRET' if environment == 'prod' else 'SANDBOX_APP_KEY/SANDBOX_APP_SECRET'}")
    return app_key, app_secret, "us"


class WebullClient:
    def __init__(self, app_key: str | None = None, app_secret: str | None = None,
                 region: str = "us", timeout: int = 60, environment: str | None = None):
        """environment: 默认取 config.ENVIRONMENT；sandbox | prod"""
        if environment is None:
            environment = config.ENVIRONMENT
        if app_key is None:
            app_key, app_secret, region = load_credentials(environment)
        self.app_key = app_key
        self.region = region
        self.environment = environment
        # 脱敏打印（只显示前后 4 位）
        print(f"[webull] 初始化 ApiClient region={region} env={environment} "
              f"app_key={app_key[:4]}...{app_key[-4:] if len(app_key) > 8 else ''}")
        # auto_retry：网络抖动/超时自动重试（最多3次），避免瞬时错误直接崩溃
        self.api_client = ApiClient(app_key, app_secret, region, timeout=timeout,
                                    auto_retry=True, max_retry_num=3)
        # 关闭 SDK 默认日志（否则会在目录里生成 webull_*_sdk.log 并在控制台刷 INFO）
        self.api_client._stream_logger_set = True
        self.api_client._file_logger_set = True
        if environment == "sandbox":
            self.api_client.add_endpoint(region, SANDBOX_HOST, api_type=HTTP_API_TYPE)
            print(f"[webull] 已切换到沙盒端点: {SANDBOX_HOST}")
        else:
            print("[webull] 使用生产端点: api.webull.com")
        self.data = DataClient(self.api_client)

    # ------------------------------------------------------------------
    # 历史K线
    # ------------------------------------------------------------------
    def get_history_bars(self, symbol: str, timespan="D", count=1200,
                         start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
        """拉取历史K线。timespan: D=日线 W=周 M=月；count 上限 1200；可配合 start_ms/end_ms 分页"""
        resp = self.data.market_data.get_history_bar(
            symbol=symbol, category=Category.US_ETF, timespan=timespan,
            count=str(count), start_time=start_ms, end_time=end_ms,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}: {resp.text[:300] if hasattr(resp, 'text') else resp}")
        body = resp.json()
        if body is None:
            raise RuntimeError("响应为空")
        items = body.get("data") if isinstance(body, dict) else body
        if items is None:
            raise RuntimeError(f"响应无 data 字段: {str(body)[:300]}")
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
        df = pd.DataFrame(items)
        if df.empty:
            return df
        # 统一列名
        rename = {
            "close": "close", "open": "open", "high": "high", "low": "low",
            "volume": "volume", "timestamp": "ts", "time": "ts", "date": "date",
            "v": "volume", "o": "open", "h": "high", "l": "low", "c": "close", "t": "ts",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "ts" in df.columns:
            ts = df["ts"]
            if pd.api.types.is_numeric_dtype(ts):
                # 毫秒时间戳
                df["date"] = pd.to_datetime(ts, unit="ms", utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
            else:
                # ISO 字符串（如 2026-08-21T04:00:00.000+0000）
                df["date"] = pd.to_datetime(ts, utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
        elif "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_all_history_bars(self, symbol: str, timespan="D", start_date: str | None = None) -> pd.DataFrame:
        """分页拉取全部历史K线（按 end_time 向前翻页，直到取完或达到页数上限）"""
        import time as _time
        from datetime import datetime, timezone
        end_ms = None
        frames = []
        for page in range(60):  # 最多 60 页（日线约 100+ 年）
            df = self.get_history_bars(symbol, timespan=timespan, count=1200, end_ms=end_ms)
            if df.empty:
                break
            frames.append(df)
            if start_date is not None and df.index.min() <= pd.Timestamp(start_date):
                break
            end_ms = int(df.index.min().tz_localize("America/New_York").timestamp() * 1000) - 1
            _time.sleep(0.6)  # 限速
            if len(df) < 1200:
                break
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames).sort_index()
        out = out[~out.index.duplicated(keep="first")]
        if start_date is not None:
            out = out[out.index >= pd.Timestamp(start_date)]
        return out


if __name__ == "__main__":
    import sys
    print("=== 微牛 Webull OpenAPI 连接测试（沙盒）===")
    client = WebullClient()
    sym = sys.argv[1] if len(sys.argv) > 1 else "QQQ"
    df = client.get_history_bars(sym, count=10)
    print(f"\n{sym} 最近 {len(df)} 根日线：")
    print(df[["open", "high", "low", "close", "volume"]].tail(5).to_string())
    print(f"\n数据范围: {df.index.min()} ~ {df.index.max()}")
