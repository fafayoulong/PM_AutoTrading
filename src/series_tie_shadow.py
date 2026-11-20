# -*- coding: utf-8 -*-
"""Deciding-map tied-score shadow portfolios for series-based esports.

This module is read-only.  It records what would have happened if we bought
the cheaper side whenever a best-of series reached a new tied round/game score
in its deciding map.  No order is ever submitted from here.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from src import settlement_lookup
from src.atomic_io import rotate_by_size
from src.config import Config
from src.cs2_signal import parse_cs2_score

logger = logging.getLogger(__name__)


def cs2_deciding_map_tie_state(game: dict, max_round_score: int = 12) -> dict | None:
    """Return a tied state in a live CS2 deciding map, or ``None``.

    A BO3 is decided on map 3 and a BO5 on map 5.  The series score must be
    tied immediately before that map (1-1 or 2-2).  The current map round score
    must also be tied, excluding 0-0 because that is the start of the map.
    """
    if not game.get("live") or game.get("ended"):
        return None
    if str(game.get("status") or "").lower() not in {
        "running", "inprogress", "live", "in"
    }:
        return None

    parsed = parse_cs2_score(str(game.get("score") or ""))
    if parsed is None:
        return None
    home_rounds, away_rounds, home_maps, away_maps, maps_to_win = parsed
    if not maps_to_win or maps_to_win <= 1:
        return None

    deciding_map_number = maps_to_win * 2 - 1
    required_map_wins = maps_to_win - 1
    if home_maps != required_map_wins or away_maps != required_map_wins:
        return None
    if home_rounds != away_rounds or not (1 <= home_rounds <= max(1, max_round_score)):
        return None

    home_team = str(game.get("homeTeam") or "").strip()
    away_team = str(game.get("awayTeam") or "").strip()
    if not home_team or not away_team:
        return None
    round_score = f"{home_rounds}-{away_rounds}"
    return {
        "sport": "CS2",
        "game_id": str(game.get("gameId") or ""),
        "league": str(game.get("leagueAbbreviation") or "CS2"),
        "series_format": f"Bo{deciding_map_number}",
        "deciding_map_number": deciding_map_number,
        "series_score": f"{home_maps}-{away_maps}",
        "round_score": round_score,
        "rounds": home_rounds,
        "home_team": home_team,
        "away_team": away_team,
        "period": str(game.get("period") or ""),
        "status_detail": str(game.get("status") or ""),
        "feed_updated_at": game.get("received_at"),
    }


class SeriesTieShadowTracker:
    """Persistent, read-only ledger for deciding-map tied scores."""

    entry_kind = "series-tie-shadow-entry"
    settlement_kind = "series-tie-shadow-settlement"
    strategy = "series_deciding_map_tie_low"
    id_prefix = "STIE"

    def __init__(self, config: Config):
        self.enabled = bool(getattr(config, "cs2_tie_shadow_enabled", True))
        self.path = Path(
            getattr(config, "cs2_tie_shadow_path", "trades/cs2_tie_shadow.jsonl")
        )
        self.stake = max(
            0.01, float(getattr(config, "cs2_tie_shadow_stake_usdc", 1.0) or 1.0)
        )
        self.fee_rate = max(
            0.0, float(getattr(config, "cs2_tie_shadow_fee_rate", 0.05) or 0.0)
        )
        self.reconcile_interval = max(
            30.0,
            float(
                getattr(config, "cs2_tie_shadow_reconcile_interval_seconds", 300.0)
            ),
        )
        self.reconcile_min_age = max(
            0.0,
            float(
                getattr(config, "cs2_tie_shadow_reconcile_min_age_seconds", 300.0)
            ),
        )
        self.timeout = max(
            3.0, float(getattr(config, "cs2_tie_shadow_timeout_seconds", 20.0))
        )
        self.max_bytes = max(
            0, int(getattr(config, "cs2_tie_shadow_max_bytes", 32 * 1024 * 1024))
        )
        self.backup_count = max(
            0, int(getattr(config, "cs2_tie_shadow_backup_count", 10))
        )
        self.max_round_score = max(
            1, int(getattr(config, "cs2_tie_shadow_max_round_score", 12))
        )
        self._entries: dict[str, dict] = {}
        self._settlements: dict[str, dict] = {}
        self._attempted_at: dict[str, float] = {}
        self._records_at_start = 0
        self._records_written = 0
        self._attempts = 0
        self._misses = 0
        self._last_error = ""
        self._last_reconcile_at = 0.0
        self._load()

    @staticmethod
    def _shadow_id(game_id: str, map_number: int, round_score: str) -> str:
        return (
            f"CS2TIE_{game_id}_M{int(map_number)}_"
            f"{round_score.replace('-', '_')}"
        )

    def shadow_id(self, state: dict) -> str:
        return self._shadow_id(
            str(state["game_id"]),
            int(state["deciding_map_number"]),
            str(state["round_score"]),
        )

    @property
    def _paths(self) -> list[str]:
        paths = [p for p in glob.glob(str(self.path) + "*") if Path(p).is_file()]
        return sorted(paths, key=lambda p: Path(p).stat().st_mtime)

    def _load(self) -> None:
        for path in self._paths:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as file:
                    for line in file:
                        if not line.strip():
                            continue
                        self._records_at_start += 1
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        shadow_id = str(row.get("shadow_id") or "")
                        if not shadow_id:
                            continue
                        if row.get("kind") == self.entry_kind:
                            self._entries[shadow_id] = row
                        elif row.get("kind") == self.settlement_kind:
                            self._settlements[shadow_id] = row
            except OSError:
                continue

    def should_attempt(self, state: dict, now: float | None = None) -> bool:
        shadow_id = self.shadow_id(state)
        if shadow_id in self._entries:
            return False
        when = float(now if now is not None else time.time())
        if when - self._attempted_at.get(shadow_id, 0.0) < 3.0:
            return False
        self._attempted_at[shadow_id] = when
        self._attempts += 1
        return True

    def note_miss(self, reason: str) -> None:
        self._misses += 1
        self._last_error = str(reason or "unknown")[:500]

    def record_entry(
        self,
        *,
        state: dict,
        question: str,
        players: list[dict],
        now: float | None = None,
    ) -> dict | None:
        """Record one virtual buy of the cheaper outcome."""
        if not self.enabled or len(players) != 2:
            return None
        valid: list[dict] = []
        for item in players:
            try:
                ask = float(item.get("best_ask"))
            except (TypeError, ValueError):
                self.note_miss(f"NO_ASK:{item.get('name') or '?'}")
                return None
            try:
                bid = (
                    float(item.get("best_bid"))
                    if item.get("best_bid") is not None
                    else None
                )
            except (TypeError, ValueError):
                bid = None
            if not (0.0 < ask < 1.0) or not item.get("token_id"):
                self.note_miss(f"BAD_QUOTE:{item.get('name') or '?'}")
                return None
            valid.append({**item, "best_ask": ask, "best_bid": bid})

        selected = min(valid, key=lambda item: item["best_ask"])
        other = max(valid, key=lambda item: item["best_ask"])
        shadow_id = self.shadow_id(state)
        if shadow_id in self._entries:
            return None
        when = float(now if now is not None else time.time())
        ask = float(selected["best_ask"])
        shares = self.stake / ask
        fee = self.stake * self.fee_rate * (1.0 - ask)
        row = {
            "kind": self.entry_kind,
            "t": when,
            "beijing_time": datetime.fromtimestamp(when).astimezone().isoformat(
                timespec="seconds"
            ),
            "shadow_id": shadow_id,
            "sport": state.get("sport", "CS2"),
            "strategy": self.strategy,
            "game_id": state["game_id"],
            "question": question,
            "series_format": state.get("series_format", ""),
            "deciding_map_number": int(state["deciding_map_number"]),
            "series_score": state.get("series_score", ""),
            "round_score": state["round_score"],
            "period": state.get("period", ""),
            "status_detail": state.get("status_detail", ""),
            "feed_updated_at": state.get("feed_updated_at"),
            "home_team": state.get("home_team", ""),
            "away_team": state.get("away_team", ""),
            "selected": selected.get("name", ""),
            "selected_token_id": str(selected["token_id"]),
            "selected_best_bid": (
                round(float(selected["best_bid"]), 6)
                if selected["best_bid"] is not None
                else None
            ),
            "selected_ask": round(ask, 6),
            "selected_ask_size": selected.get("ask_size"),
            "selected_ask_depth5": selected.get("ask_depth5"),
            "other": other.get("name", ""),
            "other_token_id": str(other["token_id"]),
            "other_best_bid": (
                round(float(other["best_bid"]), 6)
                if other["best_bid"] is not None
                else None
            ),
            "other_ask": round(float(other["best_ask"]), 6),
            "other_ask_size": other.get("ask_size"),
            "other_ask_depth5": other.get("ask_depth5"),
            "stake_usdc": round(self.stake, 6),
            "shares": round(shares, 6),
            "fee_rate": self.fee_rate,
            "entry_fee_usdc": round(fee, 6),
            "breakeven_win_rate": round((self.stake + fee) / shares, 6),
            "pnl_if_win": round(shares * (1.0 - ask) - fee, 6),
            "pnl_if_loss": round(-self.stake - fee, 6),
            "status": "OPEN",
            "note": "只读影子仓；未提交任何实盘订单",
        }
        if self._append([row]):
            self._entries[shadow_id] = row
            self._attempted_at.pop(shadow_id, None)
            self._last_error = ""
            return row
        return None

    def _settle_entry(self, entry: dict, exit_price: float, note: str) -> dict | None:
        shadow_id = str(entry.get("shadow_id") or "")
        if not shadow_id or shadow_id in self._settlements:
            return None
        stake = float(entry.get("stake_usdc") or 0.0)
        shares = float(entry.get("shares") or 0.0)
        fee = float(entry.get("entry_fee_usdc") or 0.0)
        pnl = shares * float(exit_price) - stake - fee
        cost = stake + fee
        row = {
            "kind": self.settlement_kind,
            "t": time.time(),
            "beijing_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "shadow_id": shadow_id,
            "sport": entry.get("sport", "CS2"),
            "game_id": entry.get("game_id"),
            "deciding_map_number": entry.get("deciding_map_number"),
            "round_score": entry.get("round_score"),
            "selected": entry.get("selected"),
            "selected_won": bool(exit_price >= 0.5),
            "exit_price": round(float(exit_price), 6),
            "pnl": round(pnl, 6),
            "roi": round(pnl / cost, 6) if cost > 0 else None,
            "note": note,
        }
        if self._append([row]):
            self._settlements[shadow_id] = row
            return row
        return None

    def settle_completed_game(self, game: dict) -> list[dict]:
        """Settle immediately when the final CS2 score is present in the feed."""
        if not game.get("ended"):
            return []
        parsed = parse_cs2_score(str(game.get("score") or ""))
        if parsed is None:
            return []
        _home_rounds, _away_rounds, home_maps, away_maps, _best_of = parsed
        if home_maps == away_maps:
            return []
        winner = str(
            game.get("homeTeam") if home_maps > away_maps else game.get("awayTeam")
        ).strip()
        game_id = str(game.get("gameId") or "")
        if not winner or not game_id:
            return []
        settled: list[dict] = []
        for entry in self._entries.values():
            if str(entry.get("game_id")) != game_id:
                continue
            selected_won = (
                settlement_lookup.normalize(entry.get("selected"))
                == settlement_lookup.normalize(winner)
            )
            row = self._settle_entry(
                entry,
                1.0 if selected_won else 0.0,
                f"终局比分 {home_maps}-{away_maps}，{winner} 获胜",
            )
            if row:
                settled.append(row)
        return settled

    async def reconcile_open(self, force: bool = False) -> list[dict]:
        """Resolve stale entries through Polymarket settlement."""
        if not self.enabled:
            return []
        now = time.time()
        if not force and now - self._last_reconcile_at < self.reconcile_interval:
            return []
        self._last_reconcile_at = now
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for shadow_id, entry in self._entries.items():
            if shadow_id in self._settlements:
                continue
            if now - float(entry.get("t") or 0.0) < self.reconcile_min_age:
                continue
            key = (
                str(entry.get("question") or ""),
                str(entry.get("selected_token_id") or ""),
                str(entry.get("selected") or ""),
            )
            groups.setdefault(key, []).append(entry)

        settled: list[dict] = []
        for (question, token_id, selected), entries in list(groups.items())[:4]:
            if not question or not token_id:
                continue
            try:
                price, note = await asyncio.to_thread(
                    settlement_lookup.resolve_outcome_price,
                    question,
                    token_id,
                    selected,
                    self.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - shadow work must not affect trading
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("CS2 tie shadow settlement lookup failed: %s", exc)
                continue
            if price is None:
                self._last_error = str(note)[:500]
                continue
            for entry in entries:
                row = self._settle_entry(entry, float(price), note)
                if row:
                    settled.append(row)
        return settled

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                await self.reconcile_open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("CS2 tie shadow settlement loop failed: %s", exc)
            await asyncio.sleep(min(60.0, self.reconcile_interval))

    def _append(self, rows: list[dict]) -> bool:
        if not rows:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rotate_by_size(
                self.path, max_bytes=self.max_bytes, backup_count=self.backup_count
            )
            with self.path.open("a", encoding="utf-8") as file:
                for row in rows:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._records_written += len(rows)
            return True
        except OSError as exc:
            self._last_error = str(exc)
            logger.warning("CS2 tie shadow ledger write failed: %s", exc)
            return False

    def _summary(self, rows: list[dict]) -> dict:
        settled = [
            self._settlements[row["shadow_id"]]
            for row in rows
            if row.get("shadow_id") in self._settlements
        ]
        wins = sum(bool(row.get("selected_won")) for row in settled)
        cost = sum(
            float(self._entries[row["shadow_id"]].get("stake_usdc") or 0.0)
            + float(self._entries[row["shadow_id"]].get("entry_fee_usdc") or 0.0)
            for row in settled
        )
        pnl = sum(float(row.get("pnl") or 0.0) for row in settled)
        return {
            "count": len(rows),
            "open": len(rows) - len(settled),
            "settled": len(settled),
            "wins": wins,
            "losses": len(settled) - wins,
            "win_rate": round(wins / len(settled), 6) if settled else None,
            "cost_usdc": round(cost, 6),
            "pnl_usdc": round(pnl, 6),
            "roi": round(pnl / cost, 6) if cost > 0 else None,
        }

    def status(self) -> dict:
        by_score = {
            f"{score}-{score}": self._summary(
                [
                    row
                    for row in self._entries.values()
                    if row.get("round_score") == f"{score}-{score}"
                ]
            )
            for score in range(1, self.max_round_score + 1)
        }
        recent: list[dict] = []
        for entry in sorted(
            self._entries.values(),
            key=lambda row: float(row.get("t") or 0.0),
            reverse=True,
        )[:20]:
            settled = self._settlements.get(str(entry.get("shadow_id") or ""))
            recent.append(
                {
                    "t": entry.get("t"),
                    "beijing_time": entry.get("beijing_time"),
                    "game_id": entry.get("game_id"),
                    "question": entry.get("question"),
                    "series_score": entry.get("series_score"),
                    "deciding_map_number": entry.get("deciding_map_number"),
                    "round_score": entry.get("round_score"),
                    "selected": entry.get("selected"),
                    "selected_ask": entry.get("selected_ask"),
                    "status": "SETTLED" if settled else "OPEN",
                    "selected_won": settled.get("selected_won") if settled else None,
                    "pnl": settled.get("pnl") if settled else None,
                    "roi": settled.get("roi") if settled else None,
                }
            )
        return {
            "enabled": self.enabled,
            "sport": "CS2",
            "strategy": self.strategy,
            "path": str(self.path),
            "stake_usdc": self.stake,
            "fee_rate": self.fee_rate,
            "max_round_score": self.max_round_score,
            "attempts_this_run": self._attempts,
            "misses_this_run": self._misses,
            "records_this_run": self._records_written,
            "records_total": self._records_at_start + self._records_written,
            "last_error": self._last_error,
            **self._summary(list(self._entries.values())),
            "by_score": by_score,
            "recent": recent,
        }
