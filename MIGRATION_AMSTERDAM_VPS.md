# 迁移到阿姆斯特丹 VPS（<VPS_PUBLIC_IP>）

> 2026-09-19 起草。本文所有 VPS 数据都是**实测**（只读命令：`cat /etc/os-release`、
> `curl -w`、`free`、`ss` 等），没有在 VPS 上安装或修改任何东西。

---

## ✅ 执行结果（2026-09-19 已完成）

bot 已在 VPS 上**实盘运行**：`mode=live`、`armed=True`、`client_ready=True`、`blockers=[]`。

| 指标 | 迁前（本地经代理） | 迁后（阿姆斯特丹） |
|---|---:|---:|
| 探活 client（下单通道，认证 GET） | 406 ms | **40 ms** |
| 探活 rest（公共 REST 兜底） | 786 ms | **28 ms** |
| 探活 gamma（市场数据） | 2227 ms | **28 ms** |
| 读盘口 `/book` 一次完整 HTTPS | ~1170 ms（中位） | **35~56 ms** |
| 签名耗时（`create_limit_order`） | — | **68 ms** |

历史数据完整迁移（137 单 / 127 胜 / 12 负 / 已实现 4.96 USDC），
`trades/**` 逐行解析检查"全部可解析"，日志 0 warning。

**迁移用的可复用脚本**（都在 `tools/`）：
`vps_setup.sh`（幂等装机：apt + 时区 + 用户 + venv + 锁版本依赖）、
`vps_deploy.sh`（部署 + 数据完整性检查 + systemd 单元 + 模式切换 + 体检）、
`restart_bot.sh`（Linux 版重启，等价 `restart_bot.ps1`）。

**还差两件事（需要你操作）**：

1. **杀掉 Windows 上的旧进程**（管理员启动，我这边权限不够）：
   它已经通过面板「全局停止」解除了武装（`armed=False`、`mode=paper` 已落盘，
   重启也不会自动实盘），但进程还活着，建议杀掉后留作冷备：

   ```powershell
   Get-NetTCPConnection -LocalPort 8082 -State Listen | Select-Object -ExpandProperty OwningProcess
   Stop-Process -Id <上面查到的 PID> -Force     # 需要管理员 PowerShell
   ```

2. **改 VPS 密码并确认密钥登录**（那个 root 密码已在聊天里明文出现过）：
   `passwd`，然后确认 `~/.ssh/authorized_keys` 里有 `pmbot-migration` 这把公钥
   （私钥在本地 `<VPS_SSH_KEY_PATH>`，**务必备份**）。
   验证无误后可把 `/etc/ssh/sshd_config` 的 `PasswordAuthentication` 改成 `no`。

**看面板**（浏览器地址和以前一样）：

```powershell
ssh -N -L 8082:127.0.0.1:8082 root@<VPS_PUBLIC_IP>
# 然后打开 http://127.0.0.1:8082
```

**看第一笔 VPS 实盘成交的端到端耗时**：

```bash
ssh root@<VPS_PUBLIC_IP> "cd /opt/pmbot && grep -A1 PRE_ORDER trades/live_order_events.jsonl | tail -40"
```

## 0. 先纠正一个前提：这台机器不是 CentOS

```
PRETTY_NAME="Ubuntu 26.04.1 LTS"
VERSION="26.04.1 LTS (Resolute Raccoon)"
```

所以下面全部用 `apt`，不是 `dnf/yum`。如果当初是按 CentOS 买的镜像，装的东西会不一样，
但**不影响结论**——Ubuntu 26.04 反而更合适（自带 Python 3.14，正好和本地一致）。

## 1. 实测：这台 VPS 值不值得迁

### 1.1 网络延迟（这是迁移的全部理由）

| 目标 | 阿姆斯特丹 VPS | 本地（经代理 127.0.0.1:7897） |
|---|---:|---:|
| `clob.polymarket.com` 一次完整 HTTPS | **35 ~ 47 ms** | **~1170 ms**（读盘口中位） |
| `ws-subscriptions-clob.polymarket.com`（行情流） | 48 ms | 未测（原为抖动源） |
| `sports-api.polymarket.com`（体育实时比分流） | 46 ms | — |
| `relayer-v2.polymarket.com`（下单/赎回） | 47 ms | — |
| `polygon.drpc.org`（RPC） | 77 ms | — |
| `site.api.espn.com`（比分源） | 153 ms | 经常连不上 |

VPS 的 `connect=2.4ms`、`TLS 握手 17ms` —— 说明**直连 Cloudflare 阿姆斯特丹边缘**。
本地到这台 VPS 的 RTT 是 260ms，所以延迟差距不是线路问题，纯粹是**本地到 Polymarket
必须绕代理**造成的。

**结论：网络腿快 20~30 倍。** 按之前实测的链路拆解（读盘口 0.4~1.1s + SDK 元数据
0.4~0.7s + POST 0.4~0.6s），迁移后这三段各能压到几十毫秒。

### 1.2 一个附带好处：连接池问题自动消失

今天 11:24~12:07 那次"43 分钟一单没成交"的根因是 SDK 的
`httpx.Timeout(pool=2.0)` 在代理抖动时拿不到连接（见 PROJECT_DESIGN #89）。
在 35ms 的链路上，`pool=2s` 这个阈值等于形同虚设。

### 1.3 机器规格（够不够用）

| 项 | 实测值 | 判断 |
|---|---|---|
| CPU | **1 vCPU** | 够。bot 是 I/O 密集（等网络），CPU 只在比分计算上 |
| 内存 | **950 MB**（+ 2.4 GB swap 已挂） | 够。本地 bot 只吃 **140 MB / 私有 121 MB** |
| 磁盘 | 23 GB，已用 6.1 GB，**剩 16 GB** | 够。`trades/position_paths.jsonl` 才 22 MB |
| Python | **3.14.4**（`/usr/bin/python3.14`） | 和本地 3.14.0 同大版本，省事 |
| pip / venv | pip 25.1.1，`python3 -m venv` 可用 | 就绪 |
| tzdata | `/usr/share/zoneinfo/Asia/Shanghai` 存在 | 就绪（`zoneinfo` 依赖它） |
| 防火墙 | `ufw` **active**，对外只开 **22** | 好。面板必须走 SSH 隧道 |
| 时区 | `Etc/UTC` | 需要改成 Asia/Shanghai（见 3.3） |
| cloud-init | `status: done` | 可以正常用 |
| 出口 IP 归属 | Amsterdam, NL（Vultr, AS20473） | **非美国**，不触发 Polymarket 地区限制 |

## 2. 迁移前必须先修的三个坑

这三个不解决，迁过去会直接跑不起来。

### 坑 1：`requirements.txt` 缺关键依赖（会装出跑不起来的 venv）

`src/` 里实际 import 的第三方包：

```
aiohttp, python-dotenv, eth-account, httpx, numpy, scipy, polymarket(=polymarket-client)
```

而 `requirements.txt` 现在是：

```
aiohttp>=3.9
python-dotenv>=1.0
py-clob-client>=0.35        ← src 里根本没用到
py-clob-client-v2>=1.1      ← src 里根本没用到
eth-account>=0.13
numpy>=1.26
scipy>=1.13
```

**缺 `polymarket-client`（实盘用的官方 SDK）和 `httpx`**。本地是靠手动装上的。

已在 VPS 上验证可解析（`pip install --dry-run`，exit=0）：

```
polymarket-client==0.9.0 httpx==0.28.1 aiohttp==3.14.3 \
python-dotenv eth-account numpy scipy
```

本地实际版本（照这个锁）：`polymarket-client 0.9.0 / httpx 0.28.1 / aiohttp 3.14.3 /
python-dotenv 1.2.2 / eth-account 0.13.7 / numpy 2.4.6 / scipy 1.17.1 / tzdata 2026.2`。

### 坑 2：代理是硬编码的，VPS 上必须关掉

`src/config.py`：

```python
proxy_url: str | None = "http://127.0.0.1:7897"
```

`LiveTrader._build_client()` 会把它塞进 `HTTP_PROXY/HTTPS_PROXY`。VPS 上没有这个代理，
**不改就是全部请求失败**。（推荐改成环境变量驱动，见第 8 节）

### 坑 3：时区会让日志时间对不上面板

- **交易日/日额度重置不受影响**：`src/trading_day.py` 显式用 `ZoneInfo("Asia/Shanghai")`，
  跟系统时区无关，VPS 上 tzdata 也在。
- **但日志的 `%(asctime)s` 用系统本地时间**。VPS 是 UTC，日志会比面板（北京时间）**早 8 小时**，
  排查问题时极易看错（"11:24 那次故障"在日志里会显示成 03:24）。

→ 建议 `timedatectl set-timezone Asia/Shanghai`，让日志和面板口径一致。

## 3. 迁移步骤

顺序很重要：**同一时间只能有一台在下单**，否则同一个信号会下两笔真单。

### 阶段 A：VPS 初始化（约 10 分钟）

```bash
# A1. 先改密码！这个密码已经在聊天里明文出现过，必须视为已泄露
passwd

# A2. 系统更新 + 装依赖（bot 需要 venv；rsync/git 用于传代码）
apt update && apt -y upgrade
apt -y install python3-venv python3-pip rsync git

# A3. 时区（见坑 3）
timedatectl set-timezone Asia/Shanghai && timedatectl

# A4. 建专用用户（不要用 root 跑交易进程）
useradd -m -s /bin/bash pmbot
mkdir -p /opt/pmbot && chown pmbot:pmbot /opt/pmbot

# A5. 可选但强烈建议：改用 SSH 密钥登录
#     在本地执行 ssh-copy-id pmbot@<VPS_PUBLIC_IP>，然后在 /etc/ssh/sshd_config 里
#     设 PasswordAuthentication no && systemctl restart ssh
```

> ⚠️ ufw 现在只放行 22，是对的。**不要**为了看面板去开 8082 ——
> 面板能切实盘模式、能"全局停止"，暴露到公网等于把账户交出去。用隧道（阶段 F）。

### 阶段 B：传代码（保留目录结构）

本地（Windows）在项目根目录执行，用 `scp` 或 `rsync`（Windows 可用 Git Bash 的 rsync，
或直接 tar + scp）：

```bash
# 建议先打一个只含必要内容的包，避免把 1000+ 个调试脚本和历史日志搬过去
tar --exclude='__pycache__' --exclude='logs' --exclude='.git' \
    --exclude='tmp_py_clob_client' -czf pmbot-src.tar.gz \
    src tools tmp/recalc_paper_cash.py tmp/fix_settlement_prices.py \
    requirements.txt PROJECT_DESIGN.md MIGRATION_AMSTERDAM_VPS.md

scp pmbot-src.tar.gz root@<VPS_PUBLIC_IP>:/tmp/
```

**必须带上的两个脚本**（`tools/restart_bot.ps1` 会在每次重启前跑它们，改账本用）：
`tmp/recalc_paper_cash.py`、`tmp/fix_settlement_prices.py`。
`tmp/` 里其余一千多个 `test_*/probe_*/_*.py` 是历史调试脚本，可以不带。

### 阶段 C：装 Python 依赖

```bash
cd /opt/pmbot
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install \
  'polymarket-client==0.9.0' 'httpx==0.28.1' 'aiohttp==3.14.3' \
  'python-dotenv==1.2.2' 'eth-account==0.13.7' 'numpy==2.4.6' 'scipy==1.17.1' 'tzdata'
# 验证 SDK 能导入（这是最容易漏的一步）
./.venv/bin/python -c "import polymarket, httpx, aiohttp, eth_account; print('deps OK')"
```

### 阶段 D：配置 `.env`

把本地的 `.env` 传过去（`scp .env root@...:/opt/pmbot/.env && chown pmbot: pmbot .env && chmod 600 .env`），
然后改这几处：

| 项 | 本地值 | VPS 上 |
|---|---|---|
| 代理 | `proxy_url` 硬编码 `http://127.0.0.1:7897` | **关掉**（见第 8 节，需先改成环境变量驱动） |
| `LIVE_DRY_RUN` | 0 | **先设 1**（阶段 E 验证用） |
| `SIGNAL_DRILL_MODE` | 0 | 0 |
| `LIVE_EXECUTION_ENABLED` / `LIVE_GLOBAL_ENABLED` / `LIVE_UNATTENDED_ENABLED` | 1 | 先保持 1，但实盘 arm 由面板/`risk_settings.json` 控制 |

### 阶段 E：数据迁移（**必须在本地 bot 停止后做**）

```bash
# 本地：先停 Windows 上的 bot（否则内存里的状态会覆盖你的拷贝）
#   powershell -ExecutionPolicy Bypass -File tools\restart_bot.ps1 -DryRun   # 只是为了拿到 PID
#   然后 Stop-Process，或直接在面板点「全局停止」再杀进程

scp -r trades root@<VPS_PUBLIC_IP>:/opt/pmbot/
```

`trades/` 是**唯一不可再生的资产**，务必全量带走，尤其：

| 文件 | 为什么不能丢 |
|---|---|
| `live_orders.jsonl` / `live_order_events.jsonl` | 实盘成交与重试历史（对账、去重都靠它） |
| `paper_trades.jsonl` / `state.json` | 纸面账本与信号日志 |
| `risk_settings.json` | **实盘 arm 状态、各运动风控参数（含今天的网球 0.86）** |
| `position_paths.jsonl` | 盘口路径采集（影子止损复盘用的数据） |
| `shadow_stop_events.jsonl` | 影子止损记录（丢了就没了） |
| `probability_*` | 概率策略账本 |

校验：`du -sh trades`、`find trades -type f | wc -l` 与本地一致。

### 阶段 F：启动方式（systemd，替代 `restart_bot.ps1`）

`/etc/systemd/system/pmbot.service`：

```ini
[Unit]
Description=Polymarket sweep-lock bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pmbot
WorkingDirectory=/opt/pmbot
Environment=PYTHONUNBUFFERED=1
# 重启前的账本校准（等价 restart_bot.ps1 的 step 2/2b），幂等、失败不阻塞
ExecStartPre=/bin/bash -lc 'cd /opt/pmbot && ./.venv/bin/python tmp/recalc_paper_cash.py --apply || true'
ExecStartPre=/bin/bash -lc 'cd /opt/pmbot && ./.venv/bin/python tmp/fix_settlement_prices.py --apply --ledger paper || true'
ExecStart=/opt/pmbot/.venv/bin/python -m src.main
Restart=always
RestartSec=5
StandardOutput=append:/opt/pmbot/logs/systemd.out.log
StandardError=append:/opt/pmbot/logs/systemd.err.log

[Install]
WantedBy=multi-user.target
```

```bash
mkdir -p /opt/pmbot/logs && chown -R pmbot:pmbot /opt/pmbot/logs
systemctl daemon-reload
systemctl enable --now pmbot
systemctl status pmbot --no-pager
tail -f /opt/pmbot/logs/bot.log        # bot 自己的日志
```

### 阶段 G：面板怎么访问（SSH 隧道）

面板绑的是 `127.0.0.1:8082`，在本地开隧道即可，浏览器里地址不变：

```powershell
ssh -N -L 8082:127.0.0.1:8082 root@<VPS_PUBLIC_IP>
# 然后浏览器打开 http://127.0.0.1:8082 （和现在一模一样）
```

## 4. 上线顺序（关键：不能两台同时实盘）

1. **停本地 bot**（面板「全局停止」→ 杀进程），确认端口 8082 释放。
2. 传 `trades/`（阶段 E）。
3. VPS 上先 **干跑**：`LIVE_DRY_RUN=1` 启动，看 10 分钟日志有没有报错、能不能拿到盘口。
4. 关干跑、**保持纸面/未 arm**，跑 1~2 小时，对比纸面信号与本地历史是否一致。
5. 面板切实盘（需要 `LIVE_MODE_PASSWORD`）、确认右上角变实盘、无红色告警。
6. 观察第一笔真实成交：面板「实盘持仓与结算」出现记录即成功。

## 5. 回滚方案

- 本地代码和 `trades/` **不要删**，当作冷备。
- 回滚前必须先把 VPS 上的 `trades/` 拉回来（否则这段实盘记录会丢），再启本地 bot。
- 任何时刻**只有一台**处于 armed 状态。

## 6. 验收清单

| 检查项 | 期望 |
|---|---|
| `systemctl is-active pmbot` | `active` |
| `/api/live-trader` 的 `blockers` | `[]` |
| 三条探活灯（client/rest/gamma） | 全绿，延迟 ~50ms 级 |
| 读盘口耗时（日志 `httpx ... /book`） | 从 ~1.2s 降到 ~0.05s |
| 下单链路（`PRE_ORDER` → `ORDER_SUBMITTED`） | 从 0.3~1.3s 降到 <0.1s |
| 面板「实盘持仓与结算」 | 有历史记录（说明 `trades/` 带过来了） |
| 影子止损记录条数 | 与本地迁移前一致（不是 0） |
| 日志时间戳 | 北京时间（时区已改） |

## 7. 风险与对策

| 风险 | 说明 | 对策 |
|---|---|---|
| **私钥上云** | `PM_PRIVATE_KEY` 明文存在 VPS 上，等于钱包私钥交给这台机器 | 用**专门的小额钱包**（当前只有 ~$24，可接受）；`chmod 600 .env`；别把 `.env` 放进 git |
| 单点故障 | VPS 挂了就停摆 | systemd `Restart=always`；保留本地为冷备 |
| 1 vCPU | 安装 numpy/scipy 时会吃内存 | swap 已有 2.4G；装完检查 `free -m` |
| 密码已泄露 | 明文出现在聊天里 | **阶段 A1 立刻改密码 + 换密钥登录** |
| 忘关代理 | 全部请求失败 | 阶段 D 显式确认；建议改成环境变量驱动（第 8 节） |
| 忘关本地 bot | 两台同时下单 | 阶段 E 前必须先停本地；上线后 `netstat -ano | findstr 8082` 确认本地无监听 |

## 8. 建议先做的三个代码改动（要不要做，你定）

1. **`proxy_url` 改成环境变量驱动**（`PM_PROXY_URL`，默认 `None`）
   —— 同一份代码在两台机器上都能跑，迁移不用改代码；顺带把"忘了关代理"这个坑消灭。
2. **补齐 `requirements.txt`**：加 `polymarket-client==0.9.0`、`httpx==0.28.1`、
   `tzdata`；并生成 `requirements-lock.txt`（按第 2 节的实锁版本），让 VPS 一键装出同构环境。
3. **加 `tools/restart_bot.sh`**（Linux 版 restart_bot.ps1），或直接依赖 systemd
   （本文阶段 F 已给出 unit）。建议两者都留：unit 负责守护，脚本负责"重启 + 校准账本 + 体检"。

改完我会照例跑测试 + 追加 `PROJECT_DESIGN.md`。
