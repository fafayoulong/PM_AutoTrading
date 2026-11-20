# -*- coding: utf-8 -*-
"""Polymarket 体育实时流的「ESPN 覆盖层」。

Polymarket 自带的体育比分流（上游为 Sportradar）在事件发生后数百毫秒内
推送状态变化，比 ESPN 记分板轮询（默认 3 秒）更快。把 PM 的最新比分叠加到
ESPN 的比赛行上，可以让扫尾盘信号更早触发，抢到更长的赔率收敛窗口。

安全约束（很重要，全部为「失败即回退」）：

1. 只有当 PM 比分与 ESPN 比分**逐队完全一致**过一次之后，才允许覆盖。
   队名映射错误、比分格式识别错误时，两者永远对不上，因此永远不会覆盖，
   下单决策继续使用 ESPN 数据。
2. 比分顺序（客-主 / 主-客）由校验过程自动判定：一致的那一侧才会被采纳，
   因此即使 PM 的比分是「主-客」顺序也不会把领先方判反。
3. 只覆盖比分与数据来源标记；局数 / 剩余时钟 / 赛程仍以 ESPN 为准
   （这些字段变化慢，且直接决定信号阈值）。
4. 比分单调不减：出现回退（小于当前 ESPN 比分）时丢弃该条 PM 数据。
5. PM 数据过期（超过 stale 秒）时自动退回 ESPN。
"""

import difflib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import Config

logger = logging.getLogger(__name__)

# PM 流的 leagueAbbreviation → 内部运动名
PM_LEAGUES: dict[str, str] = {
    "mlb": "MLB",
    "nba": "NBA",
    "wnba": "WNBA",
    # 美式橄榄球 / 冰球：流里给的是「队名缩写」（如 TXAM），需要缩写等价规则
    # 才能与 ESPN 的队名/缩写对上（见 _team_similarity）。
    "nfl": "NFL",
    "cfb": "CFB",
    "ncaaf": "CFB",  # 备用写法：若流改用它，也能接上
    "nhl": "NHL",
    # 网球：流里**没有** "tennis" 这个标签，实测是巡回赛代码
    # （wta / challenger，atp 同理）。另有 eventState.type 兜底，见 EVENT_STATE_SPORTS。
    "wta": "TENNIS",
    "atp": "TENNIS",
    "challenger": "TENNIS",
}

# payload.eventState.type → 内部运动名。比 leagueAbbreviation 更可靠，
# 用来兜住"巡回赛代码没登记"的情况。
EVENT_STATE_SPORTS: dict[str, str] = {
    "tennis": "TENNIS",
}

# 这些运动的行里 *_team 存的是"名字"而不是"缩写"，不能当缩写去比对
ABBR_FREE_SPORTS = {"TENNIS"}

# 缩写别名表：同一支球队在两个数据源里缩写不同时，在这里登记等价关系。
# 键值两侧都会先做 normalize_abbr()，所以 TA&M / T-A&M 都是 TAM。
# 注意：这张表只用来「放宽匹配」，匹配错也不会写到决策里 ——
# 覆盖层仍然要求比分逐队一致过一次才生效（见 5.3.1）。
ABBR_CANON: dict[str, str] = {
    "TXAM": "TAM",   # 德州农工：PM 流 TXAM，ESPN TA&M
    "TAMU": "TAM",
    "MIZZ": "MIZ",   # 密苏里：PM 流 MIZZ，ESPN MIZ
    "BAMA": "ALA",   # 阿拉巴马
    "PITT": "PITT",
    "MASS": "UMASS",
}

# 两个缩写至少要有这么像才认为是同一支球队（宁可不覆盖，也不覆盖错）
ABBR_MIN_RATIO = 0.75
# 整对球队（客+主）的最低匹配分：低于它就不认这场比赛
PAIR_MIN_SCORE = 0.7

_TEAM_NOISE = {
    "fc", "cf", "sc", "ac", "afc", "bk", "club", "the", "de", "cd",
}


def normalize_team(name: Any) -> str:
    """队名归一化：去重音符、标点、噪音词，便于跨数据源匹配。"""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [t for t in text.split() if t and t not in _TEAM_NOISE]
    return " ".join(tokens)


def _nickname(normalized: str) -> str:
    tokens = normalized.split()
    return tokens[-1] if tokens else ""


def normalize_abbr(value: Any) -> str:
    """缩写归一化：只保留字母数字并大写（``TA&M`` → ``TAM``）。"""
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def canonical_abbr(value: Any) -> str:
    """缩写归一化后再套一次别名表。"""
    abbr = normalize_abbr(value)
    return ABBR_CANON.get(abbr, abbr)


def abbr_similarity(left: Any, right: Any) -> float:
    """两个缩写是否指同一支球队：1.0 = 等价，0.0 = 无关。"""
    left_abbr, right_abbr = canonical_abbr(left), canonical_abbr(right)
    if not left_abbr or not right_abbr:
        return 0.0
    if left_abbr == right_abbr:
        return 1.0
    if len(left_abbr) < 2 or len(right_abbr) < 2:
        return 0.0
    ratio = difflib.SequenceMatcher(None, left_abbr, right_abbr).ratio()
    return ratio if ratio >= ABBR_MIN_RATIO else 0.0


def parse_tennis_sets(raw: Any) -> Optional[list[tuple[int, int]]]:
    """解析网球比分：``"6-4, 0-0"`` → ``[(6, 4), (0, 0)]``。

    PM 流给的是**逐盘局分**（逗号分隔），`period` 给当前盘号（``S2``）。
    只接受「纯数字-纯数字」，抢七写法（``7-6(4)``）等一律判为无法识别 ——
    宁可不用加速，也不能把比分读错。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    sets: list[tuple[int, int]] = []
    for chunk in text.split(","):
        chunk = chunk.split("|")[0].strip().replace(" ", "")
        if not chunk:
            continue
        parts = chunk.split("-")
        if len(parts) != 2 or not all(re.fullmatch(r"\d{1,2}", p) for p in parts):
            return None
        sets.append((int(parts[0]), int(parts[1])))
    return sets or None


def tennis_progresses(previous: list, current: list) -> bool:
    """网球的逐盘比分只会前进：盘数不减少，同一盘里局数不减少。"""
    if len(current) < len(previous):
        return False
    for (prev_away, prev_home), (away, home) in zip(previous, current):
        if away < prev_away or home < prev_home:
            return False
    return True


def tennis_line_progresses(previous: list, current: list) -> bool:
    """一维局分列表（某一方的逐盘局分）同样只会前进。"""
    if len(current) < len(previous):
        return False
    return all(cur >= prev for prev, cur in zip(previous, current))


def _is_token_subset(left: str, right: str) -> bool:
    """一侧的词全是另一侧的子集（``texas a m`` ⊂ ``texas a m aggies``）。"""
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return False
    return left_tokens <= right_tokens or right_tokens <= left_tokens


def team_similarity(pm_name: Any, row_name: Any, row_abbr: Any = "") -> float:
    """PM 队名与轮询源队名（+缩写）的相似度，0 表示对不上。

    逐级放宽，顺序即优先级：

    1. 全名归一化后完全一致（1.0）
    2. PM 用一个缩写、轮询源给了缩写（``TXAM`` ↔ ``TA&M``）（0.9 ~ 0.99）
    3. 昵称一致且有共同词（``Cincinnati Bengals`` ↔ ``Bengals``）（0.8）
    4. 一侧的词是另一侧的子集（``Texas A&M`` ↔ ``Texas A&M Aggies``）（0.7）
    """
    pm_key, row_key = normalize_team(pm_name), normalize_team(row_name)
    if not pm_key or not row_key:
        return 0.0
    if pm_key == row_key:
        return 1.0
    if row_abbr and len(pm_key.split()) == 1:
        score = abbr_similarity(pm_name, row_abbr)
        if score:
            return 0.9 + 0.09 * score
    pm_nick = _nickname(pm_key)
    if pm_nick and pm_nick == _nickname(row_key) and set(pm_key.split()) & set(row_key.split()):
        return 0.8
    if _is_token_subset(pm_key, row_key):
        return 0.7
    return 0.0


def parse_pair_score(raw: Any) -> Optional[tuple[int, int]]:
    """从 PM 的 score 字段取出「两个数字」，失败返回 None。

    PM 的 score 形如 ``"3-2|Top 8th|..."``（电竞会是 ``"000-000|0-0|Bo3"``）。
    只接受「第一段恰好是两个数字」的形式，其它一律判为无法识别，
    这样格式变动时只会退化成不覆盖，不会写错比分。
    """
    head = str(raw or "").split("|")[0].strip()
    if not head or "-" not in head:
        return None
    parts = [p.strip() for p in head.split("-")]
    if len(parts) != 2:
        return None
    if not all(re.fullmatch(r"\d{1,3}", p) for p in parts):
        return None
    return int(parts[0]), int(parts[1])


@dataclass
class StreamEntry:
    """某一场比赛在 PM 实时流里的最新状态。"""

    sport: str
    away_name: str
    home_name: str
    away_key: str
    home_key: str
    raw_score: str = ""
    first_score: Optional[int] = None
    second_score: Optional[int] = None
    # 网球专用：逐盘局分 [(客, 主), ...]，first/second 存的是「当前盘」
    set_scores: Optional[list] = None
    orientation: Optional[str] = None  # away-home / home-away
    live: bool = False
    ended: bool = False
    period: str = ""
    message_count: int = 0
    received_at: float = 0.0
    validated_at: Optional[float] = None
    last_applied_at: Optional[float] = None
    applied_count: int = 0
    mismatch_count: int = 0
    regression_count: int = 0
    last_mismatch: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.away_key, self.home_key)

    @property
    def validated(self) -> bool:
        return self.validated_at is not None

    def scores(self) -> Optional[tuple[int, int]]:
        """按已判定的顺序返回 (客队比分, 主队比分)。"""
        if self.first_score is None or self.second_score is None:
            return None
        if self.orientation == "home-away":
            return self.second_score, self.first_score
        return self.first_score, self.second_score

    def label(self) -> str:
        if self.sport == "TENNIS" and self.set_scores:
            text = ", ".join(f"{away}-{home}" for away, home in self.set_scores)
            return f"{text}（{self.period}）" if self.period else text
        pair = self.scores()
        text = f"{pair[0]}-{pair[1]}" if pair else self.raw_score or "?"
        if self.orientation is None:
            return text
        return f"{text}（{'客-主' if self.orientation == 'away-home' else '主-客'}）"


@dataclass
class OverlayStats:
    tracked: int = 0
    validated: int = 0
    live: int = 0
    applied_last: int = 0
    applied_total: int = 0
    unmatched: int = 0
    last_apply_at: Optional[float] = None
    last_message_at: Optional[float] = None
    sample: str = ""
    note: str = ""


class SportsStreamOverlay:
    """把 Polymarket 实时比分叠加到 ESPN 比赛行上。"""

    def __init__(self, config: Config):
        self.config = config
        self.enabled: bool = bool(getattr(config, "pm_stream_overlay_enabled", True))
        sports = getattr(config, "pm_stream_override_sports", ("MLB", "NBA", "WNBA"))
        self.sports: set[str] = {str(s).upper() for s in (sports or ())}
        self.stale_seconds: float = float(
            getattr(config, "pm_stream_stale_seconds", 90.0) or 90.0
        )
        self.entries: dict[tuple[str, str, str], StreamEntry] = {}
        self._by_sport: dict[str, list[StreamEntry]] = {}
        self._stats: dict[str, OverlayStats] = {}
        self.last_unmatched: list[str] = []
        self.ingest_count = 0

    # ---------- 摄入 ----------

    def ingest(self, payload: dict) -> Optional[str]:
        """记录一条 PM 推送。返回运动名；不属于覆盖范围时返回 None。"""
        if not self.enabled:
            return None
        if not isinstance(payload, dict):
            return None
        league = str(payload.get("leagueAbbreviation") or "").lower()
        sport = PM_LEAGUES.get(league) or self._sport_from_event_state(payload)
        if sport is None or sport not in self.sports:
            return None
        away_name = str(payload.get("awayTeam") or "").strip()
        home_name = str(payload.get("homeTeam") or "").strip()
        if not away_name or not home_name:
            return None

        away_key, home_key = normalize_team(away_name), normalize_team(home_name)
        if not away_key or not home_key:
            return None
        key = (sport, away_key, home_key)
        now = time.time()
        entry = self.entries.get(key)
        if entry is None:
            entry = StreamEntry(
                sport=sport,
                away_name=away_name,
                home_name=home_name,
                away_key=away_key,
                home_key=home_key,
            )
            self.entries[key] = entry
            self._by_sport.setdefault(sport, []).append(entry)

        if sport == "TENNIS":
            entry.live = bool(payload.get("live"))
            entry.ended = bool(payload.get("ended"))
            entry.period = str(payload.get("period") or "")
            entry.received_at = now
            entry.message_count += 1
            self.ingest_count += 1
            sets = parse_tennis_sets(payload.get("score"))
            if sets is None:
                # 比分读不出来（例如改成抢七写法）：只留状态，不更新比分
                return sport
            if entry.set_scores and not tennis_progresses(entry.set_scores, sets):
                entry.regression_count += 1
                return sport
            if sets != entry.set_scores:
                entry.set_scores = sets
                entry.raw_score = str(payload.get("score") or "")
                entry.first_score, entry.second_score = sets[-1]
            return sport

        pair = parse_pair_score(payload.get("score"))
        if pair is not None:
            prev = (entry.first_score, entry.second_score)
            if prev[0] is not None and (pair[0] < prev[0] or pair[1] < prev[1]):
                # 同一场比赛比分不应回退，判定为脏数据，保留上一条
                entry.regression_count += 1
                entry.received_at = now
                return sport
            if pair != prev:
                entry.first_score, entry.second_score = pair
                entry.raw_score = str(payload.get("score") or "")

        entry.live = bool(payload.get("live"))
        entry.ended = bool(payload.get("ended"))
        entry.period = str(payload.get("period") or "")
        entry.received_at = now
        entry.message_count += 1
        self.ingest_count += 1
        return sport

    # ---------- 匹配 ----------

    @staticmethod
    def _sport_from_event_state(payload: dict) -> Optional[str]:
        """按 ``eventState.type`` 兜底识别运动（网球没有 "tennis" 标签）。"""
        state = payload.get("eventState")
        if not isinstance(state, dict):
            return None
        kind = str(state.get("type") or "").strip().lower()
        return EVENT_STATE_SPORTS.get(kind)

    def _entry_for_names(
        self,
        sport: str,
        away_name: Any,
        home_name: Any,
        away_abbr: Any = "",
        home_abbr: Any = "",
    ) -> Optional[StreamEntry]:
        """按「运动 + 客队 + 主队」找对应的流记录。

        先走精确键（快路径），再逐级放宽队名写法。放宽后的匹配**必须唯一**，
        否则宁可返回 None（不覆盖），也不会去猜一场比赛。
        """
        away_key, home_key = normalize_team(away_name), normalize_team(home_name)
        if not away_key or not home_key:
            return None
        entry = self.entries.get((sport, away_key, home_key))
        if entry is not None:
            return entry
        candidates = self._by_sport.get(sport, ())
        if not candidates:
            return None
        scored = []
        for cand in candidates:
            score = min(
                team_similarity(cand.away_name, away_name, away_abbr),
                team_similarity(cand.home_name, home_name, home_abbr),
            )
            if score >= PAIR_MIN_SCORE:
                scored.append((score, cand))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] <= scored[1][0]:
            # 两场比赛的相似度一样高，无法区分
            return None
        return scored[0][1]

    @staticmethod
    def _row_names(sport: str, row: Any) -> tuple[Any, Any]:
        if isinstance(row, dict):
            return (
                row.get("away_team_name") or row.get("away_team"),
                row.get("home_team_name") or row.get("home_team"),
            )
        return (
            getattr(row, "away_team_name", None) or getattr(row, "away_team", None),
            getattr(row, "home_team_name", None) or getattr(row, "home_team", None),
        )

    @staticmethod
    def _row_teams(sport: str, row: Any) -> tuple[tuple[Any, Any], tuple[Any, Any]]:
        """返回 ((客队名, 客队缩写), (主队名, 主队缩写))。

        缩写只有橄榄球 / 冰球这类行才有：ESPN 记分板的 ``abbreviation``
        存在 ``*_team`` 字段里（NBA / MLB 的行同样是缩写，不影响匹配）。
        """
        if isinstance(row, dict):
            return (
                (row.get("away_team_name") or row.get("away_team"), row.get("away_team") or ""),
                (row.get("home_team_name") or row.get("home_team"), row.get("home_team") or ""),
            )
        return (
            (
                getattr(row, "away_team_name", None) or getattr(row, "away_team", None),
                getattr(row, "away_team", "") or "",
            ),
            (
                getattr(row, "home_team_name", None) or getattr(row, "home_team", None),
                getattr(row, "home_team", "") or "",
            ),
        )

    @staticmethod
    def _row_scores(sport: str, row: Any) -> tuple[int, int]:
        if isinstance(row, dict):
            return int(row.get("away_score") or 0), int(row.get("home_score") or 0)
        return int(getattr(row, "away_score", 0) or 0), int(getattr(row, "home_score", 0) or 0)

    def match_row(self, sport: str, row: Any) -> Optional[StreamEntry]:
        (away_name, away_abbr), (home_name, home_abbr) = self._row_teams(sport, row)
        if sport in ABBR_FREE_SPORTS:
            # 网球行的 home_team / away_team 是球员姓名，不是缩写
            away_abbr = home_abbr = ""
        return self._entry_for_names(
            sport, away_name, home_name, away_abbr, home_abbr
        )

    def entry_for_payload(self, sport: str, payload: dict) -> Optional[StreamEntry]:
        return self._entry_for_names(
            sport, payload.get("awayTeam"), payload.get("homeTeam")
        )

    # ---------- 覆盖 ----------

    def _try_validate(self, entry: StreamEntry, away_score: int, home_score: int) -> bool:
        """用 ESPN 的比分给 PM 数据「对齐口径」，成功即认为可以信任。"""
        pair = (entry.first_score, entry.second_score)
        if pair[0] is None or pair[1] is None:
            return False
        direct = pair == (away_score, home_score)
        swapped = pair == (home_score, away_score)
        if direct and swapped:
            # 两队平分，无法判定顺序，等下一次
            return False
        if not direct and not swapped:
            entry.mismatch_count += 1
            entry.last_mismatch = (
                f"PM {pair[0]}-{pair[1]} vs ESPN {away_score}-{home_score}"
            )
            return False
        entry.orientation = "away-home" if direct else "home-away"
        entry.validated_at = time.time()
        logger.info(
            "PM 流校验通过：%s %s vs %s（顺序 %s）",
            entry.sport, entry.away_name, entry.home_name, entry.orientation,
        )
        return True

    # ---------- 网球专用覆盖 ----------

    @staticmethod
    def _tennis_linescores(row: Any) -> tuple[list, list]:
        p1 = (row.get("player1") or {}).get("linescores") or []
        p2 = (row.get("player2") or {}).get("linescores") or []
        return [int(x) for x in p1], [int(x) for x in p2]

    def _try_validate_tennis(self, entry: StreamEntry, row: Any) -> bool:
        """用 ESPN 的逐盘局分给 PM 数据对齐口径。"""
        pm_sets = entry.set_scores
        if not pm_sets or not isinstance(row, dict):
            return False
        ls1, ls2 = self._tennis_linescores(row)
        if len(ls1) != len(pm_sets) or len(ls2) != len(pm_sets):
            return False
        direct = all((away, home) == (a, b) for (away, home), a, b in zip(pm_sets, ls1, ls2))
        swapped = all((away, home) == (b, a) for (away, home), a, b in zip(pm_sets, ls1, ls2))
        if direct and swapped:
            return False
        if not direct and not swapped:
            entry.mismatch_count += 1
            entry.last_mismatch = (
                f"PM {entry.label()} vs ESPN {ls1} / {ls2}"
            )
            return False
        entry.orientation = "away-home" if direct else "home-away"
        entry.validated_at = time.time()
        logger.info(
            "PM 流校验通过：TENNIS %s vs %s（顺序 %s，盘分 %s）",
            entry.away_name, entry.home_name, entry.orientation, entry.label(),
        )
        return True

    def _apply_to_tennis_row(self, row: Any) -> tuple[Any, bool]:
        """把 PM 的逐盘局分写回网球行（这是唯一会被覆盖的字段）。"""
        entry = self.match_row("TENNIS", row)
        if entry is None or not isinstance(row, dict):
            return row, False
        if not entry.validated:
            self._try_validate_tennis(entry, row)
            return row, False
        now = time.time()
        if entry.ended or not entry.live:
            return row, False
        if now - entry.received_at > self.stale_seconds:
            return row, False
        pm_sets = entry.set_scores
        if not pm_sets:
            return row, False
        ls1, ls2 = self._tennis_linescores(row)
        # 盘数必须与轮询源一致：ESPN 多出一盘（或还没补上）时不动，等下一轮
        if len(ls1) != len(pm_sets) or len(ls2) != len(pm_sets):
            return row, False
        if entry.orientation == "away-home":
            new1 = [away for away, _ in pm_sets]
            new2 = [home for _, home in pm_sets]
        else:
            new1 = [home for _, home in pm_sets]
            new2 = [away for away, _ in pm_sets]
        if new1 == ls1 and new2 == ls2:
            entry.last_applied_at = now
            return row, False
        if not tennis_line_progresses(ls1, new1) or not tennis_line_progresses(ls2, new2):
            # PM 读数比轮询源还旧：宁可不用
            return row, False

        merged = dict(row)
        merged["player1"] = {**(row.get("player1") or {}), "linescores": new1}
        merged["player2"] = {**(row.get("player2") or {}), "linescores": new2}
        # 盘分沿用轮询源的算法（未分出胜负的盘不计入），口径保持不变
        merged["away_score"] = sum(1 for a, b in zip(new1, new2) if a > b)
        merged["home_score"] = sum(1 for a, b in zip(new1, new2) if b > a)
        merged["updated_at"] = now
        merged["source"] = "pm_stream"
        merged["pm_score"] = entry.label()
        entry.last_applied_at = now
        entry.applied_count += 1
        return merged, True

    def apply_to_row(self, sport: str, row: Any) -> tuple[Any, bool]:
        """把 PM 最新比分叠加到一行 ESPN 数据上。返回 (行, 是否已覆盖)。"""
        if not self.enabled or sport not in self.sports:
            return row, False
        if sport == "TENNIS":
            return self._apply_to_tennis_row(row)
        entry = self.match_row(sport, row)
        if entry is None:
            return row, False
        row_away, row_home = self._row_scores(sport, row)
        if not entry.validated:
            self._try_validate(entry, row_away, row_home)
            return row, False
        now = time.time()
        if entry.ended or not entry.live:
            return row, False
        if now - entry.received_at > self.stale_seconds:
            return row, False
        pair = entry.scores()
        if pair is None:
            return row, False
        away_score, home_score = pair
        if away_score < row_away or home_score < row_home:
            # PM 读数比 ESPN 还旧：宁可不用
            return row, False
        if away_score == row_away and home_score == row_home:
            entry.last_applied_at = now
            return row, False

        if isinstance(row, dict):
            merged = dict(row)
            merged["away_score"] = away_score
            merged["home_score"] = home_score
            merged["updated_at"] = now
            merged["source"] = "pm_stream"
            merged["pm_score"] = entry.label()
        else:
            from dataclasses import replace

            merged = replace(
                row,
                away_score=away_score,
                home_score=home_score,
                updated_at=now,
            )
        entry.last_applied_at = now
        entry.applied_count += 1
        return merged, True

    def merge_rows(self, sport: str, rows: Optional[list]) -> tuple[list, int]:
        applied = 0
        merged_rows = []
        for row in rows or []:
            merged, hit = self.apply_to_row(sport, row)
            merged_rows.append(merged)
            applied += 1 if hit else 0
        return merged_rows, applied

    # ---------- 上报 ----------

    def stats(self) -> dict[str, OverlayStats]:
        result: dict[str, OverlayStats] = {}
        now = time.time()
        for sport in sorted(self.sports):
            entries = self._by_sport.get(sport, [])
            stat = OverlayStats(
                tracked=len(entries),
                validated=sum(1 for e in entries if e.validated),
                live=sum(1 for e in entries if e.live and not e.ended),
            )
            recent = [e for e in entries if e.received_at and now - e.received_at <= self.stale_seconds]
            if recent:
                newest = max(recent, key=lambda e: e.received_at)
                stat.last_message_at = newest.received_at
                stat.sample = f"{newest.away_name} vs {newest.home_name} {newest.label()}"
            applied = [e for e in entries if e.last_applied_at]
            if applied:
                latest = max(applied, key=lambda e: e.last_applied_at or 0)
                stat.last_apply_at = latest.last_applied_at
                stat.applied_total = sum(e.applied_count for e in entries)
                stat.applied_last = len(
                    [e for e in applied if now - (e.last_applied_at or 0) <= self.stale_seconds]
                )
            pending = [e for e in recent if not e.validated]
            if pending:
                worst = pending[0]
                stat.note = worst.last_mismatch or "等待与 ESPN 比分一致后启用"
            elif entries and not recent:
                stat.note = "暂无该运动的实时推送"
            result[sport] = stat
        return result

    def health_suffix(self, sport: str) -> str:
        """数据来源后缀，例如「+ PM 实时流（已覆盖 2 场）」。"""
        stat = self.stats().get(sport.upper())
        if stat is None or not self.enabled:
            return ""
        if stat.applied_last:
            return f" + PM 实时流覆盖 {stat.applied_last} 场"
        if stat.validated:
            return " + PM 实时流已校验"
        if stat.tracked:
            return " + PM 实时流待校验"
        return ""
