# -*- coding: utf-8 -*-
"""Deciding-set tied-score shadow portfolio.

This module never submits an order.  It records what would have happened if we
bought the cheaper side whenever a best-of-three tennis match reached a new
tied game score in the deciding set (1-1, 2-2, ..., 6-6).

Each ``(match_id, game_score)`` triggers at most once.  Entries and settlements
are append-only JSONL records so a process restart does not erase history.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src import settlement_lookup
from src.atomic_io import rotate_by_size
from src.config import Config

logger = logging.getLogger(__name__)


def _line_wins(lines_a: list, lines_b: list) -> tuple[int, int]:
    wins_a = sum(1 for a, b in zip(lines_a, lines_b) if int(a) > int(b))
    wins_b = sum(1 for a, b in zip(lines_a, lines_b) if int(b) > int(a))
    return wins_a, wins_b


def deciding_tie_state(match: dict) -> dict | None:
    """Return the deciding-set tied state, or ``None``.

    The current bot models tennis as best-of-three.  The deciding set must be
    the third set, the first two sets must be split 1-1, and the current games
    must be tied at 1-1 through 6-6.  ``0-0`` is intentionally excluded because
    it is the start of a set rather than a tie reached during play.
    """
    if str(match.get("status") or "").lower() not in {"in", "live"}:
        return None
    p1 = match.get("player1") or {}
    p2 = match.get("player2") or {}
    ls1 = list(p1.get("linescores") or [])
    ls2 = list(p2.get("linescores") or [])
    if len(ls1) != 3 or len(ls2) != 3:
        return None
    sets1, sets2 = _line_wins(ls1[:-1], ls2[:-1])
    if sets1 != 1 or sets2 != 1:
        return None
    try:
        games1, games2 = int(ls1[-1]), int(ls2[-1])
    except (TypeError, ValueError):
        return None
    if games1 != games2 or not (1 <= games1 <= 6):
        return None
    serving = None
    if p1.get("serving") is True:
        serving = str(p1.get("name") or "")
    elif p2.get("serving") is True:
        serving = str(p2.get("name") or "")
    return {
        "match_id": str(match.get("match_id") or ""),
        "set_index": 3,
        "set_score": f"{sets1}-{sets2}",
        "game_score": f"{games1}-{games2}",
        "games": games1,
        "player1": str(p1.get("name") or ""),
        "player2": str(p2.get("name") or ""),
        "serving": serving,
        "status_detail": str(match.get("status_detail") or ""),
        "feed_updated_at": match.get("updated_at"),
    }


class TennisTieShadowTracker:
    """Persistent, read-only shadow ledger for deciding-set tied scores."""

    def __init__(self, config: Config):
        self.enabled = bool(getattr(config, "tennis_tie_shadow_enabled", True))
        self.path = Path(
            getattr(config, "tennis_tie_shadow_path", "trades/tennis_tie_shadow.jsonl")
        )
        self.stake = max(
            0.01, float(getattr(config, "tennis_tie_shadow_stake_usdc", 1.0) or 1.0)
        )
        self.fee_rate = max(
            0.0, float(getattr(config, "tennis_tie_shadow_fee_rate", 0.05) or 0.0)
        )
        self.reconcile_interval = max(
            30.0,
            float(getattr(config, "tennis_tie_shadow_reconcile_interval_seconds", 300.0)),
        )
        self.reconcile_min_age = max(
            0.0,
            float(getattr(config, "tennis_tie_shadow_reconcile_min_age_seconds", 300.0)),
        )
        self.timeout = max(
            3.0, float(getattr(config, "tennis_tie_shadow_timeout_seconds", 20.0))
        )
        self.max_bytes = max(
            0, int(getattr(config, "tennis_tie_shadow_max_bytes", 32 * 1024 * 1024))
        )
        self.backup_count = max(
            0, int(getattr(config, "tennis_tie_shadow_backup_count", 10))
        )
        self._entries: dict[str, dict] = {}
        self._settlements: dict[str, dict] = {}
        self._attempted_at: dict[str, float] = {}
        self._records_at_start = 0
        self._records_written = 0
        self._last_error = ""
        self._last_reconcile_at = 0.0
        self._load()

    @staticmethod
    def _shadow_id(match_id: str, game_score: str) -> str:
        return f"TTIE_{match_id}_{game_score.replace('-', '_')}"

    def shadow_id(self, state: dict) -> str:
        return self._shadow_id(str(state["match_id"]), str(state["game_score"]))

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
                        kind = row.get("kind")
                        if kind == "tennis-tie-shadow-entry":
                            self._entries[shadow_id] = row
                        elif kind == "tennis-tie-shadow-settlement":
                            self._settlements[shadow_id] = row
            except OSError:
                continue

    def should_attempt(self, state: dict, now: float | None = None) -> bool:
        shadow_id = self._shadow_id(state["match_id"], state["game_score"])
        if shadow_id in self._entries:
            return False
        now = float(now if now is not None else time.time())
        last = self._attempted_at.get(shadow_id, 0.0)
        if now - last < 3.0:
            return False
        self._attempted_at[shadow_id] = now
        return True

    def record_entry(
        self,
        *,
        state: dict,
        question: str,
        players: list[dict],
        live_order_id: str = "",
        live_status: str = "",
        now: float | None = None,
    ) -> dict | None:
        """Record one virtual buy of the cheaper outcome.

        ``players`` contains exactly two rows with ``name``, ``token_id``,
        ``best_bid`` and ``best_ask``.
        """
        if not self.enabled:
            return None
        if len(players) != 2:
            return None
        valid: list[dict] = []
        for item in players:
            try:
                ask = float(item.get("best_ask"))
            except (TypeError, ValueError):
                return None
            try:
                bid = float(item.get("best_bid")) if item.get("best_bid") is not None else None
            except (TypeError, ValueError):
                bid = None
            if not (0.0 < ask < 1.0) or not item.get("token_id"):
                return None
            valid.append({**item, "best_ask": ask, "best_bid": bid})
        selected = min(valid, key=lambda item: item["best_ask"])
        other = max(valid, key=lambda item: item["best_ask"])
        shadow_id = self._shadow_id(state["match_id"], state["game_score"])
        if shadow_id in self._entries:
            return None
        when = float(now if now is not None else time.time())
        ask = float(selected["best_ask"])
        shares = self.stake / ask
        fee = self.stake * self.fee_rate * (1.0 - ask)
        row = {
            "kind": "tennis-tie-shadow-entry",
            "t": when,
            "beijing_time": datetime.fromtimestamp(when).astimezone().isoformat(
                timespec="seconds"
            ),
            "shadow_id": shadow_id,
            "match_id": state["match_id"],
            "question": question,
            "set_index": state["set_index"],
            "set_score": state["set_score"],
            "game_score": state["game_score"],
            "status_detail": state.get("status_detail", ""),
            "feed_updated_at": state.get("feed_updated_at"),
            "serving": state.get("serving"),
            "player1": state.get("player1", ""),
            "player2": state.get("player2", ""),
            "selected": selected.get("name", ""),
            "selected_token_id": str(selected["token_id"]),
            "selected_best_bid": (
                round(float(selected["best_bid"]), 6)
                if selected["best_bid"] is not None
                else None
            ),
            "selected_ask": round(ask, 6),
            "selected_ask_size": selected.get("ask_size"),
            "other": other.get("name", ""),
            "other_token_id": str(other["token_id"]),
            "other_best_bid": (
                round(float(other["best_bid"]), 6)
                if other["best_bid"] is not None
                else None
            ),
            "other_ask": round(float(other["best_ask"]), 6),
            "other_ask_size": other.get("ask_size"),
            "stake_usdc": round(self.stake, 6),
            "shares": round(shares, 6),
            "fee_rate": self.fee_rate,
            "entry_fee_usdc": round(fee, 6),
            "breakeven_win_rate": round((self.stake + fee) / shares, 6),
            "pnl_if_win": round(shares * (1.0 - ask) - fee, 6),
            "pnl_if_loss": round(-self.stake - fee, 6),
            "status": "OPEN",
            "live_order_id": live_order_id,
            "live_status": live_status,
            "note": (
                f"影子记录与实盘同步；live_order_id={live_order_id}"
                if live_order_id
                else "影子陪跑仓；本次未提交实盘订单"
            ),
        }
        if self._append([row]):
            self._entries[shadow_id] = row
            self._attempted_at.pop(shadow_id, None)
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
            "kind": "tennis-tie-shadow-settlement",
            "t": time.time(),
            "beijing_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "shadow_id": shadow_id,
            "match_id": entry.get("match_id"),
            "game_score": entry.get("game_score"),
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

    def settle_completed_match(self, match: dict) -> list[dict]:
        """Settle entries quickly when the completed match is still in the feed."""
        status = str(match.get("status") or "").lower()
        detail = str(match.get("status_detail") or "").lower()
        if status not in {"post", "final", "completed"} and detail != "final":
            return []
        p1 = match.get("player1") or {}
        p2 = match.get("player2") or {}
        wins1, wins2 = _line_wins(
            list(p1.get("linescores") or []), list(p2.get("linescores") or [])
        )
        if wins1 == wins2:
            return []
        winner = str(p1.get("name") if wins1 > wins2 else p2.get("name") or "")
        match_id = str(match.get("match_id") or "")
        settled: list[dict] = []
        for entry in self._entries.values():
            if str(entry.get("match_id")) != match_id:
                continue
            selected_won = (
                settlement_lookup.normalize(entry.get("selected"))
                == settlement_lookup.normalize(winner)
            )
            row = self._settle_entry(
                entry,
                1.0 if selected_won else 0.0,
                f"比分终局结算：{winner} 获胜",
            )
            if row:
                settled.append(row)
        return settled

    async def reconcile_open(self, force: bool = False) -> list[dict]:
        """Resolve stale open shadow entries against Polymarket settlement."""
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
            age = now - float(entry.get("t") or 0.0)
            if age < self.reconcile_min_age:
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
            except Exception as exc:  # noqa: BLE001 - observation must not affect trading
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("网球平分影子仓结算查询失败：%s", exc)
                continue
            if price is None:
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
                logger.debug("网球平分影子仓后台结算异常：%s", exc)
            await asyncio.sleep(min(60.0, self.reconcile_interval))

    def _append(self, rows: list[dict]) -> bool:
        if not rows:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rotate_by_size(self.path, max_bytes=self.max_bytes, backup_count=self.backup_count)
            with self.path.open("a", encoding="utf-8") as file:
                for row in rows:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._records_written += len(rows)
            return True
        except OSError as exc:
            self._last_error = str(exc)
            logger.warning("写入网球平分影子仓失败：%s", exc)
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
        by_score: dict[str, dict] = {}
        for score in ("1-1", "2-2", "3-3", "4-4", "5-5", "6-6"):
            rows = [row for row in self._entries.values() if row.get("game_score") == score]
            by_score[score] = self._summary(rows)
        recent: list[dict] = []
        for entry in sorted(
            self._entries.values(), key=lambda row: float(row.get("t") or 0.0), reverse=True
        )[:20]:
            settled = self._settlements.get(str(entry.get("shadow_id") or ""))
            recent.append(
                {
                    "t": entry.get("t"),
                    "beijing_time": entry.get("beijing_time"),
                    "match_id": entry.get("match_id"),
                    "question": entry.get("question"),
                    "game_score": entry.get("game_score"),
                    "selected": entry.get("selected"),
                    "selected_ask": entry.get("selected_ask"),
                    "serving": entry.get("serving"),
                    "status": "SETTLED" if settled else "OPEN",
                    "live_order_id": entry.get("live_order_id"),
                    "live_status": entry.get("live_status"),
                    "selected_won": settled.get("selected_won") if settled else None,
                    "pnl": settled.get("pnl") if settled else None,
                    "roi": settled.get("roi") if settled else None,
                }
            )
        summary = self._summary(list(self._entries.values()))
        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "stake_usdc": self.stake,
            "fee_rate": self.fee_rate,
            "records_this_run": self._records_written,
            "records_total": self._records_at_start + self._records_written,
            "last_error": self._last_error,
            **summary,
            "by_score": by_score,
            "recent": recent,
        }
