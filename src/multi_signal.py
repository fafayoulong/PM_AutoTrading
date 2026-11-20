# -*- coding: utf-8 -*-
"""Signal engines for MLB and tennis lock detection."""

import logging
import re
from dataclasses import dataclass

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class MLBSignal:
    game_id: str
    leader_team: str
    underdog_team: str
    leader_score: int
    underdog_score: int
    inning: int
    status_detail: str
    score_diff: int


@dataclass
class TennisSignal:
    match_id: str
    leader_name: str
    underdog_name: str
    sets_leader: int
    sets_underdog: int
    current_set_leader: int
    current_set_underdog: int
    status_detail: str
    # ---- 破发影子字段（2026-09-16，只记录不改判定）----
    # 「领先 1 局」不等于「多一个破发」：6-5 可能只是双方各自保发（各赢了自己的发球局，
    # 领先方只是多打了一局发球）。靠 ESPN 的 possession + 局数奇偶能推出真破发。
    leader_serving: bool | None = None      # 领先方是否正在发球（None = 数据里没有）
    leader_min_breaks: int | None = None    # 领先方**至少**破发几次（None = 推不出来）
    break_up: bool | None = None            # leader_min_breaks >= 1 的布尔形式

    @property
    def game_id(self) -> str:
        return self.match_id


@dataclass
class FootballSignal:
    """美式橄榄球的终局锁定信号（NFL / CFB 共用）。"""

    game_id: str
    leader_team: str
    underdog_team: str
    leader_score: int
    underdog_score: int
    quarter: int
    clock_seconds: float
    score_diff: int
    required_diff: int
    confidence: float


def required_football_diff(config: Config, clock_seconds: float):
    """按剩余时间给出"锁定"所需的最小分差。

    美式橄榄球一次球权最多 8 分（达阵+两分），还能用赌博踢抢回球权，
    所以要求比篮球严格得多，而且必须带时间上限（见下面 2026-09-13 的教训）：
      剩余 ≤40 秒 ：8 分（一次球权最多追平，且时间不够再进攻）
      剩余 ≤120 秒：10 分（要达阵+两分+赌博踢+射门，概率极低）
      剩余 ≤300 秒：16 分
      剩余 ≤600 秒：21 分
      剩余 ≤900 秒：28 分
      超过 900 秒（或非第 4 节）：返回 None，不参与判定

    为什么要有 600/900 秒这两档：只在"第 4 节 + 分差 ≥16"触发时，
    比赛刚进第四节（还剩 900 秒 = 整整一节、十几个球权）就会开仓，
    而差 17~21 分远谈不上锁定 —— 2026-09-13 的 OSU 23-3（第 4 节 889 秒，
    市场当时只给 0.92）就是这么亏掉的。
    """
    if clock_seconds <= float(getattr(config, "football_last_seconds", 40)):
        return int(getattr(config, "football_last_min_score_diff", 8))
    if clock_seconds <= float(getattr(config, "football_late_clock_seconds", 120)):
        return int(getattr(config, "football_late_min_score_diff", 10))
    if clock_seconds <= float(getattr(config, "football_clock_seconds", 300)):
        return int(getattr(config, "football_min_score_diff", 16))
    if clock_seconds <= float(getattr(config, "football_mid_clock_seconds", 600)):
        return int(getattr(config, "football_mid_min_score_diff", 21))
    if clock_seconds <= float(getattr(config, "football_wide_clock_seconds", 900)):
        return int(getattr(config, "football_wide_min_score_diff", 28))
    return None


def evaluate_football_lock(config: Config, game) -> FootballSignal | None:
    """判断美式橄榄球比赛是否进入"锁定"状态。"""
    if getattr(game, "status", "") != "in":
        return None
    diff = abs(int(game.home_score) - int(game.away_score))
    drill = getattr(config, "signal_drill_mode", False)

    if drill:
        if diff <= 0:
            return None
        required = 0
        confidence = 0.0
    else:
        if game.quarter < int(getattr(config, "football_min_quarter", 4)):
            return None
        required = required_football_diff(config, game.clock_seconds)
        if required is None or diff < required:
            return None
        confidence = min(diff / max(required, 1), 1.5)

    if game.home_score > game.away_score:
        leader, underdog = game.home_team, game.away_team
        leader_score, underdog_score = game.home_score, game.away_score
    else:
        leader, underdog = game.away_team, game.home_team
        leader_score, underdog_score = game.away_score, game.home_score

    return FootballSignal(
        game_id=game.game_id,
        leader_team=leader,
        underdog_team=underdog,
        leader_score=leader_score,
        underdog_score=underdog_score,
        quarter=int(game.quarter),
        clock_seconds=float(game.clock_seconds),
        score_diff=diff,
        required_diff=required,
        confidence=confidence,
    )

@dataclass
class CollegeBasketballSignal:
    """大学篮球（NCAAB 男篮 2 半场 / NCAAW 女篮 4 节）的终局锁定信号。"""

    game_id: str
    leader_team: str
    underdog_team: str
    leader_score: int
    underdog_score: int
    period: int
    clock_seconds: float
    score_diff: int
    required_diff: int
    confidence: float
    sport: str = "NCAAB"


def required_college_basketball_diff(config: Config, clock_seconds: float):
    """大学篮球"锁定"所需的最小分差；返回 None 表示当前剩余时间下不参与判定。

    模型形状与篮球一致（分差随剩余时间线性收紧），但默认比 NBA 严格：
        required = cbb_min_score_diff + 剩余分钟数 × cbb_score_diff_per_minute
    默认 (7 分, 每分钟 +1 分, 上限 240 秒) 展开成：
        剩余 240 秒 → 11 分   剩余 120 秒 → 9 分   剩余 0 秒 → 7 分

    为什么比 NBA 严格：大学篮球最后两分钟是犯规停表 + 三分的混合体，
    "领先 8-10 分还剩 1 分钟"在这种节奏下并不罕见地被追平；
    而 NBA 的 min_quarter=4 那套阈值是给 48 分钟、24 秒进攻时限的节奏调的。
    """
    if clock_seconds < float(getattr(config, "cbb_min_clock_seconds", 0)):
        return None
    if clock_seconds > float(getattr(config, "cbb_max_clock_seconds", 240)):
        return None
    minutes = max(clock_seconds, 0.0) / 60.0
    base = float(getattr(config, "cbb_min_score_diff", 7))
    per_minute = float(getattr(config, "cbb_score_diff_per_minute", 1.0))
    return int(round(base + minutes * per_minute))


def evaluate_college_basketball_lock(config: Config, game, sport: str = "NCAAB"):
    """判断大学篮球比赛是否进入"锁定"状态。

    NCAAB 是 2 个半场、NCAAW 是 4 节 —— 节次阈值分别取
    `ncaab_min_period` / `ncaaw_min_period`（2026-09-14 用历史比赛核对过
    ESPN 的 period 语义：男篮 Final 时 period=2、女篮 period=4）。
    """
    if getattr(game, "status", "") != "in":
        return None
    sport = str(sport).upper()
    period = int(getattr(game, "quarter", 0) or 0)
    min_period = int(
        getattr(config, "ncaaw_min_period" if sport == "NCAAW" else "ncaab_min_period", 2)
    )
    diff = abs(int(game.home_score) - int(game.away_score))
    drill = getattr(config, "signal_drill_mode", False)

    if drill:
        if diff <= 0:
            return None
        required = 0
        confidence = 0.0
    else:
        if period < min_period:
            return None
        required = required_college_basketball_diff(config, float(game.clock_seconds))
        if required is None or diff < required:
            return None
        confidence = min(diff / max(required, 1), 1.5)

    if game.home_score > game.away_score:
        leader, underdog = game.home_team, game.away_team
        leader_score, underdog_score = game.home_score, game.away_score
    else:
        leader, underdog = game.away_team, game.home_team
        leader_score, underdog_score = game.away_score, game.home_score

    return CollegeBasketballSignal(
        game_id=game.game_id,
        leader_team=leader,
        underdog_team=underdog,
        leader_score=leader_score,
        underdog_score=underdog_score,
        period=period,
        clock_seconds=float(game.clock_seconds),
        score_diff=diff,
        required_diff=required,
        confidence=confidence,
        sport=sport,
    )


@dataclass
class HockeySignal:
    """冰球（NHL）的终局锁定信号。"""

    game_id: str
    leader_team: str
    underdog_team: str
    leader_score: int
    underdog_score: int
    period: int
    clock_seconds: float
    score_diff: int
    required_diff: int
    confidence: float


def required_hockey_diff(config: Config, clock_seconds: float):
    """冰球的"锁定"所需分差；返回 None 表示当前剩余时间下不参与判定。

    冰球与篮球/橄榄球最大的不同：**常规时间打平会进加时/点球**，
    所以 1 球领先在终场哨响前都不是锁定（对手追平就能拖进加时，而我们买的是
    "全场胜者"）。2026-10-30 起使用以下分档：
      第 3 节 + 剩余 ≤120 秒：2 球
      第 3 节 + 剩余 ≤300 秒：2 球
      第 3 节 + 剩余 ≤600 秒：3 球
      第 3 节 + 剩余 ≤900 秒：4 球
      剩余 >900 秒：不参与
    """
    if clock_seconds <= float(getattr(config, "hockey_last_clock_seconds", 120)):
        return int(getattr(config, "hockey_min_score_diff", 2))
    if clock_seconds <= float(getattr(config, "hockey_wide_clock_seconds", 300)):
        return int(getattr(config, "hockey_wide_min_score_diff", 2))
    if clock_seconds <= float(getattr(config, "hockey_extended_clock_seconds", 600)):
        return int(getattr(config, "hockey_extended_min_score_diff", 3))
    if clock_seconds <= float(getattr(config, "hockey_very_wide_clock_seconds", 900)):
        return int(getattr(config, "hockey_very_wide_min_score_diff", 4))
    return None


def evaluate_hockey_lock(config: Config, game) -> HockeySignal | None:
    """判断冰球比赛是否进入"锁定"状态。"""
    if getattr(game, "status", "") != "in":
        return None
    period = int(getattr(game, "quarter", 0) or 0)
    diff = abs(int(game.home_score) - int(game.away_score))
    drill = getattr(config, "signal_drill_mode", False)

    if drill:
        if diff <= 0:
            return None
        required = 0
        confidence = 0.0
    else:
        if period < int(getattr(config, "hockey_min_period", 3)):
            return None
        # 加时（period 4）/点球（5）不参与：任何一次射门都能结束比赛
        if period > int(getattr(config, "hockey_min_period", 3)):
            return None
        required = required_hockey_diff(config, float(game.clock_seconds))
        if required is None or diff < required:
            return None
        confidence = min(diff / max(required, 1), 1.5)

    if game.home_score > game.away_score:
        leader, underdog = game.home_team, game.away_team
        leader_score, underdog_score = game.home_score, game.away_score
    else:
        leader, underdog = game.away_team, game.home_team
        leader_score, underdog_score = game.away_score, game.home_score

    return HockeySignal(
        game_id=game.game_id,
        leader_team=leader,
        underdog_team=underdog,
        leader_score=leader_score,
        underdog_score=underdog_score,
        period=period,
        clock_seconds=float(game.clock_seconds),
        score_diff=diff,
        required_diff=required,
        confidence=confidence,
    )


def parse_mlb_inning(detail: str) -> int:
    match = re.search(r"(\d+)", detail)
    return int(match.group(1)) if match else 0


def set_is_decided(leader_games: int, other_games: int) -> bool:
    """这一盘是否已经分出胜负（用于把"进行中的那盘"排除在盘分之外）。

    网球一盘的判定：至少 6 局且净胜 ≥2（6-4、6-0…），或者 7-6 / 7-5（抢七）。
    只有满足它才算"赢下一盘"，否则只是这一盘暂时领先几局。
    """
    return (leader_games >= 6 and leader_games - other_games >= 2) or (
        leader_games == 7 and other_games in (5, 6)
    )


def count_decided_sets(ls1: list, ls2: list) -> tuple[int, int]:
    """左右两侧各自已经确定赢下的盘数（不含进行中的那一盘）。"""
    first = sum(1 for a, b in zip(ls1, ls2) if set_is_decided(int(a), int(b)))
    second = sum(1 for a, b in zip(ls1, ls2) if set_is_decided(int(b), int(a)))
    return first, second


def tennis_watch_state(match: dict) -> tuple[str, str] | None:
    """比赛是否已进入「潜在锁定」状态 —— **只给入场前盘口采样用**（2026-09-28）。

    这里**不参与任何交易判定**，只回答"这个盘口值不值得开始记录价格路径"。
    满足任一条就开始采：

      A) `SET_LEAD` —— 盘分已经分出领先、且当前盘还没结束（领先方在逼近拿下比赛）；
      B) `SET_DECIDING` —— 盘分还是持平（含 0-0），但当前盘有人领先 ≥2 局且已过 3 局
         （"快要赢下这一盘"）。

    为什么要 B（2026-09-28 当晚补）：第一版只做了 A，而 A 要求"已经分出盘分"——
    可**决定盘分的那一瞬间**（领先方赢下这一盘）恰恰是市场反应最剧烈的一段，
    而那一段在我们的窗口之外：实测第一场（Zheng Wushuang vs Harriet Dart，
    game 184178）从开始采到我们入场，盘口从 0.82 一路走到 0.86 —— 也就是说
    我们只看到了反应的后半段，最该看的前半段被窗口切掉了。
    B 让采样从"即将赢下一盘"就开始，把那段让分/保发的定价过程一起记下来。

    返回 (领先方名字, 状态说明)；不满足返回 None。
    """
    p1 = match.get("player1", {}) or {}
    p2 = match.get("player2", {}) or {}
    for player in (p1, p2):
        name = str(player.get("name") or "").strip()
        if name in ("", "?"):
            return None
    ls1 = list(p1.get("linescores") or [])
    ls2 = list(p2.get("linescores") or [])
    if not ls1 or not ls2:
        return None
    latest = min(len(ls1), len(ls2)) - 1
    cur1, cur2 = int(ls1[latest]), int(ls2[latest])
    # 当前盘已经结束（比赛打完 / 新一盘还没进数据）→ 等下一 tick。
    # 必须**两个方向都判**：`set_is_decided` 是有方向性的（6-3 算分出胜负，
    # 3-6 不算），只判一个方向会把"对手刚赢下这一盘"误当成"这一盘还在打"。
    if set_is_decided(cur1, cur2) or set_is_decided(cur2, cur1):
        return None
    sets1, sets2 = count_decided_sets(ls1, ls2)

    if sets1 != sets2:
        leader_is_p1 = sets1 > sets2
        leader = p1 if leader_is_p1 else p2
        leader_sets, other_sets = (sets1, sets2) if leader_is_p1 else (sets2, sets1)
        games, other_games = (cur1, cur2) if leader_is_p1 else (cur2, cur1)
        return str(leader.get("name") or ""), (
            f"SET_LEAD sets {leader_sets}-{other_sets}; games {games}-{other_games}"
        )

    if abs(cur1 - cur2) >= 2 and max(cur1, cur2) >= 3:
        leader_is_p1 = cur1 > cur2
        leader = p1 if leader_is_p1 else p2
        games, other_games = (cur1, cur2) if leader_is_p1 else (cur2, cur1)
        return str(leader.get("name") or ""), (
            f"SET_DECIDING sets {sets1}-{sets2}; games {games}-{other_games}"
        )
    return None


def infer_break_state(
    leader_games: int, underdog_games: int, leader_serving: bool | None
) -> tuple[bool | None, int | None]:
    """推断当前盘的「真破发」状态，返回 (领先方是否在发球, 领先方至少破发几次)。

    为什么要它（2026-09-16）：原判定只要求「当前盘领先 ≥1 局」，而网球里
    **领先一局不等于多一个破发** —— 发球轮转，6-5 可能只是双方各自保发、
    领先方恰好多打了一局发球。这种"假锁定"在实盘上是负期望区间
    （0.85-0.92 档 13 笔合计 -1.33）。ESPN 的 `possession` 明确给出谁在发球，
    配合局数奇偶就能把真破发算出来。

    推导（只用局分 + 谁在发球，不需要逐分数据）：

      * 当前这局的发球方已知；设已打完 n 局、当前是第 n+1 局。
      * n 为偶数 → 第 n+1 局与第 1 局同发球方；n 为奇数 → 相反。
      * 先发球的那一方打了 ceil(n/2) 局、另一方 floor(n/2) 局。
      * 领先方赢下的局数若是**多于**他打过的发球局数，多出来的部分必然是破发
        （自己的发球局最多只能"保住"这么多局）。

    自检过的对照（领先方 = 前者）：
      1-0 对手发球（领先方先发）→ 破发 0（各自保发）
      1-0 领先方发球           → 破发 1（第一局就破了）
      5-4 领先方发球           → 破发 1（发球胜盘局）
      5-4 对手发球             → 破发 0（对手发球保盘）
      6-5 领先方发球           → 破发 1
      6-5 对手发球             → 破发 0
      3-0 对手发球 / 4-0 领先方发球 → 破发 1 / 2（双破发）

    `leader_serving` 为 None（数据里没有 possession）时返回 (None, None)，
    调用方据此把状态记为"未知" —— **绝不能把未知当成"没破发"去拦信号**。
    """
    if leader_serving is None or leader_games + underdog_games <= 0:
        return (leader_serving if isinstance(leader_serving, bool) else None), None
    games = int(leader_games) + int(underdog_games)
    # 第 1 局的发球方：局数为偶数时与当前发球方相同，奇数时相反
    first_server_is_leader = leader_serving if games % 2 == 0 else (not leader_serving)
    leader_served = (games + 1) // 2 if first_server_is_leader else games // 2
    return bool(leader_serving), max(0, int(leader_games) - leader_served)


def evaluate_mlb_lock(config: Config, game: dict) -> MLBSignal | None:
    if game.get("status") != "in":
        return None

    inning = parse_mlb_inning(game.get("status_detail", ""))
    diff = abs(game["home_score"] - game["away_score"])

    if getattr(config, "signal_drill_mode", False):
        if diff <= 0:
            return None
    else:
        if inning < config.mlb_min_inning:
            return None
        if diff < config.mlb_min_score_diff:
            return None

    if game["home_score"] > game["away_score"]:
        leader, underdog = game["home_team"], game["away_team"]
        leader_score, underdog_score = game["home_score"], game["away_score"]
    else:
        leader, underdog = game["away_team"], game["home_team"]
        leader_score, underdog_score = game["away_score"], game["home_score"]

    return MLBSignal(
        game_id=game["game_id"],
        leader_team=leader,
        underdog_team=underdog,
        leader_score=leader_score,
        underdog_score=underdog_score,
        inning=inning,
        status_detail=game.get("status_detail", ""),
        score_diff=diff,
    )


def evaluate_tennis_lock(config: Config, match: dict) -> TennisSignal | None:
    p1 = match.get("player1", {})
    p2 = match.get("player2", {})
    ls1 = p1.get("linescores", [])
    ls2 = p2.get("linescores", [])

    # 名字缺失的场次（例如 ESPN 的双打）不能参与判定：用 "?" 去搜市场不但搜不到，
    # 还有小概率误匹配到别的盘口。这里再兜一层，保证永远不产生无名信号。
    for player in (p1, p2):
        name = str(player.get("name") or "").strip()
        if name in ("", "?"):
            return None

    drill = getattr(config, "signal_drill_mode", False)
    min_sets = 1 if drill else 2
    if len(ls1) < min_sets or len(ls2) < min_sets:
        return None

    pairs = list(zip(ls1, ls2))
    # 盘分只能算"已经分出胜负的盘"。
    # 原来把进行中的那一盘按"当前局数领先"也算成赢盘，导致真实盘分 1-1
    # （第 3 盘刚 1-0）被读成 2-1，凭空造出一个"锁定"信号
    # （2026-09-12 Tiafoe vs Shelton 就是这样在 0.30 买入并亏掉的）。
    sets_p1, sets_p2 = count_decided_sets(ls1, ls2)

    # 最后一盘已经分出胜负：要么比赛已结束、要么新一盘的条目还没出现在数据里，
    # 此时"当前盘领先局数"没有意义 —— 跳过，等下一个 tick。
    if pairs and not drill and set_is_decided(int(pairs[-1][0]), int(pairs[-1][1])):
        return None

    if sets_p1 == sets_p2:
        return None

    if sets_p1 > sets_p2:
        leader, underdog = p1, p2
        leader_sets, underdog_sets = sets_p1, sets_p2
    else:
        leader, underdog = p2, p1
        leader_sets, underdog_sets = sets_p2, sets_p1

    latest_idx = min(len(ls1), len(ls2)) - 1
    leader_current = leader["linescores"][latest_idx]
    underdog_current = underdog["linescores"][latest_idx]

    if not drill:
        if leader_current <= underdog_current:
            return None
        if leader_current - underdog_current < config.tennis_min_game_diff_in_set:
            return None

    # 破发影子字段：只记录、不参与下面的判定（判定条件上面已经全部判完了）
    leader_serving, leader_min_breaks = infer_break_state(
        int(leader_current), int(underdog_current), leader.get("serving")
    )

    return TennisSignal(
        match_id=match.get("match_id", ""),
        leader_name=leader.get("name", "?"),
        underdog_name=underdog.get("name", "?"),
        sets_leader=leader_sets,
        sets_underdog=underdog_sets,
        current_set_leader=leader_current,
        current_set_underdog=underdog_current,
        status_detail=match.get("status_detail", ""),
        leader_serving=leader_serving,
        leader_min_breaks=leader_min_breaks,
        break_up=(leader_min_breaks >= 1) if leader_min_breaks is not None else None,
    )
