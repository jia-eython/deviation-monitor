# 回撤偏离监控 → 微信买入提示（DeviationMonitor）

监控标的价格从近期高点回撤超过阈值 → 通过 PushPlus 推送微信「买入提示」。

**只提示、绝不自动下单。** 只监测 K 线价格回撤，绝不使用账户/持仓/资产净值口径。
行情接口全部只读（微牛 Webull 沙盒 / yfinance / 腾讯直连 / akshare）。

> 本工程位于 `量化/DeviationMonitor/`，**内嵌消息/配置/行情模块**（`notify.py`、`config.py`、`webull_client.py`），
> 自包含、开箱即用，服务器一键部署不依赖任何外部项目（`../AutoFolio` 仅作本地旧布局兼容回退）。

---

## 一、目录结构

| 文件 | 用途 |
|---|---|
| `deviation_monitor.py` | 主监控脚本：K线回撤检测、复位式状态机、dry-run/push 区分、进程锁、日志 |
| `notify.py` | **内嵌消息模块**：PushPlus 推微信（send）+ 日志轮转（rotate_file），自包含 |
| `config.py` | **内嵌配置**：读取本工程 `.env`（密钥/环境），缺失时兼容回退 `../AutoFolio/.env` |
| `webull_client.py` | **内嵌行情模块**：微牛沙盒只读行情（懒加载，A股模式无需 SDK） |
| `.env.example` | 本工程配置示例（复制为 `.env` 使用；密钥直接配在本工程） |
| `crontab.example` | Linux 定时任务示例（每 2 小时检测 + 每日 dry-run 演练） |
| `deploy/install_deviation_monitor.sh` | Linux 一键部署脚本（Alibaba Cloud Linux 3 等） |
| `deploy/create_package.sh` | 本地打包脚本，生成 `deviation_monitor_linux_deploy.tar.gz` |
| `deploy/requirements.txt` | 部署依赖清单（pandas/requests/yfinance/akshare） |
| `README_deviation.md` | 本文档 |
| `results/deviation_state.json` | 复位式去重状态文件（**仅 `--push` 读写**，运行时自动生成） |
| `results/deviation.log` | 每次运行一行日志（dry-run 也写，超 2000 行自动轮转） |
| `results/deviation_monitor.lock` | 进程锁文件（**仅 `--push` 生效**） |

---

## 二、核心逻辑：K 线价格回撤偏离（唯一检测项）

对每个监控标的，**只用该标的自己的 K 线**计算：

| 概念 | 定义 | 默认 |
|---|---|---|
| 基准价 | 近 **N 日 K 线最高价**（`--ref-high` / `DEVIATION_REF_HIGH`） | 20 日 |
| 现价 | 最新 K 线收盘价（日线最后一根，盘中即最新价；`DEVIATION_PRICE_FIELD` 可配） | close |
| 回撤幅度 | `(基准价 - 现价) / 基准价` | — |
| 触发阈值 | 回撤 ≥ 该值 → 「回撤触发」（`--dd-threshold` / `DEVIATION_DD_THRESHOLD`） | 0.05（5%） |
| 复位阈值 | 回撤 ≤ 该值 → 「回撤已修复」（`--reset-threshold` / `DEVIATION_RESET_THRESHOLD`） | 0.03（3%） |

所有回撤百分比打印/推送均保留 **2 位小数**。

---

## 三、复位式去重（状态机，每标的一个 channel 互不影响）

```
        ┌───────────────────────────────┐
        │          armed（未触发）        │
        │      回撤 < 触发阈值            │
        └──────────────┬────────────────┘
                       │ 回撤 ≥ 触发阈值(5%)
                       ▼   → 推送微信【一次】
        ┌───────────────────────────────┐
        │        triggered（已触发）      │
        │  本波已提醒，等待修复            │
        │  回撤继续变大/持续多轮：不重复推送 │
        └──────────────┬────────────────┘
                       │ 回撤 ≤ 复位阈值(3%)
                       ▼   → 重新武装（不推送）
              （回到 armed，下一波触发才再提醒）
```

- **每波回撤只提醒一次**；必须等价格反弹、回撤收窄到复位线以内，才允许下一波再提醒。
- 状态文件 `results/deviation_state.json` 结构示例：

```json
{
  "SPY": {
    "state": "triggered",
    "last_trigger_dd": 0.0621,
    "last_push_time": "2026-08-23T10:00:00+08:00",
    "last_reset_time": null,
    "push_count": 2,
    "market": "us"
  },
  "sh600519": {
    "state": "armed",
    "last_trigger_dd": 0.0480,
    "last_push_time": "2026-07-01T09:35:00+08:00",
    "last_reset_time": "2026-07-15T09:35:00+08:00",
    "push_count": 1,
    "market": "a"
  }
}
```

- **首次运行**：状态文件中没有该标的 → 默认 `armed`。
- **状态文件损坏 / 字段缺失**：自动按 `armed` 重建（保留有效的历史提醒计数），程序不崩溃。
- **推送失败**（网络/未配置 token）：状态保持 `armed`，下一轮自动重试，不会因失败而漏掉提醒。
- 「本轮为第 X 次提醒（复位式）」中的 X = 该标的自状态文件建立以来的累计触发轮数（`push_count`）。

时间线示例（N=20、触发 5%、复位 3%）：

| 时点 | 回撤 | 状态变化 | 是否推送 |
|---|---|---|---|
| 周一 | 4.2% | armed，未达线 | 否 |
| 周二 | 6.2% | armed → **triggered** | ✅ 第 1 次提醒 |
| 周三 | 8.5% | 仍 triggered | ❌ 不重复推 |
| 周四 | 4.0% | 仍 triggered（>3% 未修复） | ❌ |
| 周五 | 2.8% | triggered → **armed**（复位） | 否（复位不推） |
| 下周三 | 5.5% | armed → **triggered** | ✅ 第 2 次提醒 |

---

## 四、dry-run 与 --push 的区别（重要）

| 行为 | `--dry-run`（**默认**） | `--push` |
|---|---|---|
| 拉取行情、计算回撤 | ✅ | ✅ |
| 打印检测结果 | ✅ | ✅ |
| 写日志 `results/deviation.log` | ✅ | ✅ |
| 推送微信 | ❌ 绝不推送 | ✅ 仅在触发时推送 |
| **读状态文件** | ❌ **不读** | ✅ |
| **写状态文件** | ❌ **不写**（保持原样） | ✅ 状态变化时原子写入 |
| 进程锁 | ❌ 不加锁 | ✅ 加锁防并发 |

> **dry-run 是「只读探测」**：试配置、验行情时随便跑，绝不会把状态写成 triggered，
> 也不会污染正式的去重记录。**`--push` 是唯一会「更新状态 + 推送」的入口。**

---

## 五、怎么配 .env

```bash
cd 量化/DeviationMonitor
cp .env.example .env      # 然后编辑 .env
```

最少只需要一行：

```ini
DEVIATION_SYMBOLS=SPY,QQQ,600519
```

其余全部有默认值（N=20 日、触发 5%、复位 3%、市场 us）。要监控 A 股记得加：

```ini
DEVIATION_MARKETS=600519:a
```

**标的也支持混合市场前缀写法**（更直观，`DEVIATION_MARKETS` 可省略）：

```ini
DEVIATION_SYMBOLS=us:ASHR,us:FXI,a:510900,a:512890,a:512800,a:561580,a:510300
```

密钥类配置（`PUSHPLUS_TOKEN`、微牛沙盒 `SANDBOX_APP_KEY/SECRET`、`ENVIRONMENT`）**直接写在本工程 `.env`** 即可，
内嵌模块会自动读取。若某项缺失，会尝试回退读取同级 `../AutoFolio/.env`（本地旧布局兼容，自包含部署时该目录不存在则自动忽略）。

> 优先级：命令行参数 > 本工程 .env（单标的覆盖 > 全局） > 内置默认。

---

## 六、怎么手动跑

Windows 用 `python`，Linux 用 `python3`（一键部署后请用工程内 `.venv/bin/python`）。

```bash
# ① 纯计算演练（默认 dry-run：不推送、不读不写状态文件）—— 推荐先跑这个
python deviation_monitor.py --symbols SPY,AAPL --market us

# ② A股演练（600519 自动加 sh 前缀；腾讯直连优先，失败回退 akshare）
python deviation_monitor.py --symbols 600519,000001 --market a

# ③ 覆盖参数演练：30 日窗口、触发 6%、复位 3.5%
python deviation_monitor.py --symbols SPY --ref-high 30 --dd-threshold 0.06 --reset-threshold 0.035

# ④ 真正推送（只在触发时推微信；读写状态文件；加进程锁）
python deviation_monitor.py --symbols SPY,QQQ --push

# ⑤ 不传 --symbols 时读 .env 的 DEVIATION_SYMBOLS（生产 cron 用法）
python deviation_monitor.py --push
```

运行时会先打印完整配置供人工核对，例如：

```text
╔══════════════════ 回撤偏离监控 · 配置核对 ══════════════════╗
  运行模式    : dry-run（只计算+写日志，不推送、不读不写状态文件）
  微信推送    : ✅ 已配置 PUSHPLUS_TOKEN
  监控标的    : 共 2 个
    • SPY（美股） N=20日 触发≥5.00% 复位≤3.00% 现价口径=close
    • sh600519（A股） N=20日 触发≥5.00% 复位≤3.00% 现价口径=close
```

---

## 七、怎么装 cron（Linux）

```bash
# 1. 进入本工程，记下真实路径
cd 量化/DeviationMonitor && pwd        # 例如 /home/you/量化/DeviationMonitor

# 2. 确认 Python 环境（一键部署后优先用工程内 venv）
ls .venv/bin/python 2>/dev/null || which python3

# 3. 打开 crontab，把 crontab.example 的内容（改好路径）粘进去
crontab -e

# 4. 验证已安装 & 看日志
crontab -l
tail -f results/deviation.log
```

核心一行（每 2 小时）：

```cron
0 */2 * * * cd /home/you/量化/DeviationMonitor && /usr/bin/python3 deviation_monitor.py --push >> results/cron_deviation.log 2>&1
```

- 程序**单次运行即退出**（检测一次、推一次），天然适配 cron 每 X 小时调度。
- `--push` 内置进程锁：即使 cron 重叠调度也不会重复推送/重复写状态。
- 建议再加一条每日 dry-run 演练（不推送、不碰状态文件），用于验证行情源与配置正常。

---

## 八、怎么验证微信推送通不通

直接测内嵌消息模块（会真发一条到微信）：

```bash
python notify.py            # 本机开发
.venv/bin/python notify.py  # 一键部署后
```

- 收到消息 → 推送链路 OK。
- 没收到且提示未配置 → 去本工程 `.env` 填 `PUSHPLUS_TOKEN`（PushPlus：https://www.pushplus.plus）。
- `notify.send` 未配置 token 时静默返回 `(False, 原因)`，**不会报错中断监控**。

推送文案示例（PushPlus 纯文本）：

```text
标题: [买入提示] SPY 回撤 6.21%
正文: 【标的】SPY（美股）
      【现价】512.34
      【基准价】546.27（近20日K线最高价）
      【回撤幅度】6.21%（触发线 5.00% / 复位线 3.00%）
      【建议】可分批买入（仅价格回撤提示，非投资建议）
      【提醒】本轮为第 1 次提醒（复位式）
      【检测时间】2026-08-23 10:00:00
      ——
      说明：本提示仅基于K线价格回撤生成，监控程序绝不自动下单。
```

无触发则**不推送**（只在日志/控制台留一行）。

---

## 九、数据源与常见问题

| 市场 | 优先 | 回退 |
|---|---|---|
| 美股 | `webull_client.WebullClient`（沙盒，只读行情） | `yfinance` |
| A股 | 腾讯直连 `web.ifzq.gtimg.cn/.../fqkline/get?...qfq`（列序 date/open/close/high/low/volume） | `akshare`（东方财富 → 新浪） |

常见问题：

1. **Webull 报错/密钥缺失** → 自动回退 yfinance（无需处理；yfinance 偶发 429 限流，稍后再试即可）。
2. **腾讯直连失败** → 自动回退 akshare；东财被网络拦截时会再自动切新浪。
3. **复位阈值 ≥ 触发阈值** → 程序直接报错退出（否则会退化成每轮都推送）。必须 `0 ≤ 复位阈值 < 触发阈值`。
4. **A股代码规则**：6/9/5 开头→sh（含 5 开头沪市基金/ETF）；0/1/2/3 开头→sz；4/8 开头→bj。也接受 `sh600519` 这种带前缀写法。
5. **提示"已有另一个 --push 实例在运行"** → 进程锁生效（上一个实例还没跑完），等它结束即可；dry-run 不受影响。
6. **Windows 上提示无 fcntl** → 正常（锁只在 Linux cron 生效），Windows 只是本地调试。
7. **中文乱码** → 脚本已强制 UTF-8；如仍乱码加环境变量 `PYTHONIOENCODING=utf-8`。
8. **K线不足 N 日** → 会按现有K线计算基准价并在输出中警告（新上市标的正常现象）。

---

## 十、Linux 一键部署（Alibaba Cloud Linux 3）

已内置一键部署包：`deviation_monitor_linux_deploy.tar.gz`（本工程内直接生成，也可用
`bash deploy/create_package.sh` 重新生成；包含内嵌消息/配置/行情模块、微牛 SDK wheel、安装脚本）。

**服务器部署步骤（3 条命令）：**

```bash
# ① 本机把包传到服务器（换成你的服务器 IP）
scp deviation_monitor_linux_deploy.tar.gz root@你的服务器IP:~/

# ② 登录服务器
ssh root@你的服务器IP

# ③ 解压并一键安装（部署包已自带预填好的 .env）
tar xzf deviation_monitor_linux_deploy.tar.gz
cd DeviationMonitor
vim .env                          # 可选：核对/修改预填的 token 与标的（已填好，不改也行）
bash deploy/install_deviation_monitor.sh   # 装 python38→venv→依赖→dry-run 验证→可选 crontab
```

> 部署包内已包含**预填好的 `.env`**（PUSHPLUS_TOKEN、标的、市场、阈值都按《投资操作手册》V2 填好），
> 直接安装即可；**安装脚本绝不询问、绝不改写 `.env`**，以后要改配置直接 `vim .env`。

**重新部署（之前装过）**：新包自带 `.env`，直接解压会覆盖你改过的 `.env`。
如果你改过 `.env`，用下面的命令跳过它：

```bash
# 方式 A（推荐）：解压时保留你现有的 .env
tar xzf deviation_monitor_linux_deploy.tar.gz --exclude=DeviationMonitor/.env

# 方式 B：先备份再解压
cp .env .env.bak
tar xzf deviation_monitor_linux_deploy.tar.gz

cd DeviationMonitor && bash deploy/install_deviation_monitor.sh   # 幂等：venv/状态/crontab 都安全
```

**脚本会自动做的事：**

| 步骤 | 说明 |
|---|---|
| 系统检测 | 识别 Alibaba Cloud Linux 3（dnf 系），兼容 RHEL8/CentOS8/Ubuntu |
| Python | 未找到 3.8+ 时自动 `dnf install python38 python38-pip python38-devel` |
| 虚拟环境 | 创建 `.venv`（venv 不可用则直接使用系统 Python） |
| 依赖 | pip 安装 pandas/requests/yfinance/akshare + 内附的微牛 SDK wheel |
| 配置 | **不询问、不改写**：只读校验 `.env` 是否已有 `DEVIATION_SYMBOLS`，缺失则提示你编辑 |
| 验证 | 自动跑一次 `--dry-run`（读取你 .env 里的标的，不推送、不碰状态文件） |
| crontab | 询问是否安装「每 2 小时整点 `--push`」（`AUTO_INSTALL_CRON=1` 可免交互安装） |
| 微信测试 | 询问是否发一条测试消息（`TEST_PUSH=1` 可免交互测试） |

**典型流程（配置归你，安装归脚本）：**

```bash
# 1) 只跑一次：先装环境（此时 .env 不存在，脚本生成模板后退出）
bash deploy/install_deviation_monitor.sh

# 2) 手动填配置（token/标的/市场/阈值）
vim .env

# 3) 再跑一次：脚本完成依赖校验 + dry-run + 可选 crontab/测试推送
bash deploy/install_deviation_monitor.sh
```

> Alibaba Cloud Linux 3 默认 `python3` 是 3.6，脚本会自动通过 dnf 安装 python38；
> 若你已装 3.8+（如 python3.11），脚本会直接复用，不重复安装。

---

## 十一、安全边界（硬性）

1. **只提示，绝不自动下单**——本程序没有任何下单/资金接口。
2. 默认 `--dry-run`，显式 `--push` 才推微信、才读写状态文件。
3. 行情接口全部只读（Webull 沙盒 / yfinance / 腾讯 / akshare），绝不触达账户/交易接口。
4. 所有回撤百分比保留 2 位小数。
