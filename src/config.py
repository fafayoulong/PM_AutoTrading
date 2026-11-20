# -*- coding: utf-8 -*-
"""Global configuration for the NBA end-game lock paper trading bot."""

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SportRiskRules:
    enabled: bool = False
    max_entry_price: float = 0.70
    # 市场一致性闸门：市场价低于它就不下单。
    # 扫尾盘的逻辑前提是"市场已认为比赛快结束（0.9+），我们抢最后几个点"；
    # 如果市场只给 0.30，说明市场完全不认为锁定 —— 通常是我们的判定错了
    # （2026-09-12 Tiafoe @0.30 误触发并亏损就是这种情况）。
    live_min_entry_price: float = 0.60
    min_expected_profit: float = 0.30
    max_position_size: float = 500.0
    max_open_positions: int = 5

    live_max_order_size_usdc: float = 2.0
    live_max_open_positions: int = 1
    live_max_daily_spend_usdc: float = 6.0
    live_max_daily_loss_usdc: float = 1.0
    live_order_timeout_seconds: int = 45
    # 必须不大于 live_max_order_size_usdc，否则该运动永远无法下单
    live_min_order_size_usdc: float = 1.0
    live_max_book_age_seconds: int = 30
    live_max_book_spread: float = 0.20
    # 2026-09-23 曾把它改成 0（想省掉"抬价一个 tick"），**当天回退**：
    # 拿交易所账本交叉核对后发现，本地账本的 entry_price 记的是**委托限价**而不是成交价
    # （`live_trader._apply_remote_order` 把订单的 `price` 字段当成交价用），
    # 交易所真实成交价中位只比卖一高 0.36 tick —— 执行本身并没有白送一个 tick。
    # 真按卖一挂单（buffer=0）大概率不成交、变成挂单，退化成 maker 场景；证据不支持。
    # 保留 1 tick 作为"确保吃到"的缓冲。详见 PROJECT_DESIGN #120。
    live_entry_buffer_ticks: int = 1
    # 实盘价格滑点容差：允许实盘报价比 max_entry_price 多付这么多（绝对价）
    live_entry_price_tolerance: float = 0.005

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "max_entry_price": self.max_entry_price,
            "live_min_entry_price": self.live_min_entry_price,
            "min_expected_profit": self.min_expected_profit,
            "max_position_size": self.max_position_size,
            "max_open_positions": self.max_open_positions,
            "live_max_order_size_usdc": self.live_max_order_size_usdc,
            "live_max_open_positions": self.live_max_open_positions,
            "live_max_daily_spend_usdc": self.live_max_daily_spend_usdc,
            "live_max_daily_loss_usdc": self.live_max_daily_loss_usdc,
            "live_order_timeout_seconds": self.live_order_timeout_seconds,
            "live_min_order_size_usdc": self.live_min_order_size_usdc,
            "live_max_book_age_seconds": self.live_max_book_age_seconds,
            "live_max_book_spread": self.live_max_book_spread,
            "live_entry_buffer_ticks": self.live_entry_buffer_ticks,
            "live_entry_price_tolerance": self.live_entry_price_tolerance,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SportRiskRules":
        known = cls()
        for key, value in data.items():
            if hasattr(known, key):
                setattr(known, key, value)
        return known


@dataclass
class Config:
    # Paper trading budget in USDC
    starting_budget: float = 10_000.0

    # Relaxed heuristic signal thresholds targeting roughly 75% scenarios.
    min_quarter: int = 4
    min_time_remaining_seconds: int = 480
    max_time_remaining_seconds: int = 0
    min_score_diff: int = 5
    score_diff_per_minute: float = 0.0

    # MLB signal thresholds
    mlb_min_inning: int = 8
    mlb_min_score_diff: int = 2

    # Tennis signal thresholds
    #
    # 2026-10-23 由 2 回退到 1（PROJECT_DESIGN #136）。
    # 缘由：修好盘口采集（#131）后用干净快照回放，原始判定（局分差≥1）配合
    # 价格上限 0.92 在 185 场上给出 **+5.33%**，且前后两段独立样本几乎相同
    # （+5.34% / +5.33%）；而"局分差≥2 + 0.60~0.96"是 +1.05%，前后段
    # +3.17% / −2.19% 不稳。工具 tools/tennis_original_rule_replay.py。
    # 仍属实验：合并 0.60~0.92 是 +3.28%、n=177，95% 区间 [−1.8%, +8.3%] 跨 0。
    # 两周后用同一个工具对比实盘 ROI 与 +3.3% 的预测决定去留。
    tennis_min_game_diff_in_set: int = 1

    # 美式橄榄球（NFL / CFB）锁定阈值：按剩余时间分五档。
    # 2026-09-13 实测教训：只在"第 4 节 + 分差 ≥16"判定会在第四节一开始
    # （还剩 900 秒 = 整整一节、十几个球权）就触发，差 17~21 分并不锁定
    # （OSU 23-3、第 4 节 889s 触发后输掉，市场当时只给 0.92）。所以补上时间上限：
    #   ≤ 40 秒  ：8 分
    #   ≤ 120 秒 ：10 分
    #   ≤ 300 秒 ：16 分
    #   ≤ 600 秒 ：21 分
    #   ≤ 900 秒 ：28 分（整节 15 分钟只剩下 0 秒才等于"一节内追 28 分"）
    #   > 900 秒 或非第 4 节：不参与
    football_min_quarter: int = 4
    football_last_seconds: int = 40
    football_last_min_score_diff: int = 8
    football_late_clock_seconds: int = 120
    football_late_min_score_diff: int = 10
    football_clock_seconds: int = 300
    football_min_score_diff: int = 16
    football_mid_clock_seconds: int = 600
    football_mid_min_score_diff: int = 21
    football_wide_clock_seconds: int = 900
    football_wide_min_score_diff: int = 28

    # 冰球（NHL）锁定阈值：规矩结束前允许追平进加时，所以 1 球领先永不"锁定"。
    #   第 3 节（常规时间最后一节）+ 剩余 ≤120 秒：领先 2 球
    #   第 3 节 + 剩余 ≤300 秒：领先 3 球
    #   加时/点球（period ≥ 4）不参与：任何一次射门都能结束比赛
    hockey_min_period: int = 3
    hockey_last_clock_seconds: int = 120
    hockey_min_score_diff: int = 2
    hockey_wide_clock_seconds: int = 300
    hockey_wide_min_score_diff: int = 2
    hockey_extended_clock_seconds: int = 600
    hockey_extended_min_score_diff: int = 3
    hockey_very_wide_clock_seconds: int = 900
    hockey_very_wide_min_score_diff: int = 4

    # 大学篮球（NCAAB 男篮 / NCAAW 女篮）锁定阈值。
    # 为什么不能直接复用 NBA 的 min_quarter=4：
    #   男篮是 2 个半场（终场 period=2），套 min_quarter=4 会永不触发；
    #   女篮是 4 节（终场 period=4），可以按第 4 节判。
    # 为什么默认比 NBA 更保守：大学篮球回合数更少、但三分与犯规停表让最后两分钟
    # 极不稳定（领先 10 分 40 秒内被追平是常态），所以要求更大分差 + 更少剩余时间。
    ncaab_min_period: int = 2        # 男篮：进入第 2 半场才判定
    ncaaw_min_period: int = 4        # 女篮：第 4 节
    cbb_min_clock_seconds: int = 0   # 剩余时间下限（0 = 终场哨响也算）
    cbb_max_clock_seconds: int = 240  # 剩余时间上限：最多还剩 4 分钟
    cbb_min_score_diff: int = 7      # 终场时刻至少领先 7 分
    cbb_score_diff_per_minute: float = 1.0  # 每多剩 1 分钟，多要求 1 分

    # ---------- 足球（模型驱动，不用固定阈值）----------
    # 每个联赛登记三件事：Polymarket 的盘口 tag、ESPN 记分板 slug、
    # 以及该联赛的主/客期望进球 λ（由 5868 场实测拟合，见 docs 20.3）。
    # λ 的联赛差异不小（意甲 2.58 球/场 vs 德甲 3.18），所以按联赛分开用。
    #
    # 足球的锁定判定不看"分差 + 剩余时间"的硬阈值，而是直接问胜率模型：
    #   P(领先方最终获胜) >= soccer_min_win_probability 才出信号，
    #   再看价格是否还在盈利区间内（price <= P - soccer_min_edge）。
    soccer_enabled: bool = True
    soccer_min_win_probability: float = 0.90
    # 2026-09-20 用户要求：2pp → **1pp**（"有 1pp 就可以成交"）。
    # ⚠️ 已知风险：足球 Poisson 模型自己的校准表里，0.7-0.8 档**高估 3.9pp** ——
    #    门槛降到 1pp 后，成交的正是边际最小的那批，而 1pp 完全落在模型误差范围内。
    #    放宽的反向价值：给 #102「市场是否未收敛」的前提检验补上低边际对照组
    #    （原来成交全是高边际、没有对照）。所以要盯着前 N 笔 1~2pp 的单子：
    #    如果它们的实际胜率明显低于入场价，就说明这 1pp 是模型噪声，应该调回 2pp。
    soccer_min_edge: float = 0.01
    # 分差 × 时间分档（2026-09-20 按 2314 场德甲/德乙历史数据定，见 PROJECT_DESIGN #112）：
    #   领先 ≥3 球 → 任何时间都可以（历史胜率 96.6~100%）
    #   领先 2 球  → 需要比赛已进行到 soccer_min_minute
    #                （45′ 之前只有 90.7~91.3%，而我们的成交价约 0.89 —— 太薄）
    #   领先 1 球  → **暂不限制**（历史 73~88%，其实偏薄，但先观察一段时间再决定；
    #                这正是用户要求"先做 2 球/45′ 这条，剩下的再观察"的那部分）
    soccer_diff_anytime: int = 3        # ≥这个分差：不设时间限制
    soccer_diff_needs_minute: int = 2   # 这个分差：需要满足时间下限
    soccer_min_minute: float = 45.0     # 时间下限（比赛已进行分钟）
    # 末段领先球数下限（2026-09-21 加，见 PROJECT_DESIGN #116）。
    # 依据：48 小时内 6 笔亏损里 4 笔发生在 80 分钟以后，全部是"1 球领先被追平"：
    #   FUL 1-0@86' → 91' 被追平（付 0.73） / BET 1-0@86' → 87' 被追平（付 0.89，入场后 86 秒）
    #   两笔模型都给 90~92%，而我们 43 笔足球的实际胜率只有 83.7%（模型高估 10.4pp）。
    # 历史表里「1 球领先 80-90' = 88.4%」是**欧洲联赛 90 分钟口径**：既没算补时，
    # 也不含 MLS（实测 MLS 90 分钟以后的进球占 18.4%）。所以末段只做 ≥2 球领先。
    soccer_late_minute: float = 85.0    # 「末段」从第几分钟算起
    soccer_late_min_diff: int = 2       # 末段要求的最低领先球数
    soccer_max_goals: int = 10
    # 盘口映射缓存秒数：token/题面稳定，只有价格要实时（价格走 CLOB 另行读取）
    soccer_market_cache_seconds: float = 600.0
    soccer_leagues: list[dict] = field(default_factory=lambda: [
        {"key": "EPL", "tag": "epl", "espn": "eng.1", "name": "英超",
         "lambda_home": 1.657, "lambda_away": 1.450},
        {"key": "LALIGA", "tag": "laliga", "espn": "esp.1", "name": "西甲",
         "lambda_home": 1.464, "lambda_away": 1.128},
        {"key": "SERIEA", "tag": "serie-a", "espn": "ita.1", "name": "意甲",
         "lambda_home": 1.396, "lambda_away": 1.182},
        {"key": "BUNDESLIGA", "tag": "bundesliga", "espn": "ger.1", "name": "德甲",
         "lambda_home": 1.765, "lambda_away": 1.411},
        {"key": "LIGUE1", "tag": "ligue-1", "espn": "fra.1", "name": "法甲",
         "lambda_home": 1.513, "lambda_away": 1.314},
        {"key": "EREDIVISIE", "tag": "eredivisie", "espn": "ned.1", "name": "荷甲",
         "lambda_home": 1.731, "lambda_away": 1.364},
        # 2026-09-21 起**摘掉 MLS**（见 PROJECT_DESIGN #116）。理由不是"运气差"，是可验证的：
        #   * 模型的训练数据里根本没有 MLS（soccer_matches.jsonl 5868 场只有
        #     英超/西甲/意甲/德甲/法甲/荷甲），对 MLS 的领先安全性是**外推**；
        #   * 实测 8 笔 MLS：模型平均 94.9% vs 实际胜率 **62.5%**（真实边际 −24.4pp），
        #     纸面 $500/笔亏 −1153.84 —— 单独一个联赛把整个足球账本从 +579 拖成 −574；
        #   * MLS 的末段进球明显更多：12 场 / 49 个进球里，**90 分钟以后 9 个 = 18.4%**，
        #     85 分钟以后合计 22.4%（模型用的德甲基线 85-90 分钟只有 9.69%）。
        # 要恢复的话：先给 MLS 单独拟合 λ 与"补时进球占比"，别直接套欧洲参数。
        # {"key": "MLS", "tag": "mls", "espn": "usa.1", "name": "MLS",
        #  "lambda_home": 1.573, "lambda_away": 1.293},
        # 2026-09-20 起**暂时摘掉欧冠（UCL）**：它是杯赛，淘汰赛有加时/点球，而
        # 我们买的是"某队取胜"的二元盘 —— 如果该盘按**90 分钟常规时间**结算，
        # 那么"90′ 被追平、加时赢了"对我们就是**全额亏损**，而我们的信号完全没考虑这点
        # （只看领先球数/时间/模型概率）。规则原文还没查到（当时没有活跃的 UCL 盘口），
        # 在查清"按 regulation time 还是含加时"之前，不在杯赛上冒这个额外风险。
        # 联赛制比赛（英超/西甲/意甲/德甲/法甲/荷甲/MLS）没有加时，不受影响。
        # 查清规则后如果要恢复，把下面这行取消注释即可（其它代码无需改动）：
        # {"key": "UCL", "tag": "ucl", "espn": "uefa.champions", "name": "欧冠",
        #  "lambda_home": 1.573, "lambda_away": 1.293},
    ])

    # CS2（电竞）信号阈值：系列赛领先 + 当前地图拿到赛点且领先足够多
    cs2_map_point_rounds: int = 12
    cs2_min_map_round_lead: int = 4

    # Polymarket thresholds
    max_entry_price: float = 0.70
    min_expected_profit: float = 0.30

    # Position sizing
    max_position_size: float = 500.0
    max_open_positions: int = 5

    # Probability paper module (Plan A): collect data without live execution.
    probability_enabled: bool = True
    probability_log_min_probability: float = 0.60
    probability_min_probability: float = 0.70
    probability_min_edge: float = 0.05
    probability_max_entry_price: float = 0.70
    probability_paper_budget: float = 1_000.0
    probability_max_position_size: float = 50.0
    probability_max_open_positions_per_sport: int = 2
    probability_max_open_positions_total: int = 6
    probability_trades_path: str = "trades/probability_paper_trades.jsonl"
    probability_state_path: str = "trades/probability_state.json"
    probability_events_path: str = "trades/probability_events.jsonl"
    probability_processed_path: str = "trades/probability_processed.json"
    mlb_dataset_path: str = "trades/mlb_dataset.jsonl"
    mlb_model_path: str = "trades/mlb_logit_model.json"
    mlb_metrics_path: str = "trades/mlb_logit_metrics.json"

    # Polling intervals (seconds)
    nba_poll_interval: int = 3
    polymarket_poll_interval: int = 5

    # Paths
    trades_path: str = "trades/paper_trades.jsonl"
    state_path: str = "trades/state.json"
    log_path: str = "logs/bot.log"
    signal_events_path: str = "trades/signal_events.jsonl"
    risk_settings_path: str = "trades/risk_settings.json"
    score_change_state_path: str = "trades/score_change_state.json"

    # ESPN API base
    espn_scoreboard_url: str = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    )
    espn_mlb_url: str = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
    # MLB 官方数据源（ESPN 的上游，通常更快）
    mlb_statsapi_schedule_url: str = "https://statsapi.mlb.com/api/v1/schedule"
    # espn | statsapi
    mlb_feed_source: str = "statsapi"
    # 2026-10-29：NBA / NHL 也改走联盟官方接口（比 ESPN 快 5~10 秒），失败自动回退 ESPN。
    #   NBA : cdn.nba.com liveData（需要 Referer/Origin，否则 403）
    #   NHL : api-web.nhle.com/v1/scoreboard/now
    nba_live_data_url: str = (
        "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
    )
    nhl_scoreboard_url: str = "https://api-web.nhle.com/v1/scoreboard/now"
    # espn | official
    # ⚠️ 2026-10-29 实测：cdn.nba.com 对 VPS 的连续请求会触发 Akamai "Access Denied"（403，
    # 带 edgesuite reference）。单次请求能成，但每 3-5 秒轮询必被封 → 不适合生产。
    # 实现保留在 `NBAFeed._fetch_nba_official()`，等有代理/换 IP 时把这里改成 "official" 即可。
    nba_feed_source: str = "espn"
    # espn | official
    nhl_feed_source: str = "official"
    espn_wnba_url: str = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
    # 美式橄榄球（Polymarket 盘口 tag 分别是 nfl / cfb，与 sport.lower() 一致，无需额外映射）
    espn_nfl_url: str = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    espn_cfb_url: str = (
        "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
    )
    espn_nhl_url: str = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
    # 大学篮球：**男篮是 2 个半场、女篮是 4 节**（2026-09-14 用历史比赛核对过
    # ESPN 的 period 语义：男篮 Final 时 period=2、女篮 period=4），
    # 所以两者不能共用同一个节次阈值。队名用学校名（同 CFB）。
    espn_ncaab_url: str = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
    )
    espn_ncaaw_url: str = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard"
    )
    # Polymarket 自带的体育实时比分流（含电竞）
    polymarket_sports_ws_url: str = "wss://sports-api.polymarket.com/ws"
    # PM 实时流覆盖 ESPN 快照：PM 推送更快（数百毫秒 vs 轮询 3 秒），
    # 但必须先与 ESPN 比分逐队一致过一次才会生效（详见 src/sports_overlay.py）
    pm_stream_overlay_enabled: bool = True
    pm_stream_override_sports: tuple = (
        "MLB", "NBA", "WNBA", "NFL", "CFB", "NHL", "TENNIS",
    )
    pm_stream_stale_seconds: float = 90.0
    # 部分运动的 tag_slug 与实际比赛盘口所在 tag 不一致，需要按 tag_id 查询
    sport_market_tag_ids: dict = field(default_factory=lambda: {"CS2": "100780"})
    espn_tennis_urls: list = field(default_factory=lambda: [
        "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
    ])
    # ESPN 经代理访问较慢（实测 1.6~8 秒），超时给足余量
    espn_timeout_seconds: int = 20

    # Polymarket CLOB API base (public, no key needed for prices)
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"

    # Dashboard mode: paper / readonly / live. Paper is the safe default.
    live_mode: str = "paper"
    live_refresh_interval: int = 60

    # Stage 2 live execution safety switches.
    live_execution_enabled: bool = False
    live_global_enabled: bool = False
    live_unattended_enabled: bool = True
    live_allowed_sports: list[str] = field(default_factory=lambda: ["MLB", "NBA"])
    sport_rules: dict[str, SportRiskRules] = field(default_factory=lambda: {
        "NBA": SportRiskRules(),
        "MLB": SportRiskRules(),
        # 网球最低入场价：**0.60（与其它 10 个运动一致）**。
        #
        # 历史：2026-09-19 曾按"近一周亏损复盘"把它抬到 0.86，依据是
        # 0.80-0.85 档 n=16、入场价隐含胜率 82.7% vs 实际 81.2%（差 **-1.5pp**）。
        # **2026-09-29 撤销**（PROJECT_DESIGN #123），三条理由：
        #   1) n=16 时胜率标准误 ≈9.8pp，-1.5pp 只有 0.15σ —— 拿噪声定的阈值；
        #   2) 同一时期模拟盘在 <0.86 那 46 笔上给**相反**结论：ROI +11.04%
        #      （全样本最赚的一档），实盘同段 -6.95%，两边都不显著；
        #   3) 地板生效前后：地板之前两个账本都为正（实盘 +2.08% n=71 /
        #      模拟盘 +8.22% n=107），之后都为负（-3.84% / -1.38%）——根因是
        #      入场价上移（0.865→0.935），不是地板本身，但地板也没防住它。
        # 地板还挡住约 1/3 的网球信号且**没被采集反事实路径**（OBS_ 至今为 0），
        # 等于一个无法被验证的常数。要恢复必须用**新样本**按价位分档复核。
        #
        # 注意：这不是"加止损"。2026-10-30 的 3,918 条已结算影射触发复核显示：
        # 按可执行买一价成交时所有阈值/确认组合仍为负；按阈值价成交的上限口径
        # 虽然在 0.50~0.70 偶尔为正，但前后两段样本符号翻转，不能据此上线。
        "TENNIS": SportRiskRules(
            enabled=True,
            max_entry_price=0.99,
            live_min_entry_price=0.60,
            min_expected_profit=0.01,
            max_open_positions=10,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=4,
            live_max_daily_spend_usdc=50.0,
            live_max_daily_loss_usdc=20.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.15,
            live_entry_buffer_ticks=1,
        ),
        # 新增运动默认开启；可在风控设置页按运动关闭
        "WNBA": SportRiskRules(enabled=True),
        "CS2": SportRiskRules(enabled=True),
        # 美式橄榄球：盘口薄一些，价差收紧、最低入场价与其它运动一致
        "NFL": SportRiskRules(
            enabled=True,
            max_entry_price=0.98,
            min_expected_profit=0.02,
            live_min_entry_price=0.60,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=2,
            live_max_daily_spend_usdc=10.0,
            live_max_daily_loss_usdc=6.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.15,
            live_entry_buffer_ticks=1,
        ),
        "CFB": SportRiskRules(
            enabled=True,
            max_entry_price=0.98,
            min_expected_profit=0.02,
            live_min_entry_price=0.60,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=2,
            live_max_daily_spend_usdc=10.0,
            live_max_daily_loss_usdc=6.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.15,
            live_entry_buffer_ticks=1,
        ),
        # 冰球：盘口同样偏薄，参数与橄榄球一致
        "NHL": SportRiskRules(
            enabled=True,
            max_entry_price=0.98,
            min_expected_profit=0.02,
            live_min_entry_price=0.60,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=2,
            live_max_daily_spend_usdc=10.0,
            live_max_daily_loss_usdc=6.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.15,
            live_entry_buffer_ticks=1,
        ),
        # 大学篮球：盘口深度未知（赛季 11 月才开始），先按橄榄球口径保守起步；
        # 开赛后看 NO_MARKET / 盘口深度再调。
        "NCAAB": SportRiskRules(
            enabled=True,
            max_entry_price=0.98,
            min_expected_profit=0.02,
            live_min_entry_price=0.60,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=2,
            live_max_daily_spend_usdc=10.0,
            live_max_daily_loss_usdc=6.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.15,
            live_entry_buffer_ticks=1,
        ),
        "NCAAW": SportRiskRules(
            enabled=True,
            max_entry_price=0.98,
            min_expected_profit=0.02,
            live_min_entry_price=0.60,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=2,
            live_max_daily_spend_usdc=10.0,
            live_max_daily_loss_usdc=6.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.15,
            live_entry_buffer_ticks=1,
        ),
        # 足球：所有联赛共用一套风控（盘口结构相同：主胜/平局/客胜三个二元盘）。
        # 价差放到 0.20 —— 实测足球单场盘的 bid/ask 比篮球宽（样例 0.34/0.63）。
        "SOCCER": SportRiskRules(
            enabled=True,
            max_entry_price=0.98,
            min_expected_profit=0.02,
            live_min_entry_price=0.60,
            live_max_order_size_usdc=2.0,
            live_max_open_positions=4,
            live_max_daily_spend_usdc=10.0,
            live_max_daily_loss_usdc=6.0,
            live_min_order_size_usdc=1.0,
            live_max_book_spread=0.20,
            live_entry_buffer_ticks=1,
        ),
    })
    live_max_order_size_usdc: float = 2.0
    live_max_daily_spend_usdc: float = 6.0
    live_max_daily_loss_usdc: float = 1.0
    live_order_timeout_seconds: int = 45
    live_min_order_size_usdc: float = 5.0
    live_abnormal_pause_enabled: bool = True
    live_max_book_age_seconds: int = 30
    live_max_book_spread: float = 0.20
    live_orders_path: str = "trades/live_orders.jsonl"
    live_order_events_path: str = "trades/live_order_events.jsonl"
    # 只读账户接口的重试：出口抖动时同一次刷新内重试，避免面板整分钟显示报错
    live_fetch_attempts: int = 3
    live_fetch_retry_delay_seconds: float = 0.4
    # 已结算持仓的分页上限：data-api 单页最多 50 条，必须翻页才能统计全量盈亏
    # （只取第一页会让面板的「已实现盈亏」漏掉更早的仓位）
    live_closed_positions_max_pages: int = 20
    # 实盘对账：用交易所只读持仓校准本地 OPEN 记录，避免过期记录占用持仓额度
    live_reconcile_enabled: bool = True
    # 本地 OPEN 在交易所「持仓」与「已结算」里都找不到时，超过这个小时数就释放额度
    live_reconcile_orphan_hours: float = 12.0
    # 当日额度 / 当日盈亏的换日时区（默认北京时间 0 点重置；旧行为是 UTC 0 点）
    daily_reset_timezone: str = "Asia/Shanghai"

    # ---------- 持久化体积控制 ----------
    # 运行日志轮转（logs/bot.log）
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5
    # 交易所权益采样日志（trades/live_account_history.jsonl）轮转与启动读取上限
    live_history_max_bytes: int = 5 * 1024 * 1024
    live_history_backup_count: int = 10
    live_history_load_lines: int = 20000
    # 实盘状态快照日志（trades/live_orders.jsonl）超过多少行就做一次压缩
    live_state_compact_lines: int = 2000
    # state.json 里保留的最近信号条数与权益点数（完整历史在 signal_events.jsonl）
    paper_signal_log_limit: int = 500
    paper_equity_history_limit: int = 5000

    # ---------- 持仓期间盘口采集（只读） ----------
    # 止损策略复核的前置数据：中间价回答不了「挂单能不能成交」，
    # 必须记持仓期间的 best_bid/best_ask。见 src/position_paths.py。
    position_path_enabled: bool = True
    position_paths_path: str = "trades/position_paths.jsonl"
    position_path_interval_seconds: float = 8.0
    position_path_max_concurrent: int = 4
    position_path_timeout_seconds: float = 6.0
    position_path_max_bytes: int = 32 * 1024 * 1024
    # 2026-10-27：保留 ~60 天回测数据（32MB/份 ≈ 5 天 → 12 份 ≈ 60 天）
    position_path_backup_count: int = 12

    # ---------- 入场前盘口采集（只读，2026-09-28）----------
    # 为什么需要：整个策略的前提是"我们下单时市场还没收敛完"，而
    # `position_paths.jsonl` **只采入场之后**的快照（age_s 最小值 0.3 秒、
    # 负值 0 条），所以「信号触发前价格已经走到哪」从来没有数据 ——
    # 这正是这个前提反复验证不出结论的根因（PROJECT_DESIGN #121）。
    #
    # 采样对象是**预匹配成功、且已进入"潜在锁定"状态**的比赛盘口
    # （网球：已分出盘数领先且当前盘未结束）。不采全部比赛，避免请求量失控。
    # 见 src/watch_paths.py。
    watch_path_enabled: bool = True
    watch_paths_path: str = "trades/watch_paths.jsonl"
    watch_path_sports: list[str] = field(default_factory=lambda: ["TENNIS"])
    watch_path_interval_seconds: float = 3.0
    # 每轮最多采几个 token、并发几个：这是"只读但会产生请求"的部分，
    # 上限宁可保守 —— 采少了只是样本少，采猛了会影响下单链路的网络。
    watch_path_max_targets: int = 12
    watch_path_max_concurrent: int = 2
    watch_path_timeout_seconds: float = 6.0
    watch_path_max_bytes: int = 32 * 1024 * 1024
    # 入场前盘口采集：32MB/份 ≈ 2.3 天 → 30 份 ≈ 70 天
    watch_path_backup_count: int = 30

    # ---------- 影子止损（只记录"如果挂了止损会怎样"，不真的卖）----------
    # 为什么要影子模式：2026-09-15 用真实买一价做的反事实回测只有 15 笔样本
    # （2 笔亏损），阈值排序完全由两笔决定 —— 直接上限价卖出等于拿真金白银
    # 赌一个可能过拟合的参数。影子模式让每个新仓位都自动产生反事实数据，
    # 攒够 1-2 周再决定线上的阈值。
    #
    # 同时评估多个阈值 × 多种确认规则：
    #   * 阈值：从 0.70（浅，保护多但误杀多）到 0.40（深，几乎不误杀但保护少）
    #   * 确认：连续 N 次采样跌破才触发（1=立刻，2≈16 秒，3≈24 秒）
    # 确认规则是"有状态"的，离线从价格轨迹也能算，但在这里记录更贴近真实挂单行为。
    shadow_stop_enabled: bool = True
    shadow_stop_path: str = "trades/shadow_stop_events.jsonl"
    # ---------- 模拟盘结算兜底（2026-09-20）----------
    # 模拟盘原来只有"比分流"一条结算路径（要求终局那一刻行情快照里有这场比赛），
    # WS 在终局前后断线就会把仓位永久挂成 OPEN —— 实测 CS2 1681114 就是这样丢了
    # 一笔亏损，账面显示 22 胜 0 负而实盘是 13 胜 1 负。
    # 兜底路径与传输层无关：开仓满 min_age 分钟后直接问 Gamma 市场结算结果。
    # min_age = 30 分钟，与实时流对终局行的淘汰宽限一致，保证比分流仍然优先。
    paper_settle_reconcile_enabled: bool = True
    paper_settle_reconcile_path: str = "trades/settlement_reconcile_events.jsonl"
    paper_settle_reconcile_interval_seconds: float = 600.0
    paper_settle_reconcile_min_age_seconds: float = 1800.0
    paper_settle_reconcile_timeout_seconds: float = 20.0
    # API 可达性探活间隔（秒）：面板上的"实盘客户端 / 公共 REST / Gamma"状态灯
    api_health_interval_seconds: float = 60.0
    # 下单失败重试（2026-09-18）：传输层失败（TLS/连接）自动重试，
    # 每次重试前先查一次交易所，避免"其实已经成交"时补第二单。
    live_submit_retries: int = 3
    live_submit_retry_delays: list = field(default_factory=lambda: [1.5, 4.0])
    # 客户端初始化失败后的重试间隔（秒）：网络恢复后自动接上，不用重启 bot
    live_client_retry_seconds: float = 60.0
    # 下单后的成交确认短轮询（2026-09-18 引入，2026-09-19 迁到 VPS 后再收紧）。
    # 演进：5 秒全局轮询 → 0.5s×10（实测确认耗时中位 0.82s）→ **0.2s×20**。
    # 为什么能再砍：这段耗时几乎全是"轮询间隔"本身，跟网络无关 —— 本地和 VPS
    # 实测都是 0.8s 左右，说明瓶颈是 sleep 不是 RTT。VPS 上查一次订单只要 20ms，
    # 间隔 0.2s 时代价是多打几次请求，收益是确认延迟→约 0.2~0.4s。
    # 次数从 10 提到 20 是为了**保持总覆盖窗口**（0.2×20 = 4s ≈ 原来 0.5×10 = 5s），
    # 免得"慢成交"反而更容易落到 5 秒全局轮询上。设为 0 可关闭。
    live_confirm_poll_attempts: int = 20
    live_confirm_poll_interval_seconds: float = 0.2
    shadow_stop_thresholds: list[float] = field(
        default_factory=lambda: [0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40]
    )
    shadow_stop_confirm_samples: list[int] = field(default_factory=lambda: [1, 2, 3])
    shadow_stop_max_bytes: int = 32 * 1024 * 1024
    shadow_stop_backup_count: int = 10

    # ---------- 补单影子线（2026-09-21）----------
    # 用户观察："网球每次买入后价格总会波动、下降很多，但最后还是我赢 ——
    # 能不能等价格跌到低点再补一单？"
    #
    # 复盘结论（`tmp/analyze_tennis_dip.py`，62 笔有入场后路径的模拟盘仓位）：
    #   * 现象是真的：69% 的仓位回撤 ≥3pp，中位 −6.2pp，90% 最终回到入场价之上；
    #   * 但回撤**带信息**：赢单回撤中位 −5.9pp，输单 −75.8pp（4 笔输单全部 ≥5pp）；
    #     条件胜率随深度下降（全体 94% → ≥5pp 档 89% → ≥8pp 档 86%）；
    #   * 反事实（固定金额、按当时卖一成交）在 ≥3pp 档是 +8.1pp 边际，但 z≈1.7，
    #     而且**全部来自模拟盘** —— 实盘此前根本没有入场后路径（这次一并补上）。
    #
    # 所以决策是：**先不开真钱**，只把"如果补一单会怎样"逐笔记下来，
    # 攒够真实样本再定阈值与单笔上限（用户 2026-09-21 选 A+B）。
    dip_add_shadow_enabled: bool = True
    dip_add_shadow_path: str = "trades/dip_add_shadow.jsonl"
    # 触发线 = 相对「入场后第一份中间价」的跌幅；每仓位每阈值只记第一次命中
    dip_add_shadow_thresholds: list[float] = field(
        default_factory=lambda: [0.02, 0.03, 0.05, 0.08]
    )
    dip_add_shadow_max_bytes: int = 32 * 1024 * 1024
    dip_add_shadow_backup_count: int = 10

    # ---------- 决胜盘平分影子陪跑仓（2026-10-29） ----------
    # 只记录：第三盘盘分 1-1、当前局分首次达到 1-1/2-2/.../6-6 时，
    # 按当时真实卖一价虚拟买入低价方，并持有到结算。绝不下任何真实订单。
    tennis_tie_shadow_enabled: bool = True
    tennis_tie_shadow_path: str = "trades/tennis_tie_shadow.jsonl"
    tennis_tie_shadow_stake_usdc: float = 1.0
    # 运动类 taker fee = 5% * notional * (1 - price)。
    tennis_tie_shadow_fee_rate: float = 0.05
    tennis_tie_shadow_reconcile_interval_seconds: float = 300.0
    tennis_tie_shadow_reconcile_min_age_seconds: float = 300.0
    tennis_tie_shadow_timeout_seconds: float = 20.0
    tennis_tie_shadow_max_bytes: int = 32 * 1024 * 1024
    tennis_tie_shadow_backup_count: int = 10
    # 决胜盘平分事件级链路账本：比分变化、两边盘口、选边、发单、成交逐阶段记录。
    tennis_tie_events_enabled: bool = True
    tennis_tie_events_path: str = "trades/tennis_tie_events.jsonl"
    tennis_tie_events_max_bytes: int = 32 * 1024 * 1024
    tennis_tie_events_backup_count: int = 8
    # 2026-11-09：按目标金额下单。交易所要求 marketable BUY 至少 $1，
    # 固定 1 份在低价盘口会变成 $0.3~$0.8，直接被 CLOB 拒绝。
    tennis_tie_live_enabled: bool = True
    tennis_tie_live_order_usdc: float = 1.10

    # ---------- CS2 deciding-map tied-score shadow ledger ----------
    # BO3 starts on map 3 at series score 1-1; BO5 starts on map 5 at 2-2.
    # A new tied round score (1-1 ... 12-12) records one read-only entry.
    cs2_tie_shadow_enabled: bool = True
    cs2_tie_shadow_path: str = "trades/cs2_tie_shadow.jsonl"
    cs2_tie_shadow_stake_usdc: float = 1.0
    cs2_tie_shadow_fee_rate: float = 0.05
    cs2_tie_shadow_max_round_score: int = 12
    cs2_tie_shadow_reconcile_interval_seconds: float = 300.0
    cs2_tie_shadow_reconcile_min_age_seconds: float = 300.0
    cs2_tie_shadow_timeout_seconds: float = 20.0
    cs2_tie_shadow_max_bytes: int = 32 * 1024 * 1024
    cs2_tie_shadow_backup_count: int = 10

    # ---------- 入场价区间闸门（2026-09-23 上线 / 2026-09-28 关闭）----------
    # 上线依据（PROJECT_DESIGN #119）：按入场价分档比，当时的样本里
    # 0.94–0.97 是两个账本唯一同时为正的区间。
    #
    # **但上线后立刻反转，且证明它是拟合出来的**（PROJECT_DESIGN #121）：
    #   * 同样 0.94–0.97：上线前实盘 48 笔 48 连胜 +4.79%，上线后实盘 20 笔
    #     95.0% 胜 −0.76%、模拟盘 48 笔 89.6% 胜 −5.86%；
    #   * 同一样本换 0.02 一档，相邻档符号相反（0.90–0.92 两账本都正、
    #     0.92–0.94 两账本都负）—— 说明分档 ROI 的差异主要是噪声；
    #   * 闸门整段（09-23~09-28）两个账本都为负：实盘 n=32 −5.76%、模拟盘 n=52 −4.87%。
    #
    # 结论：**默认关闭**。价格区间不是可用的筛选维度；要重新启用必须先把阈值写死、
    # 再用之后的新样本（且不在同一样本上重挑区间）验证。
    entry_band_enabled: bool = False
    entry_band_min: float = 0.94
    entry_band_max: float = 0.97      # 上界不含（左闭右开）

    # 演练模式：放宽信号阈值 + 允许同一场比赛重复触发，用于验证 信号→下单 链路
    signal_drill_mode: bool = False
    signal_drill_cooldown_seconds: int = 60
    # 事件驱动：实时流收到状态变化就立即评估信号，不等主循环
    signal_event_driven: bool = True
    # 市场预匹配：提前把 gameId → token 映射与订单元数据准备好，压缩下单时延
    prematch_enabled: bool = True
    # 每 tick 最多启动几个预匹配任务。原来是 3，实测会积压（网球排最后总轮不到），
    # 提高到 6 让积压排空更快；预匹配完全在后台，不在下单关键路径上。
    prematch_max_per_tick: int = 6
    prematch_ttl_seconds: int = 1800
    # 匹配失败（找不到盘口）的负缓存时长。短于命中缓存，因为市场可能晚点才挂出来。
    # 意义：失败也占位，避免"永远匹配不上的比赛"每 tick 重排、把队列堵死
    # （2026-09-14 实测：网球因此一整个下午 0 次预匹配成功）。
    prematch_miss_ttl_seconds: int = 300
    # 并发上限必须跟着 per_tick 一起提，否则 per_tick 再大也会被这里卡住
    # （每个预匹配任务 1~2 秒，3 秒一个 tick，并发 3 时实际吞吐就是 3/tick）。
    prematch_max_concurrent: int = 4
    # 干跑：走完校验和签名，但不提交订单
    live_dry_run: bool = False

    # 代理：统一从环境变量 PM_PROXY_URL 读取，没设就表示直连。
    # 为什么要改成环境变量：本地在国内必须走 Clash（http://127.0.0.1:7897），
    # 而阿姆斯特丹 VPS 直连 clob 只要 35ms —— 同一份代码要在两台机器上跑，
    # 硬编码必然有一边跑不起来。
    # 注意这是**唯一来源**：aiohttp/httpx 两套客户端都读它，LiveTrader 还会把它
    # 写进 HTTP_PROXY/HTTPS_PROXY（见 live_trader._build_client）。
    proxy_url: str | None = field(
        default_factory=lambda: (os.getenv("PM_PROXY_URL", "") or "").strip() or None
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def load_config() -> Config:
    # .env 必须在读取任何环境变量之前加载：原来只有 LiveTrader/LiveAccount
    # 在实例化时调用 load_dotenv()，而 load_config() 跑在它们之前，
    # 导致 .env 里的 NBA_POLL_INTERVAL / MLB_FEED_SOURCE 等配置读不到。
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # pragma: no cover - dotenv 缺失时退化为纯环境变量
        pass
    config = Config()
    config.signal_drill_mode = _env_bool("SIGNAL_DRILL_MODE", config.signal_drill_mode)
    config.signal_drill_cooldown_seconds = _env_int(
        "SIGNAL_DRILL_COOLDOWN_SECONDS", config.signal_drill_cooldown_seconds
    )
    config.signal_event_driven = _env_bool(
        "SIGNAL_EVENT_DRIVEN", config.signal_event_driven
    )
    config.prematch_enabled = _env_bool("PREMATCH_ENABLED", config.prematch_enabled)
    config.prematch_max_per_tick = _env_int(
        "PREMATCH_MAX_PER_TICK", config.prematch_max_per_tick
    )
    config.nba_poll_interval = max(1, _env_int("NBA_POLL_INTERVAL", config.nba_poll_interval))
    config.mlb_feed_source = (
        os.getenv("MLB_FEED_SOURCE") or config.mlb_feed_source
    ).strip().lower()
    config.nba_feed_source = (
        os.getenv("NBA_FEED_SOURCE") or config.nba_feed_source
    ).strip().lower()
    config.nhl_feed_source = (
        os.getenv("NHL_FEED_SOURCE") or config.nhl_feed_source
    ).strip().lower()
    config.live_dry_run = _env_bool("LIVE_DRY_RUN", config.live_dry_run)
    config.pm_stream_overlay_enabled = _env_bool(
        "PM_STREAM_OVERLAY", config.pm_stream_overlay_enabled
    )
    config.daily_reset_timezone = (
        os.getenv("DAILY_RESET_TZ") or config.daily_reset_timezone
    ).strip() or "Asia/Shanghai"
    # 换日口径只有一个来源：把配置推给 trading_day 模块
    from src.trading_day import set_timezone

    set_timezone(config.daily_reset_timezone)
    config.live_reconcile_enabled = _env_bool(
        "LIVE_RECONCILE_ENABLED", config.live_reconcile_enabled
    )
    config.live_fetch_attempts = max(
        1, _env_int("LIVE_FETCH_ATTEMPTS", config.live_fetch_attempts)
    )
    try:
        retry_delay = float(os.getenv("LIVE_FETCH_RETRY_DELAY_SECONDS", ""))
        config.live_fetch_retry_delay_seconds = max(0.0, retry_delay)
    except (TypeError, ValueError):
        pass
    config.log_max_bytes = max(0, _env_int("LOG_MAX_BYTES", config.log_max_bytes))
    config.log_backup_count = max(0, _env_int("LOG_BACKUP_COUNT", config.log_backup_count))
    config.live_history_max_bytes = max(
        0, _env_int("LIVE_HISTORY_MAX_BYTES", config.live_history_max_bytes)
    )
    config.live_history_backup_count = max(
        0, _env_int("LIVE_HISTORY_BACKUP_COUNT", config.live_history_backup_count)
    )
    config.live_history_load_lines = max(
        100, _env_int("LIVE_HISTORY_LOAD_LINES", config.live_history_load_lines)
    )
    config.live_state_compact_lines = max(
        100, _env_int("LIVE_STATE_COMPACT_LINES", config.live_state_compact_lines)
    )
    # 持仓盘口采集：控制采样密度与文件体积（设 0 关闭）
    config.position_path_enabled = _env_bool("POSITION_PATH_ENABLED", config.position_path_enabled)
    config.position_paths_path = (
        os.getenv("POSITION_PATHS_PATH") or config.position_paths_path
    )
    try:
        interval = float(os.getenv("POSITION_PATH_INTERVAL_SECONDS", ""))
        config.position_path_interval_seconds = max(2.0, interval)
    except (TypeError, ValueError):
        pass
    config.position_path_max_concurrent = max(
        1, _env_int("POSITION_PATH_MAX_CONCURRENT", config.position_path_max_concurrent)
    )
    config.position_path_max_bytes = max(
        0, _env_int("POSITION_PATH_MAX_BYTES", config.position_path_max_bytes)
    )
    config.position_path_backup_count = max(
        0, _env_int("POSITION_PATH_BACKUP_COUNT", config.position_path_backup_count)
    )
    # 入场前盘口采集（只读，2026-09-28）
    config.watch_path_enabled = _env_bool("WATCH_PATH_ENABLED", config.watch_path_enabled)
    config.watch_paths_path = os.getenv("WATCH_PATHS_PATH") or config.watch_paths_path
    try:
        watch_interval = float(os.getenv("WATCH_PATH_INTERVAL_SECONDS", ""))
        config.watch_path_interval_seconds = max(1.0, watch_interval)
    except (TypeError, ValueError):
        pass
    watch_sports = os.getenv("WATCH_PATH_SPORTS", "").strip()
    if watch_sports:
        config.watch_path_sports = [s.strip().upper() for s in watch_sports.split(",") if s.strip()]
    config.watch_path_max_targets = max(
        1, _env_int("WATCH_PATH_MAX_TARGETS", config.watch_path_max_targets)
    )
    config.watch_path_max_concurrent = max(
        1, _env_int("WATCH_PATH_MAX_CONCURRENT", config.watch_path_max_concurrent)
    )
    config.watch_path_max_bytes = max(
        0, _env_int("WATCH_PATH_MAX_BYTES", config.watch_path_max_bytes)
    )
    config.watch_path_backup_count = max(
        0, _env_int("WATCH_PATH_BACKUP_COUNT", config.watch_path_backup_count)
    )
    # 补单影子线（只记录，不参与任何下单）
    config.dip_add_shadow_enabled = _env_bool(
        "DIP_ADD_SHADOW_ENABLED", config.dip_add_shadow_enabled
    )
    config.dip_add_shadow_path = (
        os.getenv("DIP_ADD_SHADOW_PATH") or config.dip_add_shadow_path
    )
    # 决胜盘平分影子陪跑仓（只记录，不下单）
    config.tennis_tie_shadow_enabled = _env_bool(
        "TENNIS_TIE_SHADOW_ENABLED", config.tennis_tie_shadow_enabled
    )
    config.tennis_tie_shadow_path = (
        os.getenv("TENNIS_TIE_SHADOW_PATH") or config.tennis_tie_shadow_path
    )
    try:
        config.tennis_tie_shadow_stake_usdc = max(
            0.01, float(os.getenv("TENNIS_TIE_SHADOW_STAKE_USDC", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.tennis_tie_shadow_fee_rate = max(
            0.0, float(os.getenv("TENNIS_TIE_SHADOW_FEE_RATE", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.tennis_tie_shadow_reconcile_interval_seconds = max(
            30.0,
            float(os.getenv("TENNIS_TIE_SHADOW_RECONCILE_INTERVAL_SECONDS", "")),
        )
    except (TypeError, ValueError):
        pass
    try:
        config.tennis_tie_shadow_reconcile_min_age_seconds = max(
            0.0,
            float(os.getenv("TENNIS_TIE_SHADOW_RECONCILE_MIN_AGE_SECONDS", "")),
        )
    except (TypeError, ValueError):
        pass
    config.tennis_tie_events_enabled = _env_bool(
        "TENNIS_TIE_EVENTS_ENABLED", config.tennis_tie_events_enabled
    )
    config.tennis_tie_events_path = (
        os.getenv("TENNIS_TIE_EVENTS_PATH") or config.tennis_tie_events_path
    )
    try:
        config.tennis_tie_events_max_bytes = max(
            0, int(os.getenv("TENNIS_TIE_EVENTS_MAX_BYTES", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.tennis_tie_events_backup_count = max(
            0, int(os.getenv("TENNIS_TIE_EVENTS_BACKUP_COUNT", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.tennis_tie_shadow_timeout_seconds = max(
            3.0, float(os.getenv("TENNIS_TIE_SHADOW_TIMEOUT_SECONDS", ""))
        )
    except (TypeError, ValueError):
        pass
    config.tennis_tie_shadow_max_bytes = max(
        0,
        _env_int(
            "TENNIS_TIE_SHADOW_MAX_BYTES", config.tennis_tie_shadow_max_bytes
        ),
    )
    config.tennis_tie_shadow_backup_count = max(
        0,
        _env_int(
            "TENNIS_TIE_SHADOW_BACKUP_COUNT",
            config.tennis_tie_shadow_backup_count,
        ),
    )
    config.tennis_tie_live_enabled = _env_bool(
        "TENNIS_TIE_LIVE_ENABLED", config.tennis_tie_live_enabled
    )
    try:
        config.tennis_tie_live_order_usdc = max(
            1.0, float(os.getenv("TENNIS_TIE_LIVE_ORDER_USDC", ""))
        )
    except (TypeError, ValueError):
        pass
    # 入场价区间闸门（2026-09-23）
    # CS2 deciding-map tied-score shadow ledger (read-only, never submits orders)
    config.cs2_tie_shadow_enabled = _env_bool(
        "CS2_TIE_SHADOW_ENABLED", config.cs2_tie_shadow_enabled
    )
    config.cs2_tie_shadow_path = (
        os.getenv("CS2_TIE_SHADOW_PATH") or config.cs2_tie_shadow_path
    )
    try:
        config.cs2_tie_shadow_stake_usdc = max(
            0.01, float(os.getenv("CS2_TIE_SHADOW_STAKE_USDC", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.cs2_tie_shadow_fee_rate = max(
            0.0, float(os.getenv("CS2_TIE_SHADOW_FEE_RATE", ""))
        )
    except (TypeError, ValueError):
        pass
    config.cs2_tie_shadow_max_round_score = max(
        1,
        _env_int(
            "CS2_TIE_SHADOW_MAX_ROUND_SCORE",
            config.cs2_tie_shadow_max_round_score,
        ),
    )
    try:
        config.cs2_tie_shadow_reconcile_interval_seconds = max(
            30.0,
            float(os.getenv("CS2_TIE_SHADOW_RECONCILE_INTERVAL_SECONDS", "")),
        )
    except (TypeError, ValueError):
        pass
    try:
        config.cs2_tie_shadow_reconcile_min_age_seconds = max(
            0.0,
            float(os.getenv("CS2_TIE_SHADOW_RECONCILE_MIN_AGE_SECONDS", "")),
        )
    except (TypeError, ValueError):
        pass
    try:
        config.cs2_tie_shadow_timeout_seconds = max(
            3.0, float(os.getenv("CS2_TIE_SHADOW_TIMEOUT_SECONDS", ""))
        )
    except (TypeError, ValueError):
        pass
    config.cs2_tie_shadow_max_bytes = max(
        0,
        _env_int("CS2_TIE_SHADOW_MAX_BYTES", config.cs2_tie_shadow_max_bytes),
    )
    config.cs2_tie_shadow_backup_count = max(
        0,
        _env_int(
            "CS2_TIE_SHADOW_BACKUP_COUNT",
            config.cs2_tie_shadow_backup_count,
        ),
    )

    config.entry_band_enabled = _env_bool("ENTRY_BAND_ENABLED", config.entry_band_enabled)
    try:
        band_min = float(os.getenv("ENTRY_BAND_MIN", ""))
        config.entry_band_min = band_min
    except (TypeError, ValueError):
        pass
    try:
        band_max = float(os.getenv("ENTRY_BAND_MAX", ""))
        config.entry_band_max = band_max
    except (TypeError, ValueError):
        pass
    config.paper_signal_log_limit = max(
        0, _env_int("PAPER_SIGNAL_LOG_LIMIT", config.paper_signal_log_limit)
    )
    config.paper_equity_history_limit = max(
        0, _env_int("PAPER_EQUITY_HISTORY_LIMIT", config.paper_equity_history_limit)
    )
    config.paper_settle_reconcile_enabled = _env_bool(
        "PAPER_SETTLE_RECONCILE_ENABLED", config.paper_settle_reconcile_enabled
    )
    config.paper_settle_reconcile_path = (
        os.getenv("PAPER_SETTLE_RECONCILE_PATH") or config.paper_settle_reconcile_path
    )
    try:
        config.paper_settle_reconcile_interval_seconds = max(
            30.0, float(os.getenv("PAPER_SETTLE_RECONCILE_INTERVAL_SECONDS", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.paper_settle_reconcile_min_age_seconds = max(
            0.0, float(os.getenv("PAPER_SETTLE_RECONCILE_MIN_AGE_SECONDS", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        config.paper_settle_reconcile_timeout_seconds = max(
            3.0, float(os.getenv("PAPER_SETTLE_RECONCILE_TIMEOUT_SECONDS", ""))
        )
    except (TypeError, ValueError):
        pass
    try:
        hours = float(os.getenv("LIVE_RECONCILE_ORPHAN_HOURS", ""))
        config.live_reconcile_orphan_hours = hours
    except (TypeError, ValueError):
        pass
    sports_env = os.getenv("PM_STREAM_OVERRIDE_SPORTS")
    if sports_env is not None and sports_env.strip():
        config.pm_stream_override_sports = tuple(
            part.strip().upper() for part in sports_env.split(",") if part.strip()
        )
    try:
        stale = float(os.getenv("PM_STREAM_STALE_SECONDS", ""))
        config.pm_stream_stale_seconds = stale
    except (TypeError, ValueError):
        pass
    if config.signal_drill_mode and not config.live_dry_run:
        # 演练模式会产生大量信号；没有明确开启干跑时强制干跑，避免批量真下单
        config.live_dry_run = True
    _apply_sandbox(config)
    return config


# 会被写成"真实数据"的路径字段。测试脚本一旦跑了完整 tick（会处理真实比赛、
# 开模拟盘仓位、写信号日志），就必须把它们全部重定向到沙箱目录。
SANDBOX_PATH_FIELDS = (
    "state_path", "trades_path", "signal_events_path", "risk_settings_path",
    "score_change_state_path", "log_path",
    "probability_state_path", "probability_trades_path", "probability_events_path",
    "probability_processed_path",
    "live_orders_path", "live_order_events_path",
    "position_paths_path", "shadow_stop_path", "paper_settle_reconcile_path",
    "dip_add_shadow_path", "watch_paths_path", "tennis_tie_shadow_path",
    "cs2_tie_shadow_path",
)

# 这些前缀的脚本按约定是"探针/测试"，一跑就会写真实账本 —— 自动进沙箱。
# 工具类脚本（recalc_ / fix_ / cleanup_ / analyze_ 等）不在此列，它们本来就要动真实数据。
SANDBOX_SCRIPT_PREFIXES = ("test_", "probe_", "measure_", "debug_")


def _auto_sandbox_dir() -> str:
    """测试脚本没显式设沙箱时，自动给一个临时目录。"""
    if str(os.getenv("PMBOT_NO_SANDBOX", "") or "").strip():
        return ""
    explicit = str(os.getenv("PMBOT_SANDBOX_DIR", "") or "").strip()
    if explicit:
        return explicit
    name = os.path.basename(sys.argv[0] or "")
    if any(name.startswith(prefix) for prefix in SANDBOX_SCRIPT_PREFIXES):
        return tempfile.mkdtemp(prefix="pmbot_sandbox_")
    return ""


def _apply_sandbox(config: Config) -> None:
    """`PMBOT_SANDBOX_DIR` 存在时，把所有数据文件重定向到该目录。

    背景（2026-09-17）：`tmp/test_prematch_tick.py` 这类脚本会 `LockBot()` +
    `await bot._tick()` —— 用的是**真实 config**，于是它把信号事件写进了
    `trades/signal_events.jsonl`、把模拟盘仓位写进了 `trades/state.json`、
    还在实盘日志里留下 DRY_RUN 单。排查"某条信号为什么没触发"时才发现，
    被查的信号根本是测试进程写的（生产日志里没有对应行）。
    现在测试只要设一个环境变量 `PMBOT_SANDBOX_DIR=<临时目录>` 就彻底隔离。
    """
    sandbox = _auto_sandbox_dir()
    if not sandbox:
        return
    base = Path(sandbox)
    base.mkdir(parents=True, exist_ok=True)
    for name in SANDBOX_PATH_FIELDS:
        value = getattr(config, name, None)
        if isinstance(value, str) and value.strip():
            setattr(config, name, str(base / Path(value).name))
    print(f"[沙箱] 检测到测试/探针脚本，数据文件已重定向到 {base}", file=sys.stderr)
