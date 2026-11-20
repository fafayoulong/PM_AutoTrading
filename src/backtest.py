# -*- coding: utf-8 -*-
"""Backtest framework - replays historical NBA games to evaluate lock signals."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from src.config import Config
from src.signal import required_score_diff

logger = logging.getLogger(__name__)


@dataclass
class HistoricalGame:
    game_id: str
    date: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    winner: str  # "home" or "away"


@dataclass
class BacktestTrade:
    game_id: str
    date: str
    leader_team: str
    underdog_team: str
    entry_clock_seconds: float
    entry_quarter: int
    entry_score_diff: int
    required_diff: int
    entry_price: float
    size: float
    cost: float
    leader_won: bool
    exit_price: float
    pnl: float
    roi: float


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    total_cost: float = 0.0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / max(total, 1)

    @property
    def roi(self) -> float:
        return self.total_pnl / max(self.total_cost, 0.01)


def parse_clock_str(s: str) -> float:
    if not s:
        return 0.0
    s = s.strip()
    if ":" in s:
        parts = s.split(":")
        return float(parts[0]) * 60 + float(parts[1])
    return float(s)


class Backtester:
    def __init__(self, config: Config, proxy: str | None = None):
        self.config = config
        self.proxy = proxy or config.proxy_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_completed_games(self, start_date: str, end_date: str) -> list[HistoricalGame]:
        await self._ensure_session()
        games = []
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        current = start

        while current <= end:
            date_str = current.strftime("%Y%m%d")
            url = f"{self.config.espn_scoreboard_url}?dates={date_str}"
            try:
                async with self._session.get(url, proxy=self.proxy, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        current += timedelta(days=1)
                        continue
                    data = await resp.json()
            except Exception as exc:
                logger.warning("Failed to fetch games for %s: %s", date_str, exc)
                current += timedelta(days=1)
                continue

            for event in data.get("events", []):
                competition = event.get("competitions", [{}])[0]
                status = competition.get("status", {}).get("type", {}).get("state", "")
                if status != "post":
                    continue

                home = away = None
                for team in competition.get("competitors", []):
                    if team.get("homeAway") == "home":
                        home = team
                    else:
                        away = team
                if not home or not away:
                    continue

                home_score = int(home.get("score", 0))
                away_score = int(away.get("score", 0))
                games.append(HistoricalGame(
                    game_id=event.get("id", ""),
                    date=date_str,
                    home_team=home.get("team", {}).get("abbreviation", "?"),
                    away_team=away.get("team", {}).get("abbreviation", "?"),
                    home_score=home_score,
                    away_score=away_score,
                    winner="home" if home_score > away_score else "away",
                ))
            current += timedelta(days=1)

        logger.info("Fetched %d completed games from %s to %s", len(games), start_date, end_date)
        return games

    async def fetch_game_plays(self, game_id: str) -> list[dict]:
        await self._ensure_session()
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={game_id}"
        try:
            async with self._session.get(url, proxy=self.proxy, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("plays", [])
        except Exception as exc:
            logger.warning("Failed to fetch plays for game %s: %s", game_id, exc)
            return []

    def simulate_game(
        self,
        game: HistoricalGame,
        plays: list[dict],
        config: Config,
        assumed_entry_price: float,
    ) -> list[BacktestTrade]:
        trades = []
        fired = False

        for play in plays:
            period = play.get("period", {}).get("number", 0)
            if period < config.min_quarter:
                continue
            if fired:
                break

            clock_raw = play.get("clock", {}).get("displayValue", "")
            clock_seconds = parse_clock_str(clock_raw)
            if clock_seconds < config.max_time_remaining_seconds or clock_seconds > config.min_time_remaining_seconds:
                continue

            home_score = play.get("homeScore", 0)
            away_score = play.get("awayScore", 0)
            diff = abs(home_score - away_score)
            required = required_score_diff(config, clock_seconds)
            if diff < required:
                continue

            if home_score > away_score:
                leader = game.home_team
            else:
                leader = game.away_team

            leader_won = (
                (leader == game.home_team and game.winner == "home") or
                (leader == game.away_team and game.winner == "away")
            )

            size = config.max_position_size / assumed_entry_price
            cost = config.max_position_size
            exit_price = 1.0 if leader_won else 0.0
            pnl = size * exit_price - cost

            trades.append(BacktestTrade(
                game_id=game.game_id,
                date=game.date,
                leader_team=leader,
                underdog_team=game.away_team if leader == game.home_team else game.home_team,
                entry_clock_seconds=clock_seconds,
                entry_quarter=period,
                entry_score_diff=diff,
                required_diff=required,
                entry_price=assumed_entry_price,
                size=size,
                cost=cost,
                leader_won=leader_won,
                exit_price=exit_price,
                pnl=pnl,
                roi=pnl / cost if cost > 0 else 0,
            ))
            fired = True

        return trades

    async def run(
        self,
        start_date: str,
        end_date: str,
        config: Optional[Config] = None,
        assumed_entry_price: float = 0.90,
    ) -> BacktestResult:
        config = config or self.config
        games = await self.fetch_completed_games(start_date, end_date)
        result = BacktestResult()

        for game in games:
            plays = await self.fetch_game_plays(game.game_id)
            if not plays:
                continue
            trades = self.simulate_game(game, plays, config, assumed_entry_price)
            result.trades.extend(trades)

        for t in result.trades:
            result.total_cost += t.cost
            result.total_pnl += t.pnl
            if t.leader_won:
                result.wins += 1
            else:
                result.losses += 1

        return result


def print_backtest_results(result: BacktestResult, label: str = ""):
    print(f"\n{'='*60}")
    print(f"  Backtest Results {label}")
    print(f"{'='*60}")
    print(f"  Total trades:   {len(result.trades)}")
    print(f"  Wins:           {result.wins}")
    print(f"  Losses:         {result.losses}")
    print(f"  Win rate:       {result.win_rate:.1%}")
    print(f"  Total cost:     {result.total_cost:.2f} USDC")
    print(f"  Total P&L:      {result.total_pnl:.2f} USDC")
    print(f"  ROI:            {result.roi:.1%}")
    print(f"{'='*60}")

    if result.trades:
        print(f"\n  {'Date':>10} {'Leader':>8} {'Clock':>8} {'Diff':>6} {'Req':>6} {'Won':>5} {'PnL':>10}")
        print(f"  {'-'*10} {'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*5} {'-'*10}")
        for t in result.trades:
            minutes = int(t.entry_clock_seconds // 60)
            seconds = int(t.entry_clock_seconds % 60)
            clock_str = f"{minutes}:{seconds:02d}"
            print(f"  {t.date:>10} {t.leader_team:>8} {clock_str:>8} {t.entry_score_diff:>6} {t.required_diff:>6} {'Y' if t.leader_won else 'N':>5} {t.pnl:>+10.2f}")
