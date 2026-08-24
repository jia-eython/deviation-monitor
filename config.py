# -*- coding: utf-8 -*-
"""
内嵌配置读取器（DeviationMonitor 自包含版）
==========================================
- 从【本工程根目录】的 .env 读取密钥/环境（PUSHPLUS_TOKEN、微牛沙盒密钥、ENVIRONMENT）
- 为 AutoFolio/scripts/config.py 的内嵌子集副本，只保留本监控工程用到的键
- 请勿把真实密钥写死在本文件；改配置请编辑根目录 .env
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    # ① 本工程根目录 .env（自包含部署的唯一配置入口）
    paths = [os.path.join(ROOT, ".env")]
    # ② 兼容本地旧版布局：本工程 .env 缺失时，回退读取同级 ../AutoFolio/.env
    #    （自包含部署时 ../AutoFolio 不存在，自动忽略；先读的 .env 优先）
    paths.append(os.path.join(os.path.dirname(ROOT), "AutoFolio", ".env"))
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                # setdefault：系统环境变量 / 先读到的 .env 优先（服务器可用 export 覆盖）
                os.environ.setdefault(k.strip(), v.strip())


def _bool(key, default=False):
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "on", "y")


_load_env()

# ── 密钥 ──────────────────────────────────────────────────
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
SANDBOX_APP_KEY = os.environ.get("SANDBOX_APP_KEY", "")
SANDBOX_APP_SECRET = os.environ.get("SANDBOX_APP_SECRET", "")
PROD_APP_KEY = os.environ.get("PROD_APP_KEY", "")
PROD_APP_SECRET = os.environ.get("PROD_APP_SECRET", "")

# ── 行情环境（微牛沙盒只读行情）────────────────────────────
ENVIRONMENT = os.environ.get("ENVIRONMENT", "sandbox").strip().lower()
PROD_CONFIRMED = _bool("PROD_CONFIRMED", False)
