# Polymarket 体育扫尾盘交易系统

监控 NBA、MLB 和网球的终局锁定信号，支持模拟盘与小额实盘限价执行。

## 运行

```powershell
pip install -r requirements.txt
python -m src.main
```

启动后访问 `http://127.0.0.1:8082`。

概率盘独立监控页：`http://127.0.0.1:8082/probability`。概率事件带有 `event_id` 和北京时间，并能单独查看持仓、结算和分运动统计。

风控参数在 `http://127.0.0.1:8082/settings` 独立维护；首页只做监控和实盘执行控制。NBA、MLB、Tennis 可分别启用，并分别配置模拟仓位/持仓与实盘单笔、日投入、日亏损、订单超时、最小下单和盘口异常限制。设置会持久化到 `trades/risk_settings.json`，旧版扁平配置会在启动时自动迁移。

## 阶段 1：实盘只读验证

1. 复制 `.env.example` 为 `.env`。
2. 填写 `PM_WALLET_ADDRESS`、`PM_API_KEY`、`PM_API_SECRET`、`PM_API_PASSPHRASE`。
3. 启动服务后，面板会显示实盘账户只读监控。
4. 系统会查询钱包 USDC 余额、实盘持仓、订单和成交记录，并记录权益快照。

密钥只从 `.env` 或环境变量读取，不会写入源码，也不会出现在面板 API 返回中。

## 阶段 2：小额实盘限价执行

1. 默认面板模式仍是 `paper`；`readonly` 只做账户验证，`live` 需要双因子确认。
2. 在 `.env` 中配置 `LIVE_EXECUTION_ENABLED=1` 和强随机 `LIVE_MODE_PASSWORD` 后，面板才会显示可用的实盘启用入口。
3. 启用实盘必须输入 `LIVE_MODE_PASSWORD`，并输入确认短语 `启用实盘`。
4. 执行器只会对风控设置页中启用且属于 `LIVE_ALLOWED_SPORTS` 的市场发限价单；保存设置页后，允许列表会同步为已启用运动。
5. 下单前先写 `trades/live_order_events.jsonl` 的 `PRE_ORDER` 审计事件，再写入请求、回报、撤单、成交和失败事件。
6. 订单按盘口可用深度截量，并遵守对应运动的单笔、持仓、当日投入和超时限制。
7. “全局停止”会撤掉活动订单、关闭面板 arm 状态，并在重启前阻止再次启用。
8. 模拟盘继续并行运行，不因实盘模式切换而关闭。
9. 面板实盘确认会保存到 `trades/risk_settings.json`。服务重启后，若全局开关、执行开关、密码、客户端和熔断状态仍满足安全条件，会自动恢复 live 模式；若执行器未就绪或熔断未清零，则回退到 `paper`。

## 阶段 3：无人值守与硬风控

1. `LIVE_GLOBAL_ENABLED=0` 是总闸。只有总闸、执行开关、无人值守开关、面板 `live` 模式和密码确认同时通过，才会自动下单。
2. 全局与每个运动的日亏损熔断会分别生效；触发后停止对应新订单、撤掉活动订单，并在状态中显示 `DAILY_LOSS`。
3. 盘口异常会自动暂停：单边盘、交叉盘、价差过大、盘口过期或深度为空。
4. 每个市场在下单网络请求前先写入去重标记；即使进程在下单途中崩溃，重启后也不会对同一 token 重复下单。
5. 状态、持仓、订单和已实现盈亏都会从 `trades/live_orders.jsonl` 恢复。
6. 面板会显示全局开关、无人值守开关、日盈亏、熔断状态和异常暂停原因。

## 策略逻辑

1. 每 3 秒从数据源拉取正在进行的 NBA、WNBA、MLB、网球和 CS2 比赛
   （MLB 走官方 StatsAPI，CS2 走 Polymarket 实时流）。
2. 检测终局锁定条件；MLB / NBA / WNBA / NFL / CFB / NHL / TENNIS 若 Polymarket 实时流已与该场比赛
   比分对齐，则改用实时流推送的比分并即时触发（见「组合数据源」）。
3. 匹配 Polymarket 胜负盘并读取 `best_ask`。
4. 满足入场价格和理论收益条件后，模拟或实盘买入领先方 YES。
5. 比赛结束后按胜负自动结算面板持仓。

## 组合数据源（PM 实时流 + 轮询源）

1. Polymarket 自带的体育实时流是推送（百毫秒级），轮询源（ESPN / StatsAPI）负责
   冷启动快照、口径校验与历史回填。
2. 覆盖层只覆盖**比分与更新时间**；局数、剩余时钟、gameId 仍以轮询源为准。
3. 只有当 PM 比分与轮询源比分**逐队完全一致**过一次后才开始覆盖，队名映射或
   比分格式识别错误时永远不会覆盖（退化为纯轮询，不会用错数据）。
4. 相关配置：`PM_STREAM_OVERLAY`、`PM_STREAM_OVERRIDE_SPORTS`、`PM_STREAM_STALE_SECONDS`。
5. 面板「📡 数据源状态」新增「PM 实时流」列，显示每个运动的覆盖 / 校验状态。

## 同步与审计

1. 虚拟盘成功开仓后先写入比赛级去重标记，再调用实盘执行；实盘异常不会导致同一比赛重复开虚拟仓。
2. 实盘成功、拒绝和异常都会写入日志；拒绝原因同时写入 `trades/live_order_events.jsonl`。
4. 实盘提交入口写入 `LIVE_SUBMIT_ENTRY`；没有入口事件即表示实盘调用未到达，需要重启加载新版代码。
5. 实盘下单数量会在不超过单笔上限和可成交深度的前提下，自动调整到满足最小订单金额。
6. 实盘当日投入同时受全局上限和分运动上限约束，执行时取更严格值。
7. 网球信号自动兼容 `match_id` 与 `game_id`，避免实盘执行链路异常。
3. 已完赛网球比赛会在存在未结算持仓时按 `game_id` 定向补拉，用于补齐结算。
8. 当前实盘风控限制：单笔 2 USDC，全局当日 6 USDC。
9. 实盘下单已切换到 `py-clob-client-v2`，自动适配 CLOB order version。
10. 实盘盘口解析兼容 CLOB V2 的字典结构。
11. 实盘每次账户快照成功后自动对账：交易所已结算的仓位在本地补结算，交易所查不到的
    幽灵 `OPEN` 记录会被释放，避免过期记录一直占用持仓额度（`LIVE_RECONCILE_ENABLED`）。
12. 模拟盘「盈亏」改为「净值 − 起始资金」，与净值同源；历史差额可用
    `python tmp/recalc_paper_cash.py` 核对。
13. 首页区分「交易所口径」与「本地口径」，并新增盘口状态列、当日额度剩余、
    信号日志筛选与原因聚合；`/api/status` 体积从 167 KB 降到约 65 KB。
14. 首页新增「📖 实时盘口（只读 · 不参与下单）」：官方网页没有完整订单簿，
    面板直接读 CLOB 公开 `/book` 展示阶梯（2 秒刷新，切后台自动暂停）。
    候选自动取实盘持仓 → 模拟持仓 → 6 小时内的信号，也可手粘 token_id；
    「📸 抓快照存盘」把全档阶梯追加进 `trades/book_snapshots.jsonl`。
    命令行版：`python tmp/show_book.py <token_id 或 position_id>`。
15. 「网球买入后回撤」复盘 + **补单影子线**（只记录，不下单、不改账本）：
    * 实盘未结算持仓也纳入盘口采样（`ledger="live"`），补上"实盘没有入场后路径"的缺口；
    * 卖一首次跌破阈值时记一条触发 → `trades/dip_add_shadow.jsonl`，字段含
      "同等金额再买一单"的虚拟份额与盈亏两态、以及会不会被 0.60 闸门放行；
    * `python tmp/analyze_dip_add_shadow.py [--ledger live] [--gate-only]`
      与真实结算对齐后出胜率 / ROI / z 值；
    * 开关：`DIP_ADD_SHADOW_ENABLED`、`DIP_ADD_SHADOW_PATH`。
      结论未定（模拟盘 z≈1.7），**先攒样本再决定要不要上真钱**，详见 `PROJECT_DESIGN.md` #118。
16. **入场价区间闸门（2026-09-23）**：实盘与模拟盘两个账本交叉验证后，
    唯一"两边都为正"的入场价区间是 **0.94–0.97**（0.80–0.90 两边都为负），
    所以只在这个区间下单，区间外以原因码 `ENTRY_BAND` 拒绝（只记事件、不熔断）。
    配置：`entry_band_enabled` / `entry_band_min` / `entry_band_max`
    （环境变量 `ENTRY_BAND_ENABLED`、`ENTRY_BAND_MIN`、`ENTRY_BAND_MAX`）。
    背景与数据见 `PROJECT_DESIGN.md` #119。
