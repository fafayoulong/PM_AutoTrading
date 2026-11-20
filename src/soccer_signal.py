# -*- coding: utf-8 -*-
"""足球锁定信号：由胜率模型直接判定，不用「分差 + 剩余时间」的硬阈值。

为什么足球要换一种判定方式
--------------------------
篮球/橄榄球/冰球可以写「第 4 节 + 剩 2 分钟 + 领先 10 分」这种硬规则，因为这些运动
的得分节奏和时钟结构稳定。足球不行：

  * 没有"时钟走完就锁定"的保证 —— 任何时刻都可能进球；
  * 1 球领先随时被扳平，而且**平局是真实结果**（买"某队赢"在平局时归零）；
  * 同样领先 2 球，45 分钟时是 91.8%、75 分钟时是 97.6% —— 用一条阈值没法同时表达。

所以这里把判定交给模型（`src/soccer_model.py`，双泊松 + 实测进球时间分布）：

    P(领先方最终获胜) >= 阈值（默认 0.90）  → 出信号
    价格 <= P - 最小期望（默认 0.02）        → 才下单（这一步在 main 的价格闸门里）

后者是关键：模型说 95% 而市场只给 0.90 时才有 5 个点的期望；
市场给 0.96 时反而没有价值，直接跳过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import Config
from src.nba_feed import SoccerGameState
from src.soccer_model import SoccerModel

logger = logging.getLogger(__name__)


@dataclass
class SoccerSignal:
    game_id: str
    league: str
    league_tag: str
    leader_team: str          # 队名缩写（ESPN）
    underdog_team: str
    leader_name: str          # 用于盘口搜索的完整队名
    underdog_name: str
    leader_is_home: bool
    leader_score: int
    underdog_score: int
    minute: float
    period: int
    score_diff: int
    win_probability: float    # 模型给出的「领先方最终获胜」概率
    required_diff: int = 0    # 足球不用分差阈值，保留字段是为了兼容既有日志/落库
    confidence: float = 0.0
    sport: str = "SOCCER"


def evaluate_soccer_lock(
    config: Config,
    game: SoccerGameState,
    model: SoccerModel | None = None,
) -> SoccerSignal | None:
    """判断足球比赛是否进入"模型意义上的锁定"状态。"""
    if getattr(game, "status", "") != "in":
        return None
    home_score = int(game.home_score)
    away_score = int(game.away_score)
    diff = abs(home_score - away_score)
    threshold = float(getattr(config, "soccer_min_win_probability", 0.90) or 0.90)

    if getattr(config, "signal_drill_mode", False):
        # 演练模式：只要求有领先方，用来跑通下游链路
        if diff <= 0:
            return None
        probability = 1.0
    else:
        if diff <= 0:
            return None
        model = model or SoccerModel(
            home_lambda=float(getattr(game, "lambda_home", 1.573) or 1.573),
            away_lambda=float(getattr(game, "lambda_away", 1.293) or 1.293),
            max_goals=int(getattr(config, "soccer_max_goals", 10) or 10),
        )
        probability = model.leader_hold_probability(
            max(home_score, away_score),
            min(home_score, away_score),
            float(game.minute),
            leader_is_home=home_score > away_score,
        )
        if probability < threshold:
            return None

    leader_is_home = home_score > away_score
    if leader_is_home:
        leader, underdog = game.home_team, game.away_team
        leader_name, underdog_name = game.home_team_name, game.away_team_name
        leader_score, underdog_score = home_score, away_score
    else:
        leader, underdog = game.away_team, game.home_team
        leader_name, underdog_name = game.away_team_name, game.home_team_name
        leader_score, underdog_score = away_score, home_score

    return SoccerSignal(
        game_id=game.game_id,
        league=game.league,
        league_tag=game.league_tag,
        leader_team=leader,
        underdog_team=underdog,
        leader_name=leader_name,
        underdog_name=underdog_name,
        leader_is_home=leader_is_home,
        leader_score=leader_score,
        underdog_score=underdog_score,
        minute=float(game.minute),
        period=int(game.period),
        score_diff=diff,
        win_probability=probability,
        confidence=min(probability / max(threshold, 1e-6), 1.5),
        sport="SOCCER",
    )


def soccer_price_gate(config: Config, signal: SoccerSignal, price: float) -> str | None:
    """足球专用的价格闸门：期望值不够就不下单。

    与其它运动的 `_entry_price_gate` 不同，这里用**模型概率**当锚：
        price <= P(领先方获胜) - 最小期望
    这样"模型 95% / 市场 0.90"会通过，而"模型 95% / 市场 0.96"会被挡掉 ——
    高价单的风险不是会输，而是赢面已经被价格吃干净了。
    """
    if price <= 0:
        return "PRICE_INVALID"
    if getattr(config, "signal_drill_mode", False):
        return None
    # 分差 × 时间分档（2026-09-20）。依据：2314 场德甲/德乙历史数据表明
    #   "领先 3 球"任何时间都 ≥96.6%，而"领先 2 球"在 45′ 之前只有 90.7~91.3% ——
    #   我们的成交价大约 0.88~0.92，所以 45′ 前的 2 球领先是**负期望**。
    #   1 球领先暂不限制（用户要求先观察）。
    try:
        diff = abs(int(signal.leader_score) - int(signal.underdog_score))
        minute = float(getattr(signal, "minute", 0.0) or 0.0)
    except (TypeError, ValueError):
        diff, minute = 0, 0.0
    anytime = int(getattr(config, "soccer_diff_anytime", 3) or 3)
    needs = int(getattr(config, "soccer_diff_needs_minute", 2) or 2)
    min_minute = float(getattr(config, "soccer_min_minute", 45.0) or 45.0)
    if needs <= diff < anytime and minute < min_minute:
        return "SOCCER_TIME_TOO_EARLY"
    # 末段只做 ≥2 球领先（2026-09-21，见 PROJECT_DESIGN #116）。
    # 48 小时内 4 笔亏损都发生在 80 分钟以后、且全是"1 球领先被追平"：
    #   FUL 1-0@86' → 91' 被追平；BET 1-0@86' → 87' 被追平（入场后 86 秒）。
    # 历史表里「1 球领先 80-90' = 88.4%」是欧洲联赛 + 90 分钟口径的均值：
    # 既不含补时，也不含 MLS（实测 MLS 90 分钟以后的进球占 18.4%）。
    late_minute = float(getattr(config, "soccer_late_minute", 85.0) or 0.0)
    late_diff = int(getattr(config, "soccer_late_min_diff", 2) or 0)
    if late_minute > 0 and late_diff > 0 and minute >= late_minute and diff < late_diff:
        return "SOCCER_LATE_LEAD_TOO_SMALL"
    rules = (getattr(config, "sport_rules", {}) or {}).get("SOCCER")
    if rules is not None and price > float(rules.max_entry_price):
        return "PRICE_ABOVE_LIMIT"
    if rules is not None and float(rules.live_min_entry_price) > 0:
        if price < float(rules.live_min_entry_price):
            return "PRICE_BELOW_LOCK_THRESHOLD"
    edge = float(signal.win_probability) - price
    if edge < float(getattr(config, "soccer_min_edge", 0.02) or 0.0):
        return "SOCCER_EDGE_TOO_LOW"
    return None
