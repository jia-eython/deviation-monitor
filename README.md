# Deviation Monitor · 回撤提醒

监控美股和 A 股标的相对近期 K 线高点的回撤。达到触发阈值时通过 PushPlus 发送微信提醒，回撤收窄到复位阈值后才重新允许提醒。程序只读取行情。

## 快速开始

需要 Python 3.10 或更新版本。定时推送推荐在 Linux 上运行。

```bash
python -m pip install -r deploy/requirements.txt
cp .env.example .env
python deviation_monitor.py --dry-run --market us --symbols SPY
```

试跑会查询行情并写入本地日志。配置 `.env` 中的 `DEVIATION_SYMBOLS` 和 `PUSHPLUS_TOKEN` 后，用 `python deviation_monitor.py --push` 启用提醒。阈值、混合市场配置和 cron 用法见[详细说明](README_deviation.md)。

`.env` 存放推送和可选的行情凭据。`deploy/create_package.sh` 生成的压缩包会包含真实 `.env`，请仅通过私有渠道部署。
