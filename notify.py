# -*- coding: utf-8 -*-
"""
内嵌消息模块（DeviationMonitor 自包含版）
=========================================
- send(): 推送消息到微信（PushPlus；未配置 token 则静默返回 (False, 原因)）
- rotate_file(): 日志文件超过行数上限时只保留最近的行，防无限增长
本模块为 AutoFolio/scripts/notify.py 的内嵌副本，API 完全一致，
直接读取本工程根目录的 .env（PUSHPLUS_TOKEN），不依赖任何外部项目。
"""
from __future__ import annotations

import os
import sys

# Windows 控制台强制 UTF-8（否则打印 ✅/中文可能 UnicodeEncodeError）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import config  # noqa: E402  内嵌配置（读取本工程 .env）

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def send(title: str, content: str) -> tuple[bool, str]:
    """推送消息到微信（PushPlus）。未配置 token 返回 (False, 原因) 但不算错误。"""
    token = config.PUSHPLUS_TOKEN
    if not token:
        return False, "未配置 PUSHPLUS_TOKEN（.env），跳过推送"
    try:
        import requests
        r = requests.post(PUSHPLUS_URL, json={
            "token": token, "title": title, "content": content, "template": "txt",
        }, timeout=15)
        body = r.json()
        ok = (r.status_code == 200 and body.get("code") == 200)
        return ok, (body.get("msg") or r.text)[:120]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def rotate_file(path: str, max_lines: int = 2000, keep_header: bool = True):
    """文件超过 max_lines 行时，只保留最近的行（header 可选保留首行）"""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    if len(lines) <= max_lines:
        return
    keep = ([lines[0]] + lines[-(max_lines - 1):]) if (keep_header and lines) else lines[-max_lines:]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
        print(f"  🧹 日志轮转: {os.path.basename(path)} {len(lines)} → {len(keep)} 行")
    except Exception:
        pass


if __name__ == "__main__":
    # 测试推送：python notify.py
    import datetime
    token = config.PUSHPLUS_TOKEN
    if not token:
        print("❌ 尚未配置 PUSHPLUS_TOKEN。请编辑本工程 .env 填入 token 后重试。")
        sys.exit(1)
    ok, info = send("DeviationMonitor 测试", f"这是一条测试推送。\n时间: {datetime.datetime.now()}\n若收到此消息，说明微信推送已配置成功。")
    print("✅ 推送成功，请查看微信" if ok else f"❌ 推送失败: {info}")
