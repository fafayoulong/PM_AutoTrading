# -*- coding: utf-8 -*-
"""网球胜率模型（状态模型）：给定当前比分，算出领先方的获胜概率。

**为什么是"算"而不是"训练"**
网球是少数几个可以从比分状态**精确递推**胜率的运动（Klaassen–Magnus 那套）：

    分  →  局  →  盘  →  场

每一步都只依赖"发球方赢得一分的概率"这一个参数，其余全由网球规则决定
（平分/占先、抢七、发球轮转、三盘两胜/五盘三胜）。所以：

  * 不需要历史数据训练（这也是我们能在 Sackmann 数据源下线后照样开工的原因）；
  * 参数退化时也能跑：先用手册上的巡回赛均值，等攒够自己的数据再按球员精修。

**已知局限**（照实写下来，别当成精确真理）
  1. 假设每一分的胜负独立同分布。真实网球有"势头"，文献里这个偏差不大但对
     `30-30`、`40-0` 这种关键分是可测的。
  2. 发球得分率在同一场里假设恒定：没有区分"对手接发强""关键分发球更保守"。
  3. 决胜盘 6-6 一律按抢七处理（现在四大满贯也都是抢十/抢七，老式长盘没建）。
  4. **逐分数据我们拿不到**（ESPN 只给到"局"），所以日常只能用局分状态；
     逐分接口留着，等有数据源再接。

对外主要用 `win_probability()`；下面的 `game_win_prob` / `set_win_prob` /
`match_win_prob` 都是可单独调用、可被测试校验的中间量。
"""

from __future__ import annotations

from functools import lru_cache

# 发球方赢得一分的概率（手册值，硬地大样本）：
#   ATP ≈ 0.63、WTA ≈ 0.56。这是"巡回赛平均"，不是某个球员。
# 调参入口就这一个 —— 想让模型更准，就把它换成球员各自的发球得分率。
SERVE_POINT_WIN_DEFAULT = {"ATP": 0.63, "WTA": 0.56}


def default_serve_point_win(tour: str = "ATP") -> float:
    return SERVE_POINT_WIN_DEFAULT.get(str(tour or "").upper(), 0.63)


def first_server_is_leader(leader_serving_now: bool | None, games_played: int) -> bool | None:
    """由「现在谁在发球」+「这一盘已打了几局」反推「这一盘第一局谁发球」。

    发球每局轮换，所以已打局数为偶数时两者相同、为奇数时相反。
    这个推导和 `multi_signal.infer_break_state` 里用的是同一条规则
    （那边用它推"真破发"，这边用它定模型的初始条件），**两边必须一致**。

    `leader_serving_now` 为 None（ESPN 没给 possession）时返回 None —— 调用方
    据此跳过模型，绝不要猜。
    """
    if leader_serving_now is None:
        return None
    return bool(leader_serving_now) if games_played % 2 == 0 else (not bool(leader_serving_now))


def _clamp_prob(value: float, lo: float = 0.30, hi: float = 0.85) -> float:
    """把发球得分率夹在合理区间：低于 0.30 或高于 0.85 的输入一定是数据错了。"""
    return max(lo, min(hi, float(value)))


# --------------------------------------------------------------------------
# 一分 → 一局
# --------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _game_prob(p: float, a: int, b: int) -> float:
    """发球方从「自己 a 分、接发方 b 分」开始，赢下这一局的概率。

    分值用 0/1/2/3 表示 0/15/30/40，4 表示占先(Ad)。平分之后是无穷递推，
    用解析式收口：平分时发球方最终赢下这一局的概率是
        P_d = p² / (p² + (1-p)²)
    （等价于"连得两分赢，或先丢一分再回到平分"）。
    """
    if a >= 4 and a - b >= 2:
        return 1.0
    if b >= 4 and b - a >= 2:
        return 0.0
    if a == 3 and b == 3:
        return p * p / (p * p + (1 - p) * (1 - p))          # 平分
    if a == 4 and b == 3:
        return p + (1 - p) * _game_prob(p, 3, 3)            # 占先
    if a == 3 and b == 4:
        return p * _game_prob(p, 3, 3)                      # 对方占先
    return p * _game_prob(p, a + 1, b) + (1 - p) * _game_prob(p, a, b + 1)


def game_win_prob(p_serve: float) -> float:
    """发球方从 0-0 赢下这一局的概率。"""
    return _game_prob(_clamp_prob(p_serve), 0, 0)


def game_win_prob_from_points(p_serve: float, points_self: int, points_other: int) -> float:
    """发球方从给定的分（0/15/30/40/Ad = 0/1/2/3/4）开始赢下这一局的概率。"""
    return _game_prob(_clamp_prob(p_serve), int(points_self), int(points_other))


# --------------------------------------------------------------------------
# 分 → 抢七
# --------------------------------------------------------------------------
def _tiebreak_server_is_a(points_played: int) -> bool:
    """抢七里"下一位发球的是不是 A"。

    规则：A 发第 1 分，然后每两分换人（B 发 2-3 分，A 发 4-5 分……）。
    打完 n 分时，下一位发球方由 n % 4 ∈ {0,3} → A 决定。
    """
    return points_played % 4 in (0, 3)


@lru_cache(maxsize=None)
def _tiebreak_prob(p_a: float, p_b: float, a: int, b: int, first_server_is_a: bool) -> float:
    """A 赢下抢七的概率（目标 7 分、净胜 2 分）。"""
    if a >= 7 and a - b >= 2:
        return 1.0
    if b >= 7 and b - a >= 2:
        return 0.0
    if a >= 20 and b >= 20:            # 概率质量可忽略，防止极端输入导致爆栈
        return 0.5
    played = a + b
    server_is_a_now = _tiebreak_server_is_a(played) == first_server_is_a
    p_server = p_a if server_is_a_now else p_b
    # ⚠️ 这里要的是「**A** 赢下这一分」的概率，不是「发球方赢下这一分」。
    # 2026-10-09 修（PROJECT_DESIGN #131）：原实现把 `p_server` 直接当成 A 的得分概率，
    # 于是**B 发球时也把这一分记给 A**（应该记给 A 的只有 1 − p_b）。后果：
    #   tiebreak_win_prob(0.63, 0.63) 算出 0.8514（两个完全相同的球员），
    #   整场 0-0 算出 0.6113、盘分 1-0 且当前盘 6-6 算出 0.9368（应约 0.75）。
    #   所有"能走到 6-6"的中段状态被系统性虚高 7~25pp（见 #129 的模型高估）。
    # 注意 `_set_prob` 里同一类 bug 早在 2026-09-19 就修过（局级），抢七这一支漏了。
    p_a_wins_point = p_a if server_is_a_now else (1.0 - p_b)
    # 注意：换发球规则只和"已打分数"有关，所以下一分仍然是同一套推导
    win = p_a_wins_point * _tiebreak_prob(p_a, p_b, a + 1, b, first_server_is_a)
    lose = (1 - p_a_wins_point) * _tiebreak_prob(p_a, p_b, a, b + 1, first_server_is_a)
    return win + lose


def tiebreak_win_prob(p_a: float, p_b: float, *, first_server_is_a: bool = True,
                      points_a: int = 0, points_b: int = 0) -> float:
    return _tiebreak_prob(_clamp_prob(p_a), _clamp_prob(p_b),
                          int(points_a), int(points_b), bool(first_server_is_a))


# --------------------------------------------------------------------------
# 局 → 盘
# --------------------------------------------------------------------------
def _set_first_server(games_played: int, first_server_is_a: bool) -> bool:
    """这一盘"下一位发球"是不是 A（发球每局轮换）。"""
    return first_server_is_a if games_played % 2 == 0 else (not first_server_is_a)


@lru_cache(maxsize=None)
def _set_prob(p_a: float, p_b: float, ga: int, gb: int, first_server_is_a: bool,
              tiebreak_at_66: bool = True) -> float:
    """A 赢下这一盘的概率（从局分 ga-gb 开始）。"""
    if ga >= 6 and ga - gb >= 2:
        return 1.0
    if gb >= 6 and gb - ga >= 2:
        return 0.0
    if ga >= 7 and ga - gb >= 2:
        return 1.0
    if gb >= 7 and gb - ga >= 2:
        return 0.0
    played = ga + gb
    server_is_a = _set_first_server(played, first_server_is_a)
    if ga == 6 and gb == 6:
        if not tiebreak_at_66:          # 长盘（老规则）：继续每局递推
            pass
        else:
            # 抢七的第一分由"本该发第 13 局的人"发
            return _tiebreak_prob(p_a, p_b, 0, 0, server_is_a)
    # ⚠️ 这里要的是「**A** 赢下这一局」的概率，不是「发球方赢下这一局」。
    # 2026-09-19 的初版写成 `_game_prob(p_发球方, 0, 0)` 直接当 A 的胜率用 ——
    # 发球方是 A 时两者相同，发球方是 B 时正好反了，于是把"对手发球局"也当成
    # A 的优势（模型因此严重高估）。蒙特卡洛没抓到是因为模拟器犯了同样的错，
    # 是"边界+单调性"那组断言把它逼出来的。
    p_a_wins_game = (
        _game_prob(p_a, 0, 0) if server_is_a else (1.0 - _game_prob(p_b, 0, 0))
    )
    win = p_a_wins_game * _set_prob(p_a, p_b, ga + 1, gb, first_server_is_a, tiebreak_at_66)
    lose = (1 - p_a_wins_game) * _set_prob(p_a, p_b, ga, gb + 1, first_server_is_a, tiebreak_at_66)
    return win + lose


def set_win_prob(p_a: float, p_b: float, *, games_a: int = 0, games_b: int = 0,
                 first_server_is_a: bool = True, tiebreak_at_66: bool = True) -> float:
    return _set_prob(_clamp_prob(p_a), _clamp_prob(p_b), int(games_a), int(games_b),
                     bool(first_server_is_a), bool(tiebreak_at_66))


# --------------------------------------------------------------------------
# 盘 → 场
# --------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _match_prob(p_a: float, p_b: float, sa: int, sb: int, ga: int, gb: int,
                first_server_is_a: bool, best_of: int) -> float:
    need = best_of // 2 + 1
    if sa >= need:
        return 1.0
    if sb >= need:
        return 0.0
    p_set = _set_prob(p_a, p_b, ga, gb, first_server_is_a)
    if p_set >= 1.0:
        return _match_prob(p_a, p_b, sa + 1, sb, 0, 0, not first_server_is_a, best_of)
    if p_set <= 0.0:
        return _match_prob(p_a, p_b, sa, sb + 1, 0, 0, not first_server_is_a, best_of)
    # 下一盘的第一发球方与这一盘相反（这是标准规则，不需要额外数据）
    return (p_set * _match_prob(p_a, p_b, sa + 1, sb, 0, 0, not first_server_is_a, best_of)
            + (1 - p_set) * _match_prob(p_a, p_b, sa, sb + 1, 0, 0,
                                        not first_server_is_a, best_of))


def match_win_prob(p_a: float, p_b: float, *, sets_a: int = 0, sets_b: int = 0,
                   games_a: int = 0, games_b: int = 0, first_server_is_a: bool = True,
                   best_of: int = 3) -> float:
    """A 赢下整场比赛的概率（从当前盘分/局分开始）。"""
    return _match_prob(_clamp_prob(p_a), _clamp_prob(p_b), int(sets_a), int(sets_b),
                       int(games_a), int(games_b), bool(first_server_is_a), int(best_of))


# --------------------------------------------------------------------------
# 顶层：给"我们自己的信号"用的接口
# --------------------------------------------------------------------------
def win_probability(
    *,
    leader_sets: int,
    underdog_sets: int,
    current_games_leader: int,
    current_games_underdog: int,
    leader_serves_first_in_set: bool,
    leader_serve_point_win: float | None = None,
    underdog_serve_point_win: float | None = None,
    best_of: int = 3,
    tour: str = "ATP",
    points_leader: int | None = None,
    points_underdog: int | None = None,
) -> float:
    """领先方最终赢下这场比赛的概率。

    只依赖信号里**已经有**的状态：盘分、当前盘局分、谁在这一盘先发球
    （由 `leader_serving` + 已打局数奇偶推得，见 multi_signal.infer_break_state）。

    `points_*` 是留给逐分数据的口子：现在 ESPN 拿不到，传 None 就只按局分算。
    """
    base = default_serve_point_win(tour)
    p_l = _clamp_prob(leader_serve_point_win if leader_serve_point_win else base)
    p_u = _clamp_prob(underdog_serve_point_win if underdog_serve_point_win else base)

    games_played = int(current_games_leader) + int(current_games_underdog)
    # 当前"下一位发球"的人：由这一盘的第一发球方 + 已打局数推出
    next_server_is_leader = (
        bool(leader_serves_first_in_set) if games_played % 2 == 0
        else (not bool(leader_serves_first_in_set))
    )

    if points_leader is not None and points_underdog is not None:
        # 有逐分数据：先算「**领先方**赢下当前这一局」的概率，再回到盘/场递推。
        # 注意方向：先算的是"发球方赢下这局"，发球方是对手时要取 1 减去它。
        if next_server_is_leader:
            p_game = game_win_prob_from_points(p_l, int(points_leader), int(points_underdog))
        else:
            server_wins = game_win_prob_from_points(p_u, int(points_underdog), int(points_leader))
            p_game = 1.0 - server_wins
        p_set = _set_prob_with_partial_game(
            p_l, p_u, int(current_games_leader), int(current_games_underdog),
            bool(leader_serves_first_in_set), p_game,
        )
    else:
        p_set = _set_prob(p_l, p_u, int(current_games_leader), int(current_games_underdog),
                          bool(leader_serves_first_in_set))
    # ⚠️ `best_of` 必须原样传 int。初版写成 `bool(best_of)`，3 → True → 1，
    # 于是 need = 1//2+1 = 1，"已经赢一盘"就被判成 100% —— 而我们所有真实信号
    # 都是领先一盘，等于整个模型对生产状态恒返回 1.0。是测试里
    # "1-0 保发 vs 1-0 真破发" 两个值都打印 100.00% 才暴露出来的。
    return _match_from_current_set(
        p_l, p_u, int(leader_sets), int(underdog_sets), int(best_of), p_set,
        bool(leader_serves_first_in_set),
    )


def _set_prob_with_partial_game(p_a, p_b, ga, gb, first_server_is_a, p_game_a_wins):
    """当前这一局已经打到一半时，用已知的"这一局归属概率"接上盘/场递推。"""
    if ga >= 6 and ga - gb >= 2:
        return 1.0
    if gb >= 6 and gb - ga >= 2:
        return 0.0
    if ga == 6 and gb == 6:
        return _tiebreak_prob(p_a, p_b, 0, 0, _set_first_server(ga + gb, first_server_is_a))
    return (p_game_a_wins * _set_prob(p_a, p_b, ga + 1, gb, first_server_is_a)
            + (1 - p_game_a_wins) * _set_prob(p_a, p_b, ga, gb + 1, first_server_is_a))


def _match_from_current_set(p_a, p_b, sa, sb, best_of, p_set, first_server_is_a):
    need = best_of // 2 + 1
    if sa >= need:
        return 1.0
    if sb >= need:
        return 0.0
    if p_set >= 1.0:
        return _match_prob(p_a, p_b, sa + 1, sb, 0, 0, not first_server_is_a, best_of)
    if p_set <= 0.0:
        return _match_prob(p_a, p_b, sa, sb + 1, 0, 0, not first_server_is_a, best_of)
    return (p_set * _match_prob(p_a, p_b, sa + 1, sb, 0, 0, not first_server_is_a, best_of)
            + (1 - p_set) * _match_prob(p_a, p_b, sa, sb + 1, 0, 0,
                                        not first_server_is_a, best_of))
