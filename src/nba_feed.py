# -*- coding: utf-8 -*-
"""ESPN NBA live score feed - polls public scoreboard API."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class NBAGameState:
    game_id: str
    home_team: str
    away_team: str
    home_team_name: str
    away_team_name: str
    home_score: int
    away_score: int
    quarter: int
    clock_seconds: float
    status: str  # "pre", "in", "post"
    updated_at: float


@dataclass
class SoccerGameState:
    """足球比赛状态。

    与 NBAGameState 的关键差异：
      * `minute` 是**已进行分钟**（含补时，例如 90'+6' → 96），不是倒计时；
      * 自带所属联赛的期望进球 λ（足球的进球率联赛差异大：意甲 2.58 vs 德甲 3.18 球/场）；
      * `league_tag` 是 Polymarket 的盘口 tag（epl / laliga / …），单场有
        主胜/平局/客胜三个独立二元盘。
    """

    game_id: str
    league: str
    league_tag: str
    league_name: str
    lambda_home: float
    lambda_away: float
    home_team: str
    away_team: str
    home_team_name: str
    away_team_name: str
    home_score: int
    away_score: int
    period: int          # 1 = 上半场，2 = 下半场
    minute: float        # 已进行分钟（含补时）
    status: str          # "pre" / "in" / "post"
    updated_at: float


def parse_nba_clock(clock: object) -> float:
    """把 NBA liveData 的 `gameClock`（ISO8601 时长，"PT04M32.00S"）解析成剩余秒数。

    2026-10-29 官方源接入时新增：ESPN 给的是 "4:32"，NBA 官方给的是 "PT04M32.00S"，
    两种写法都要能吃到同一套信号规则里。空字符串（节间/未开始）返回 0。
    """
    text = str(clock or "").strip().upper()
    if not text:
        return 0.0
    match = re.match(r"^PT(?:(\d+)M)?(?:([\d.]+)S)?$", text)
    if not match:
        # 兜底：万一哪天又变回 "4:32"
        legacy = re.match(r"^(\d+):(\d+)", text)
        if legacy:
            return float(legacy.group(1)) * 60 + float(legacy.group(2))
        return 0.0
    return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)


def parse_soccer_clock(clock_str: str) -> float:
    """把 ESPN 足球的 displayClock 解析成「已进行分钟」。

    ESPN 的写法实测有三种：`67'`、`90'+6'`、以及少数 `67:30`。
    注意这是**已进行时间**（足球不像篮球那样倒计时），所以不做任何减法。
    """
    text = str(clock_str or "").strip()
    if not text:
        return 0.0
    m = re.match(r"^(\d+)'\s*\+\s*(\d+)", text)
    if m:
        return float(m.group(1)) + float(m.group(2))
    m = re.match(r"^(\d+)'", text)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+):(\d+)", text)
    if m:
        return float(m.group(1)) + float(m.group(2)) / 60.0
    m = re.match(r"^(\d+)$", text)
    if m:
        return float(m.group(1))
    return 0.0


def parse_clock(clock_str: str) -> float:
    """Convert "PT12M34S" or "12:34" to seconds remaining."""
    if not clock_str:
        return 0.0
    clock_str = clock_str.replace("PT", "").replace("S", "")
    if "M" in clock_str:
        parts = clock_str.split("M")
        return float(parts[0]) * 60 + float(parts[1]) if parts[1] else float(parts[0]) * 60
    if ":" in clock_str:
        parts = clock_str.split(":")
        return float(parts[0]) * 60 + float(parts[1])
    return float(clock_str)


class NBAFeed:
    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._health: dict[str, dict] = {
            sport: {
                "sport": sport,
                "source": "",
                "last_success_at": None,
                "last_error_at": None,
                "last_error": "",
                "consecutive_failures": 0,
                "last_event_count": None,
                "last_latency_ms": None,
            }
            for sport in ("NBA", "MLB", "WNBA", "TENNIS")
        }

    def _health_entry(self, sport: str) -> dict:
        entry = self._health.get(sport)
        if entry is None:
            entry = {
                "sport": sport,
                "source": self._source_for(sport),
                "last_success_at": None,
                "last_error_at": None,
                "last_error": "",
                "consecutive_failures": 0,
                "last_event_count": None,
                "last_latency_ms": None,
            }
            self._health[sport] = entry
        if not entry.get("source"):
            entry["source"] = self._source_for(sport)
        return entry

    def _source_for(self, sport: str) -> str:
        """该运动当前实际使用的数据来源。"""
        if sport == "MLB":
            source = str(getattr(self.config, "mlb_feed_source", "espn") or "espn").lower()
            return "statsapi.mlb.com（官方）" if source == "statsapi" else "ESPN Scoreboard"
        if sport == "SOCCER":
            leagues = len(getattr(self.config, "soccer_leagues", []) or [])
            return f"ESPN Scoreboard（{leagues} 个联赛）"
        if sport in ("NBA", "WNBA", "TENNIS", "NFL", "CFB", "NHL", "NCAAB", "NCAAW"):
            return "ESPN Scoreboard"
        return "-"

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=getattr(self.config, "espn_timeout_seconds", 20))

    @staticmethod
    def _describe(error: Exception) -> str:
        """超时类异常的 str() 是空字符串，至少把类型名写出来。"""
        name = type(error).__name__
        text = str(error).strip()
        return f"{name}: {text}" if text else name

    def _record_success(self, sport: str, event_count: int, latency_ms: float) -> None:
        entry = self._health_entry(sport)
        entry["last_success_at"] = time.time()
        entry["last_event_count"] = event_count
        entry["last_latency_ms"] = round(latency_ms, 1)
        entry["consecutive_failures"] = 0
        entry["last_error"] = ""

    def _record_failure(self, sport: str, error: Exception) -> None:
        entry = self._health_entry(sport)
        entry["last_error_at"] = time.time()
        entry["last_error"] = self._describe(error)
        entry["consecutive_failures"] = int(entry["consecutive_failures"]) + 1

    def health(self) -> dict:
        return {sport: dict(entry) for sport, entry in self._health.items()}

    async def start(self):
        self._session = aiohttp.ClientSession(proxy=self.config.proxy_url)

    async def stop(self):
        if self._session:
            await self._session.close()

    async def fetch_games(self) -> list[NBAGameState]:
        """NBA 比分：默认走官方 liveData（比 ESPN 快 5~10 秒），失败回退 ESPN。"""
        source = str(getattr(self.config, "nba_feed_source", "espn") or "espn").lower()
        if source == "official":
            games = await self._fetch_nba_official()
            if games:
                return games
            logger.warning("NBA 官方 liveData 没取到数据，回退 ESPN")
        return await self._fetch_basketball(self.config.espn_scoreboard_url, "NBA")

    async def fetch_wnba_games(self) -> list[NBAGameState]:
        url = getattr(self.config, "espn_wnba_url", "")
        if not url:
            return []
        return await self._fetch_basketball(url, "WNBA")

    async def fetch_nfl_games(self) -> list[NBAGameState]:
        url = getattr(self.config, "espn_nfl_url", "")
        if not url:
            return []
        return await self._fetch_period_scores(url, "NFL")

    async def fetch_cfb_games(self) -> list[NBAGameState]:
        url = getattr(self.config, "espn_cfb_url", "")
        if not url:
            return []
        return await self._fetch_period_scores(url, "CFB")

    async def fetch_nhl_games(self) -> list[NBAGameState]:
        """NHL 比分：默认走官方 api-web.nhle.com，失败回退 ESPN。"""
        source = str(getattr(self.config, "nhl_feed_source", "espn") or "espn").lower()
        if source == "official":
            games = await self._fetch_nhl_official()
            if games:
                return games
            logger.warning("NHL 官方接口没取到数据，回退 ESPN")
        url = getattr(self.config, "espn_nhl_url", "")
        if not url:
            return []
        return await self._fetch_period_scores(url, "NHL")

    # ---------- 联盟官方接口（2026-10-29）----------

    async def _fetch_nba_official(self) -> list[NBAGameState]:
        """cdn.nba.com 的 liveData 记分板。

        比 ESPN 快 5~10 秒；必须带 `Referer`/`Origin`，否则 CDN 直接 403。
        `homeTeam.teamName` 就是 "Hawks"/"Lakers" 这种昵称，正好和 Polymarket 对齐。
        """
        if not self._session:
            return []
        url = getattr(self.config, "nba_live_data_url", "")
        if not url:
            return []
        started = time.perf_counter()
        try:
            async with self._session.get(
                url,
                # cdn.nba.com 对非浏览器 UA 直接 403（aiohttp 默认 UA 会被挡），
                # 所以这三项缺一不可：UA + Referer + Origin。
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                    "Referer": "https://www.nba.com/",
                    "Origin": "https://www.nba.com",
                },
                timeout=self._timeout(),
            ) as resp:
                if resp.status != 200:
                    logger.warning("NBA 官方 liveData 返回 %d", resp.status)
                    self._record_failure("NBA", RuntimeError(f"HTTP {resp.status}"))
                    return []
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("NBA 官方 liveData 失败：%s", self._describe(exc))
            self._record_failure("NBA", exc)
            return []

        games: list[NBAGameState] = []
        for g in ((data.get("scoreboard") or {}).get("games") or []):
            status_id = int(g.get("gameStatus") or 0)
            status = "pre" if status_id == 1 else ("in" if status_id == 2 else "post")
            home = g.get("homeTeam") or {}
            away = g.get("awayTeam") or {}
            home_name = str(home.get("teamName") or "")
            away_name = str(away.get("teamName") or "")
            games.append(NBAGameState(
                game_id=str(g.get("gameId") or ""),
                home_team=home_name,
                away_team=away_name,
                home_team_name=home_name,
                away_team_name=away_name,
                home_score=int(home.get("score") or 0),
                away_score=int(away.get("score") or 0),
                quarter=int(g.get("period") or 0),
                clock_seconds=parse_nba_clock(g.get("gameClock")),
                status=status,
                updated_at=time.time(),
            ))
        self._record_success("NBA", len(games), (time.perf_counter() - started) * 1000)
        self._health_entry("NBA")["source"] = "cdn.nba.com liveData（官方）"
        return games

    async def _fetch_nhl_official(self) -> list[NBAGameState]:
        """api-web.nhle.com 的记分板（`gamesByDate` → `games`）。

        `gameState`：FUT=未开始 / LIVE·CRIT=进行中 / OFF·FINAL=结束。
        `homeTeam.commonName.default` 是 "Maple Leafs" 这种昵称，和 Polymarket 对齐。
        """
        if not self._session:
            return []
        url = getattr(self.config, "nhl_scoreboard_url", "")
        if not url:
            return []
        started = time.perf_counter()
        try:
            async with self._session.get(url, timeout=self._timeout()) as resp:
                if resp.status != 200:
                    logger.warning("NHL 官方记分板返回 %d", resp.status)
                    self._record_failure("NHL", RuntimeError(f"HTTP {resp.status}"))
                    return []
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("NHL 官方记分板失败：%s", self._describe(exc))
            self._record_failure("NHL", exc)
            return []

        def team_name(team: dict) -> str:
            return str(((team.get("commonName") or {}).get("default"))
                       or ((team.get("name") or {}).get("default")) or "")

        games: list[NBAGameState] = []
        for block in data.get("gamesByDate") or []:
            for g in block.get("games") or []:
                state = str(g.get("gameState") or "").upper()
                if state == "FUT":
                    status = "pre"
                elif state in ("LIVE", "CRIT"):
                    status = "in"
                else:
                    status = "post"
                home = g.get("homeTeam") or {}
                away = g.get("awayTeam") or {}
                period = int((g.get("periodDescriptor") or {}).get("number") or 0)
                seconds = int((g.get("clock") or {}).get("secondsRemaining") or 0)
                games.append(NBAGameState(
                    game_id=str(g.get("id") or ""),
                    home_team=team_name(home),
                    away_team=team_name(away),
                    home_team_name=team_name(home),
                    away_team_name=team_name(away),
                    home_score=int(home.get("score") or 0),
                    away_score=int(away.get("score") or 0),
                    quarter=period,
                    clock_seconds=float(seconds),
                    status=status,
                    updated_at=time.time(),
                ))
        self._record_success("NHL", len(games), (time.perf_counter() - started) * 1000)
        self._health_entry("NHL")["source"] = "api-web.nhle.com（官方）"
        return games

    async def fetch_ncaab_games(self) -> list[NBAGameState]:
        """大学男篮（2 个半场）。记分板结构与 NBA 一致，只有节次语义不同。"""
        url = getattr(self.config, "espn_ncaab_url", "")
        if not url:
            return []
        return await self._fetch_basketball(url, "NCAAB")

    async def fetch_ncaaw_games(self) -> list[NBAGameState]:
        """大学女篮（4 节，和 WNBA/NBA 同构）。"""
        url = getattr(self.config, "espn_ncaaw_url", "")
        if not url:
            return []
        return await self._fetch_basketball(url, "NCAAW")

    async def fetch_soccer_games(self) -> list["SoccerGameState"]:
        """足球：按配置的联赛列表逐个拉 ESPN 记分板。

        与其它运动的两个关键差异：
          1. 时钟是「已进行分钟」（`displayClock` = `67'` / `90'+6'`），不是倒计时；
          2. 单场有三个独立结果（主胜/平局/客胜），平整场也有真实概率。

        健康状态**按足球一行汇总**（面板一行一个运动）：原来按联赛记，
        会冒出 EPL/LALIGA/SERIEA… 8 行，用户根本找不到"足球"，
        而且失败路径写的 SOCCER 行永远没有成功记录、一直显示异常。
        联赛明细放进 note，鼠标悬停可见。
        """
        leagues = getattr(self.config, "soccer_leagues", []) or []
        if not getattr(self.config, "soccer_enabled", True) or not leagues:
            return []
        started = time.perf_counter()
        games: list[SoccerGameState] = []
        by_league: dict[str, int] = {}
        errors: list[str] = []
        queried = 0
        for league in leagues:
            espn = str(league.get("espn") or "")
            if not espn:
                continue
            queried += 1
            name = str(league.get("name") or league.get("key") or espn)
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn}/scoreboard"
            got, error = await self._fetch_soccer_league(url, league)
            games.extend(got)
            by_league[name] = len(got)
            if error:
                errors.append(f"{name}: {error}")
        if queried and len(errors) == queried:
            # 全部联赛都失败才算这次抓取失败
            self._record_failure("SOCCER", RuntimeError("; ".join(errors[:3])))
        else:
            self._record_success("SOCCER", len(games), (time.perf_counter() - started) * 1000)
            entry = self._health_entry("SOCCER")
            played = " ".join(f"{k}{v}场" for k, v in by_league.items() if v)
            entry["note"] = played or "今日无赛事"
            if errors:
                entry["last_error"] = "; ".join(errors[:3])
        return games

    async def _fetch_soccer_league(self, url: str, league: dict):
        """返回 (games, error)。健康状态由调用方汇总，这里不自己记。"""
        if not self._session:
            return [], "session 未启动"
        label = str(league.get("key") or league.get("name") or "SOCCER")
        try:
            async with self._session.get(url, timeout=self._timeout()) as resp:
                if resp.status != 200:
                    logger.warning("ESPN %s scoreboard returned status %d", label, resp.status)
                    return [], f"HTTP {resp.status}"
                data = await resp.json()
        except Exception as exc:
            logger.warning("ESPN %s scoreboard fetch failed: %s", label, self._describe(exc))
            return [], self._describe(exc)

        out: list[SoccerGameState] = []
        for event in data.get("events", []):
            competition = (event.get("competitions") or [{}])[0]
            status_info = competition.get("status", {})
            status_type = (status_info.get("type") or {}).get("state", "")
            home = away = None
            for team in competition.get("competitors", []):
                if team.get("homeAway") == "home":
                    home = team
                else:
                    away = team
            if not home or not away:
                continue
            home_team = home.get("team", {}) or {}
            away_team = away.get("team", {}) or {}
            minute = parse_soccer_clock(status_info.get("displayClock", ""))
            period = int(status_info.get("period", 0) or 0)
            # 上半场补时的 "45'+2'" 会被解析成 47，这里不再额外折算，
            # 模型按"已进行分钟"直接算剩余进球比例即可。
            out.append(SoccerGameState(
                game_id=str(event.get("id", "")),
                league=str(league.get("key") or ""),
                league_tag=str(league.get("tag") or ""),
                league_name=str(league.get("name") or ""),
                lambda_home=float(league.get("lambda_home") or 1.573),
                lambda_away=float(league.get("lambda_away") or 1.293),
                home_team=home_team.get("abbreviation") or home_team.get("shortDisplayName") or "?",
                away_team=away_team.get("abbreviation") or away_team.get("shortDisplayName") or "?",
                # 足球盘口用完整队名（"Seattle Sounders FC"），不做去吉祥物处理
                home_team_name=str(home_team.get("displayName") or home_team.get("name") or "?"),
                away_team_name=str(away_team.get("displayName") or away_team.get("name") or "?"),
                home_score=int(float(home.get("score", 0) or 0)),
                away_score=int(float(away.get("score", 0) or 0)),
                period=period,
                minute=minute,
                status=status_type,
                updated_at=time.time(),
            ))
        return out, None

    @staticmethod
    def market_search_name(sport: str, team: dict) -> str:
        """给 Polymarket 搜索用的队名。

        NFL 的盘口用昵称（`Bears vs. Panthers`），正好是 ESPN 的 `team.name`；
        CFB 的盘口用学校名（`Arizona State vs. Texas A&M`），而 ESPN 给的是
        `displayName="Arizona State Sun Devils"` + `name="Sun Devils"`，
        去掉吉祥物就是学校名。
        """
        team = team or {}
        display = str(team.get("displayName") or "").strip()
        short = str(team.get("shortDisplayName") or "").strip()
        mascot = str(team.get("name") or "").strip()
        sport_upper = str(sport).upper()
        # NFL / NHL 的盘口都用昵称（Bears vs. Panthers / Maple Leafs vs. Canadiens）
        if sport_upper in ("NFL", "NHL"):
            return mascot or short or display or "?"
        # 大学赛事（CFB / NCAAB / NCAAW）盘口用学校名：
        # ESPN 的 displayName="Purdue Boilermakers" + name="Boilermakers" → "Purdue"，
        # 拿不到就退回 shortDisplayName（"Long Island University Sharks" → "Long Island"）。
        if sport_upper in ("CFB", "NCAAB", "NCAAW"):
            if display and mascot and display.endswith(mascot):
                school = display[: -len(mascot)].strip()
                if school:
                    return school
            return short or display or "?"
        if display and mascot and display.endswith(mascot):
            school = display[: -len(mascot)].strip()
            if school:
                return school
        return short or display or "?"

    async def _fetch_period_scores(self, url: str, sport: str) -> list[NBAGameState]:
        """按「节次 + 剩余时间」记分的运动（美式橄榄球、冰球）：结构与篮球一致。"""
        if not self._session:
            return []
        started = time.perf_counter()
        try:
            async with self._session.get(url, timeout=self._timeout()) as resp:
                if resp.status != 200:
                    logger.warning("ESPN %s scoreboard returned status %d", sport, resp.status)
                    self._record_failure(sport, RuntimeError(f"HTTP {resp.status}"))
                    return []
                data = await resp.json()
        except Exception as exc:
            logger.warning("ESPN %s scoreboard fetch failed: %s", sport, self._describe(exc))
            self._record_failure(sport, exc)
            return []

        games = []
        for event in data.get("events", []):
            competition = event.get("competitions", [{}])[0]
            status_info = competition.get("status", {})
            status_type = status_info.get("type", {}).get("state", "")
            home = away = None
            for team in competition.get("competitors", []):
                if team.get("homeAway") == "home":
                    home = team
                else:
                    away = team
            if not home or not away:
                continue
            home_team = home.get("team", {}) or {}
            away_team = away.get("team", {}) or {}
            games.append(NBAGameState(
                game_id=event.get("id", ""),
                home_team=home_team.get("abbreviation") or "?",
                away_team=away_team.get("abbreviation") or "?",
                # 这两列用于 Polymarket 市场搜索：NFL/NHL 用昵称、CFB 用学校名
                home_team_name=self.market_search_name(sport, home_team),
                away_team_name=self.market_search_name(sport, away_team),
                home_score=int(home.get("score", 0) or 0),
                away_score=int(away.get("score", 0) or 0),
                quarter=int(status_info.get("period", 0) or 0),
                clock_seconds=parse_clock(status_info.get("displayClock", "")),
                status=status_type,
                updated_at=time.time(),
            ))
        self._record_success(sport, len(games), (time.perf_counter() - started) * 1000)
        return games

    async def _fetch_basketball(self, url: str, sport: str) -> list[NBAGameState]:
        if not self._session:
            return []
        started = time.perf_counter()
        try:
            async with self._session.get(
                url, timeout=self._timeout()
            ) as resp:
                if resp.status != 200:
                    logger.warning("ESPN %s scoreboard returned status %d", sport, resp.status)
                    self._record_failure(sport, RuntimeError(f"HTTP {resp.status}"))
                    return []
                data = await resp.json()
        except Exception as exc:
            logger.warning("ESPN %s scoreboard fetch failed: %s", sport, self._describe(exc))
            self._record_failure(sport, exc)
            return []

        games = []
        for event in data.get("events", []):
            competition = event.get("competitions", [{}])[0]
            status_info = competition.get("status", {})
            status_type = status_info.get("type", {}).get("state", "")

            home = None
            away = None
            for team in competition.get("competitors", []):
                if team.get("homeAway") == "home":
                    home = team
                else:
                    away = team
            if not home or not away:
                continue

            quarter = status_info.get("period", 0)
            clock_raw = status_info.get("displayClock", "")
            clock_seconds = parse_clock(clock_raw)

            games.append(NBAGameState(
                game_id=event.get("id", ""),
                home_team=home.get("team", {}).get("abbreviation", "?"),
                away_team=away.get("team", {}).get("abbreviation", "?"),
                home_team_name=home.get("team", {}).get("displayName", home.get("team", {}).get("abbreviation", "?")),
                away_team_name=away.get("team", {}).get("displayName", away.get("team", {}).get("abbreviation", "?")),
                home_score=int(home.get("score", 0)),
                away_score=int(away.get("score", 0)),
                quarter=quarter,
                clock_seconds=clock_seconds,
                status=status_type,
                updated_at=time.time(),
            ))
        self._record_success(sport, len(games), (time.perf_counter() - started) * 1000)
        return games

    async def fetch_mlb_games(self) -> list[dict]:
        """MLB 比分：默认走官方 StatsAPI，失败时回退 ESPN。"""
        source = str(getattr(self.config, "mlb_feed_source", "espn") or "espn").lower()
        if source == "statsapi":
            games = await self._fetch_mlb_statsapi()
            if games:
                return games
            logger.warning("MLB 官方接口没有取到数据，回退 ESPN")
        return await self._fetch_mlb_espn()

    async def fetch_mlb_espn_games(self, dates: list = None) -> list[dict]:
        """按指定 ET 日期取 ESPN 的 MLB 记分板（用于给历史 id 的比赛补结算）。

        StatsAPI 与 ESPN 的 game_id 命名空间不同：一旦某场比赛是用 ESPN 回退源
        开仓的（id 形如 401816900），切回 StatsAPI 后就永远匹配不上、无法结算。
        这个入口专门用来兜住这类遗留仓位。
        """
        if not dates:
            return await self._fetch_mlb_espn()
        merged: dict = {}
        for date in dates:
            for game in await self._fetch_mlb_espn(dates=[date]):
                merged.setdefault(game.get("game_id"), game)
        return list(merged.values())

    @staticmethod
    def _mlb_dates() -> list[str]:
        """官方接口按东海岸日期排期，取今天和昨天以覆盖跨夜的进行中比赛。"""
        try:
            from zoneinfo import ZoneInfo

            et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            et = datetime.utcnow()
        return [et.strftime("%Y-%m-%d"), (et - timedelta(days=1)).strftime("%Y-%m-%d")]

    async def _fetch_mlb_statsapi(self) -> list[dict]:
        if not self._session:
            return []
        url = getattr(self.config, "mlb_statsapi_schedule_url", "")
        if not url:
            return []
        started = time.perf_counter()
        games: list[dict] = []
        seen: set[str] = set()
        errors: list[str] = []
        for date_str in self._mlb_dates():
            params = {"sportId": 1, "date": date_str, "hydrate": "linescore,team"}
            try:
                async with self._session.get(
                    url, params=params, timeout=self._timeout()
                ) as resp:
                    if resp.status != 200:
                        errors.append(f"HTTP {resp.status}")
                        continue
                    data = await resp.json()
            except Exception as exc:  # noqa: BLE001
                errors.append(self._describe(exc))
                continue
            for game in (data.get("dates") or [{}])[0].get("games", []):
                row = self._map_mlb_statsapi_game(game)
                if row and row["game_id"] and row["game_id"] not in seen:
                    seen.add(row["game_id"])
                    games.append(row)
        if errors and not games:
            logger.warning("MLB 官方接口抓取失败：%s", "; ".join(errors[:3]))
            self._record_failure("MLB", RuntimeError("; ".join(errors[:3])))
            return []
        self._health_entry("MLB")["source"] = "statsapi.mlb.com（官方）"
        self._record_success("MLB", len(games), (time.perf_counter() - started) * 1000)
        return games

    @staticmethod
    def _map_mlb_statsapi_game(game: dict) -> Optional[dict]:
        teams = game.get("teams") or {}
        away_side = teams.get("away") or {}
        home_side = teams.get("home") or {}
        away_team = away_side.get("team") or {}
        home_team = home_side.get("team") or {}
        if not away_team or not home_team:
            return None

        linescore = game.get("linescore") or {}
        ls_teams = linescore.get("teams") or {}
        away_runs = (ls_teams.get("away") or {}).get("runs")
        home_runs = (ls_teams.get("home") or {}).get("runs")
        away_score = int(away_runs if away_runs is not None else (away_side.get("score") or 0))
        home_score = int(home_runs if home_runs is not None else (home_side.get("score") or 0))

        status_info = game.get("status") or {}
        abstract = str(status_info.get("abstractGameState") or "")
        status = {"Live": "in", "Final": "post"}.get(abstract, "pre")

        inning = int(linescore.get("currentInning") or 0)
        inline_half = str(linescore.get("inningState") or "").strip()
        ordinal = linescore.get("currentInningOrdinal") or (f"{inning}th" if inning else "")
        detail = f"{inline_half} {ordinal}".strip() or str(
            status_info.get("detailedState") or ""
        )

        return {
            "game_id": str(game.get("gamePk") or ""),
            "home_team": home_team.get("abbreviation") or "?",
            "away_team": away_team.get("abbreviation") or "?",
            "home_team_name": home_team.get("name") or home_team.get("abbreviation") or "?",
            "away_team_name": away_team.get("name") or away_team.get("abbreviation") or "?",
            "home_score": home_score,
            "away_score": away_score,
            "period": inning,
            "status_detail": detail,
            "status": status,
            "updated_at": time.time(),
        }

    async def _fetch_mlb_espn(self, dates: list = None) -> list[dict]:
        if not self._session:
            return []
        started = time.perf_counter()
        try:
            # ESPN 的 dates 参数要 YYYYMMDD（带横杠会 400），多个日期用逗号分隔
            params = (
                {"dates": ",".join(str(d).replace("-", "") for d in dates)}
                if dates else None
            )
            async with self._session.get(
                self.config.espn_mlb_url, params=params, timeout=self._timeout()
            ) as resp:
                if resp.status != 200:
                    logger.warning("ESPN MLB scoreboard returned status %d", resp.status)
                    self._record_failure("MLB", RuntimeError(f"HTTP {resp.status}"))
                    return []
                data = await resp.json()
        except Exception as exc:
            logger.warning("ESPN MLB scoreboard fetch failed: %s", self._describe(exc))
            self._record_failure("MLB", exc)
            return []

        games = []
        for event in data.get("events", []):
            competition = event.get("competitions", [{}])[0]
            status_info = competition.get("status", {})
            status_type = status_info.get("type", {}).get("state", "")

            home = away = None
            for team in competition.get("competitors", []):
                if team.get("homeAway") == "home":
                    home = team
                else:
                    away = team
            if not home or not away:
                continue

            games.append({
                "game_id": event.get("id", ""),
                "home_team": home.get("team", {}).get("abbreviation", "?"),
                "away_team": away.get("team", {}).get("abbreviation", "?"),
                "home_team_name": home.get("team", {}).get("displayName", home.get("team", {}).get("abbreviation", "?")),
                "away_team_name": away.get("team", {}).get("displayName", away.get("team", {}).get("abbreviation", "?")),
                "home_score": int(home.get("score", 0)),
                "away_score": int(away.get("score", 0)),
                "period": status_info.get("period", 0),
                "status_detail": status_info.get("type", {}).get("detail", ""),
                "status": status_type,
                "updated_at": time.time(),
            })
        self._health_entry("MLB")["source"] = "ESPN Scoreboard（回退）"
        self._record_success("MLB", len(games), (time.perf_counter() - started) * 1000)
        return games

    async def fetch_tennis_matches(
        self,
        *,
        include_completed: bool = False,
        game_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        if not self._session:
            return []
        matches = []
        seen_ids = set()
        errors: list[str] = []
        skipped_doubles = 0
        started = time.perf_counter()
        for url in self.config.espn_tennis_urls:
            tour = url.rsplit("/", 2)[-2]
            try:
                async with self._session.get(url, timeout=self._timeout()) as resp:
                    if resp.status != 200:
                        errors.append(f"HTTP {resp.status} ({tour})")
                        continue
                    data = await resp.json()
            except Exception as exc:
                errors.append(f"{self._describe(exc)} ({tour})")
                continue

            for event in data.get("events", []):
                for grouping in event.get("groupings", []):
                    for comp in grouping.get("competitions", []):
                        status_info = comp.get("status", {})
                        status_type = status_info.get("type", {}).get("state", "")
                        if status_type != "in" and not include_completed:
                            continue

                        players = []
                        for c in comp.get("competitors", []):
                            linescores = [int(l.get("value", 0)) for l in c.get("linescores", [])]
                            possession = c.get("possession")
                            players.append({
                                "name": c.get("athlete", {}).get("displayName", "?"),
                                "linescores": linescores,
                                # 谁在发球（ESPN 的 possession）。缺失时给 None —— 下游必须
                                # 区分「不在这边发球」和「不知道谁发球」，不知道时不能当成
                                # 「没破发」去拦信号（2026-09-16 加的破发影子统计要用）。
                                "serving": possession if isinstance(possession, bool) else None,
                            })

                        if len(players) != 2:
                            continue
                        # ESPN 的双打场次用 competitors[].roster 存球员，没有 athlete 字段。
                        # 这些场次取不到名字（会变成 "?"），却带着正常的盘分，会被当成单打
                        # 判出"锁定"信号，最后必然 NO_MARKET —— 既污染信号日志，又存在用
                        # "?" 去搜市场的小概率误匹配风险。这里直接跳过：
                        #   * 信号/看板路径（include_completed=False）不采集双打；
                        #   * 结算回填路径仍然保留，万一有历史仓位需要按 match_id 补结算。
                        names_ok = all(
                            str(p.get("name") or "").strip() not in ("", "?") for p in players
                        )
                        if not names_ok:
                            if not include_completed:
                                skipped_doubles += 1
                                continue

                        player1 = players[0]
                        player2 = players[1]
                        player1_sets = sum(
                            1 for a, b in zip(player1["linescores"], player2["linescores"]) if a > b
                        )
                        player2_sets = sum(
                            1 for a, b in zip(player1["linescores"], player2["linescores"]) if b > a
                        )
                        mid = comp.get("id", "")
                        if mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        if game_ids is not None and mid not in game_ids:
                            continue

                        matches.append({
                            "game_id": mid,
                            "match_id": mid,
                            "player1": player1,
                            "player2": player2,
                            "away_team": player1.get("name", ""),
                            "home_team": player2.get("name", ""),
                            "away_score": player1_sets,
                            "home_score": player2_sets,
                            "period": status_info.get("period", 0),
                            "status_detail": status_info.get("type", {}).get("detail", ""),
                            "status": status_type,
                            "updated_at": time.time(),
                        })
        if errors:
            logger.warning("ESPN tennis scoreboard fetch failed: %s", "; ".join(errors))
            self._record_failure("TENNIS", RuntimeError("; ".join(errors)))
        else:
            self._record_success("TENNIS", len(matches), (time.perf_counter() - started) * 1000)
            if skipped_doubles:
                logger.debug("网球：跳过 %d 场双打（ESPN 双打无 athlete 字段）", skipped_doubles)
        return matches
