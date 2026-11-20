# -*- coding: utf-8 -*-
"""足球胜率模型的初步版本（纯函数，不联网、不下单）。

思路
----
用「双泊松 + 时间强度」这个最朴素的框架：

    P(主胜) = Σ P(主队剩余进 X 球) × P(客队剩余进 Y 球)  [X + 主队领先 > Y]

其中剩余期望进球 = 该队全场期望进球 × 「比赛还剩多少比例的进球没发生」。
最后那个比例是足球特有的：进球不是均匀分布的（下半场明显多于上半场、
终场前有抬升），所以不能简单用 (90-m)/90。

参数来源（2026-09-14 实测抓取，脚本 tmp/fetch_soccer_data.js）：
  * 全场/半场比分：football-data.co.uk，6 个联赛 × 3 赛季 = 5868 场
  * 进球分钟：OpenLigaDB（德国联赛开放数据），8 个赛季 = 7544 个进球

已知局限（建模时必须带着看）：
  1. 时间强度取自德甲/德乙，形状假定可迁移到其它联赛；
  2. 只用了「联赛平均」的期望进球，没有球队实力、红牌、比分领先后的战术变化；
  3. 泊松假设意味着进球相互独立 —— 实际有相关性（领先方收缩、落后方压上）。
     校准表（tmp/fit_soccer_model.py 输出）就是用来暴露这一点的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 每分钟的进球占比（1..90），默认值由 7544 个进球拟合得到，见 DEFAULT_MINUTE_WEIGHTS。
# 这里存成「每 5 分钟一档」的平滑形状，避免直接对着样本噪声过拟合。
DEFAULT_BIN_COUNT = 18  # 每 5 分钟一档：0-5, 5-10, ..., 85-90


@dataclass
class SoccerModel:
    """双泊松胜率模型。

    home_lambda / away_lambda：两队「全场 90 分钟」的期望进球（λ）。
    minute_weights：长度 18 的数组，表示每 5 分钟区间内发生的进球占全场比例
        （和为 1）。默认值来自实测数据，可直接替换。
    """

    home_lambda: float = 1.55
    away_lambda: float = 1.25
    minute_weights: list[float] = field(default_factory=list)
    max_goals: int = 10

    def __post_init__(self) -> None:
        if not self.minute_weights:
            self.minute_weights = list(DEFAULT_MINUTE_WEIGHTS)
        total = sum(self.minute_weights)
        if total <= 0:
            raise ValueError("minute_weights 之和必须为正")
        self.minute_weights = [w / total for w in self.minute_weights]

    # ---------- 时间维度 ----------

    def remaining_share(self, minute: float, added_time: float = 0.0) -> float:
        """从第 minute 分钟起，比赛还剩多少比例的进球没发生（0~1）。

        added_time 用来表达补时：传入 4 表示"还要再踢 4 分钟才结束"，
        这段的进球率按最后一档（85-90）的比例折算。
        """
        if minute <= 0:
            return 1.0
        if minute >= 90:
            return 0.0
        bin_size = 90.0 / len(self.minute_weights)
        index = int(minute // bin_size)
        # 当前档内已过去的部分按线性折算
        within = (minute - index * bin_size) / bin_size
        remaining = sum(self.minute_weights[index + 1 :]) + self.minute_weights[index] * (1 - within)
        if added_time > 0:
            last = self.minute_weights[-1]
            remaining += last * (added_time / bin_size)
        return max(0.0, min(1.0, remaining))

    # ---------- 概率计算 ----------

    @staticmethod
    def _poisson_pmf(lam: float, k: int) -> float:
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * lam**k / math.factorial(k)

    def outcome_probabilities(
        self,
        home_score: int,
        away_score: int,
        minute: float,
        *,
        added_time: float = 0.0,
    ) -> dict:
        """返回 {home_win, draw, away_win} 三个概率（和为 1）。"""
        share = self.remaining_share(minute, added_time)
        lam_h = self.home_lambda * share
        lam_a = self.away_lambda * share
        lead = int(home_score) - int(away_score)

        home_pmf = [self._poisson_pmf(lam_h, k) for k in range(self.max_goals + 1)]
        away_pmf = [self._poisson_pmf(lam_a, k) for k in range(self.max_goals + 1)]

        home_win = draw = away_win = 0.0
        for x, px in enumerate(home_pmf):
            for y, py in enumerate(away_pmf):
                p = px * py
                final = lead + x - y
                if final > 0:
                    home_win += p
                elif final == 0:
                    draw += p
                else:
                    away_win += p
        total = home_win + draw + away_win
        if total <= 0:  # pragma: no cover - 理论上不可能
            return {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
        return {
            "home_win": home_win / total,
            "draw": draw / total,
            "away_win": away_win / total,
        }

    def leader_hold_probability(
        self,
        leader_score: int,
        trailer_score: int,
        minute: float,
        *,
        leader_is_home: bool,
        added_time: float = 0.0,
    ) -> float:
        """领先方最终获胜（不含平局）的概率 —— 对应策略里「买领先方 YES」的胜率。"""
        home_score = leader_score if leader_is_home else trailer_score
        away_score = trailer_score if leader_is_home else leader_score
        probs = self.outcome_probabilities(home_score, away_score, minute, added_time=added_time)
        return probs["home_win"] if leader_is_home else probs["away_win"]


# 每 5 分钟一档的进球占比（18 档，和为 1）。
# 先由 tmp/fit_soccer_model.py 用 7544 个真实进球拟合，再把结果固化到代码里；
# 这样运行时不依赖数据文件，而重新拟合只要跑一次那个脚本。
DEFAULT_MINUTE_WEIGHTS: list[float] = [
    # 0-5, 5-10, …, 85-90（每档占比，实测拟合值）
    0.0398, 0.0457, 0.0491, 0.0489, 0.0495, 0.0508, 0.0510, 0.0534, 0.0575,
    0.0576, 0.0574, 0.0572, 0.0538, 0.0532, 0.0552, 0.0546, 0.0684, 0.0969,
]
