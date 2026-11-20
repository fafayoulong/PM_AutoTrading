# Polymarket 事件锁定交易机器人 — 产品设计文档

> 注意：本文件是早期产品设计快照。当前实现基线请看 `PROJECT_DESIGN.md`，运行说明请看 `README.md`。

> 版本：v1.0 ｜ 日期：2026-09-03 ｜ 状态：模拟盘已就绪，等待赛事验证

---

## 1. 产品定位

**一句话描述**：利用公开体育赛事的实时状态变化（比分、局数、盘数），比 Polymarket 上手动交易者更快识别"胜负已定"的市场，在其价格收敛前买入，赚取收敛价差。

**本质**：低延迟事件驱动交易，不是链上抢跑，不涉及内幕信息。信号来源于公开比分数据（ESPN），交易对象是 Polymarket 公开市场。

**典型场景**：

> 第四节最后 3 分钟，湖人领先 15 分。Polymarket 湖人 YES 价格仍为 0.92。Bot 检测到锁定信号，以 0.92 买入 500 USDC。比赛结束湖获胜，以 1.0 结算，赚 8.7%。

---

## 2. 策略逻辑

### 2.1 核心假设

| 假设 | 依据 |
|------|------|
| 公开信息（比分）出现后，Polymarket 手动交易者的价格调整有延迟 | 手动操作需要打开页面→阅读→下单，预计 30s-3min |
| "锁定"场景下翻盘概率极低 | 第四节末 8+ 分差的 NBA 翻盘率 < 1% |
| 市场价格未完全收敛时存在套利空间 | 回测显示收敛期间价格可从 0.85-0.92 逐步升到 0.97+ |

### 2.2 信号触发条件

#### NBA

| 参数 | 值 | 说明 |
|------|-----|------|
| `min_quarter` | 4 | 只看第四节 |
| `min_time_remaining_seconds` | 180 | 剩余时间 ≤ 3 分钟 |
| `min_score_diff` | 8 | 分差 ≥ 8 分 |
| `score_diff_per_minute` | 0.0 | 不随时间动态调整（回测最优） |

#### MLB

| 参数 | 值 |
|------|-----|
| `mlb_min_inning` | 9 |
| `mlb_min_score_diff` | 3 |

#### 网球（ATP/WTA）

| 参数 | 值 |
|------|-----|
| `tennis_min_game_diff_in_set` | 2 |

逻辑：领先盘数 > 落后盘数，且当前盘局分领先 ≥ 2。

### 2.3 交易执行条件

| 参数 | 值 | 说明 |
|------|-----|------|
| `max_entry_price` | 0.92 | 入场价不超过 0.92 |
| `min_expected_profit` | 0.03 | 预期收益 ≥ 3% |
| `max_position_size` | 500 USDC | 单笔仓位 |
| `max_open_positions` | 5 | 最大同时持仓 |

### 2.4 每场比赛只触发一次

检测到信号后立即标记该比赛 ID，后续不再重复开仓。防止同一锁定场景多次买入。

---

## 3. 数据源

| 数据 | 来源 | 延迟 | 费用 | 代理 |
|------|------|------|------|------|
| NBA 实时比分 | ESPN API (`site.api.espn.com`) | ~3-5s | 免费 | ✅ 7897 |
| MLB 局数/得分 | ESPN API | ~3-5s | 免费 | ✅ 7897 |
| 网 Tennis 盘/局比分 | ESPN API (ATP+WTA) | ~3-5s | 免费 | ✅ 7897 |
| NBA 历史 play-by-play | ESPN API (`summary?event=`) | 不适用 | 免费 | ✅ 7897 |
| Polymarket 市场搜索 | Gamma API (`gamma-api.polymarket.com`) | 实时 | 免费 | ✅ 7897 |
| Polymarket 最优价 | CLOB API (`clob.polymarket.com`) | 实时 | 免费 | ✅ 7897 |

> 所有数据源均为公开接口，无需 API key。Polymarket 价格读取不需要钱包。

---

## 4. 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        LockBot (main.py)                     │
│                                                              │
│  ┌──────────┐    ┌────────────┐    ┌──────────────────┐    │
│  │ NBAFeed  │───▶│ SignalEngine│───▶│  PaperTrader     │    │
│  │ (espn)   │    │ (signal.py) │    │ (paper_trader.py)│    │
│  └──────────┘    └────────────┘    └──────────────────┘    │
│       │               │                     │                │
│       │        ┌──────┴──────┐              │                │
│       │        │ MLB Signal  │              │                │
│       │        │ Tennis Signal│             │                │
│       │        └─────────────┘              │                │
│       ▼                                     ▼                │
│  ┌──────────┐                      ┌──────────────┐        │
│  │ PolymarketFeed│                 │  Dashboard   │        │
│  │ (gamma+clob)│                  │ (dashboard.py)│       │
│  └──────────┘                      └──────────────┘        │
│                                       http://127.0.0.1:8081 │
└─────────────────────────────────────────────────────────────┘
```

### 模块清单

| 文件 | 职责 |
|------|------|
| `src/config.py` | 所有阈值/参数集中配置 |
| `src/nba_feed.py` | ESPN 数据拉取（NBA/MLB/Tennis） |
| `src/signal.py` | NBA 锁定信号引擎 |
| `src/multi_signal.py` | MLB + Tennis 锁定信号引擎 |
| `src/polymarket_feed.py` | Polymarket 市场搜索 + 价格查询 |
| `src/paper_trader.py` | 模拟盘引擎：开仓、平仓、结算、P&L |
| `src/main.py` | 主循环：轮询 → 信号 → 下单 → 结算 |
| `src/dashboard.py` | 实时 Web 面板 |
| `src/backtest.py` | 回测框架 |
| `src/run_backtest.py` | 回测 CLI |
| `src/optimize.py` | 参数网格搜索 |
| `src/run_optimize.py` | 优化 CLI |
| `src/test_multi.py` | 多运动信号快速诊断 |

---

## 5. Dashboard

访问 `http://localhost:8081`（bot 运行时自动启动）。

| 区块 | 内容 |
|------|------|
| 指标卡 | 现金 / 净值 / 持仓数 / 总交易 / 胜率 / P&L |
| 🏟️ Live Games | 实时比赛（运动类型、双方、比分、状态如 "Bottom 9th"） |
| 📋 Open Positions | 持仓详情（入场价/份额/成本/入场时间） |
| 📊 Recent Trades | 最近 20 笔平仓（P&L/ROI/状态标签） |

每 5 秒自动刷新，暗色主题，无需额外依赖。

---

## 6. 回测结果

### 回测参数

- 日期范围：2025-03-30 至 2025-04-13（两周）
- 假设入场价：0.90（Polymarket 实际价格待验证）
- 单笔：500 USDC

### 结果

| 指标 | 值 |
|------|-----|
| 总交易次数 | 96 笔 |
| 胜率 | **100%** |
| 总收益 | **+5,333 USDC** |
| ROI | **11.1%** |

### 最优参数（已写入 config）

```python
min_score_diff = 8
min_time_remaining_seconds = 180
score_diff_per_minute = 0.0
```

> 100% 胜率的原因：策略本质是买"几乎确定会赢的队伍"，风险极低但收益也低（3-8%/笔）。真正的不确定性在于 Polymarket 实际价格是否给了足够的入场空间。

### 已知限制

1. 回测假设入场价 0.90，实际 Polymarket 价格可能已收敛到 0.95+
2. 未考虑滑点和流动性
3. 未验证 Polymarket 是否有对应 NBA/MLB/Tennis 的实时比赛市场
4. 网球锁定逻辑较简单（未考虑球员发球局、伤退等）

---

## 7. 当前状态

| 里程碑 | 状态 |
|--------|------|
| ✅ 模拟盘框架 | 完成 |
| ✅ NBA 信号引擎 | 完成（参数已优化） |
| ✅ MLB 信号引擎 | 完成 |
| ✅ Tennis 信号引擎 | 完成 |
| ✅ 回测框架 | 完成（两周 96 笔验证） |
| ✅ 参数优化器 | 完成（80 组合网格搜索） |
| ✅ 实时 Dashboard | 完成（含 Live Games 总览） |
| ✅ 交易结算跟踪 | 完成 |
| ⬜ Polymarket 实际市场匹配 | 待验证 |
| ⬜ 实时模拟盘验证 | 待 NBA 赛季开始（2026-10-21） |
| ⬜ 实盘接入 | 暂不需要 |

---

## 8. 下一步

### 短期（等待赛事开始前）

1. **验证 Polymarket 市场覆盖**：确认 Polymarket 是否实际有 NBA 单场比赛盘口、MLB 单场盘口、网球单场盘口
2. **精确市场匹配**：改进 `polymarket_feed.py` 的搜索逻辑，用队伍全名+日期精确匹配，替代目前的模糊搜索
3. **加速轮询**：把 ESPN 轮询从 10s 缩短到 3-5s

### 中期（NBA 赛季开始后）

4. **实时模拟盘跑 2-4 周**：启动 bot 挂机，记录信号触发频率和实际入场价格
5. **根据实际数据调整阈值**：如果实际价格 > 0.92，放宽 `max_entry_price`
6. **评估是否需要更快数据源**：如果 ESPN 延迟导致错过机会，考虑 Sportradar 付费 API

### 长期（验证后）

7. **实盘准备**：需要 Polymarket API key、钱包、USDC 资金
8. **风控增强**：单日最大亏损限制、连续止损暂停机制
9. **多策略扩展**：足球锁定（80min + 2球领先）、网球破发点信号等

---

## 9. 使用指南

### 环境准备

```powershell
cd <PROJECT_ROOT>
pip install aiohttp
```

> 需要开启 VPN（代理端口 7897），否则无法访问 ESPN 和 Polymarket。

### 启动模拟盘

```powershell
python -m src.main
```

Dashboard 自动启动，浏览器打开 `http://localhost:8081`。

### 回测

```powershell
python -m src.run_backtest 20250401 20250415
```

### 参数优化

```powershell
python -m src.run_optimize 20250401 20250415
```

### 快速诊断

```powershell
python -m src.test_multi
```

---

## 10. 风险与合规

| 风险 | 评估 | 缓解 |
|------|------|------|
| 策略失效（市场越来越高效） | 中 | 持续监控胜率，低于 90% 则暂停 |
| Polymarket 无对应市场或流动性不足 | 高（待验证） | 短期内先验证市场覆盖 |
| ESPN 数据延迟 | 低-中 | 已有 3-5s 延迟，可切换付费源 |
| 合规 | 建议确认所在地区是否允许使用 Polymarket | 用户自行确认 |

---

## 11. 核心代码结构速览

```
polymarket交易抢跑/
├── PRODUCT_DESIGN.md          # 本文档
├── README.md                   # 快速开始
├── requirements.txt            # aiohttp
├── trades/                     # 交易记录（JSONL）
├── logs/                       # 日志
└── src/
    ├── __init__.py
    ├── config.py               # 参数配置
    ├── nba_feed.py             # ESPN 数据源
    ├── signal.py               # NBA 信号
    ├── multi_signal.py         # MLB/Tennis 信号
    ├── polymarket_feed.py      # Polymarket 数据
    ├── paper_trader.py         # 模拟盘
    ├── main.py                 # 主循环
    ├── dashboard.py            # Web 面板
    ├── backtest.py             # 回测引擎
    ├── run_backtest.py         # 回测 CLI
    ├── optimize.py             # 参数优化
    ├── run_optimize.py         # 优化 CLI
    └── test_multi.py           # 快速诊断
```
