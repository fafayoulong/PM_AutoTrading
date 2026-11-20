# -*- coding: utf-8 -*-
"""Event-level timing ledger for deciding-set tie trades.

The existing shadow ledger answers "would this trade have won?". This ledger
answers a different question: how did the score-change signal travel through
the bot to the exchange?

Every trigger shares a stable ``event_id`` and writes multiple records to one
append-only JSONL file:

    TRIGGER
    MARKET
    QUOTE
    ORDER
    ORDER_UPDATE
    ERROR

That lets us compare the score-source timestamp, book snapshot, submission
request, exchange acknowledgement, and actual fill without treating a later
15-second model-ledger snapshot as the score-change price.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.atomic_io import rotate_by_size
from src.config import Config


logger = logging.getLogger(__name__)
BJT = ZoneInfo("Asia/Shanghai")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


class TennisTieEventRecorder:
    """Append-only event trace for the tennis deciding-set tie strategy."""

    def __init__(self, config: Config):
        self.enabled = bool(getattr(config, "tennis_tie_events_enabled", True))
        self.path = Path(
            getattr(config, "tennis_tie_events_path", "trades/tennis_tie_events.jsonl")
        )
        self.max_bytes = max(
            0, int(getattr(config, "tennis_tie_events_max_bytes", 32 * 1024 * 1024))
        )
        self.backup_count = max(
            0, int(getattr(config, "tennis_tie_events_backup_count", 8))
        )
        self.last_error = ""
        self.records_written = 0
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._last_order_update: dict[str, tuple] = {}

    @staticmethod
    def event_id(
        match_id: str,
        game_score: str,
        triggered_at: float | None = None,
    ) -> str:
        stamp = triggered_at if triggered_at is not None else time.time()
        return (
            f"TTEV_{match_id}_{game_score.replace('-', '_')}_"
            f"{int(stamp * 1000)}"
        )

    def _append(
        self,
        kind: str,
        event_id: str,
        data: dict,
        *,
        dedupe_key: str = "",
    ) -> bool:
        if not self.enabled or not event_id:
            return False
        key = (kind, f"{event_id}|{dedupe_key}") if dedupe_key else None
        row = {
            "kind": kind,
            "event_id": event_id,
            "t": time.time(),
            "beijing_time": datetime.now(BJT).isoformat(timespec="milliseconds"),
            **_json_safe(data),
        }
        try:
            with self._lock:
                if key is not None and key in self._seen:
                    return False
                self.path.parent.mkdir(parents=True, exist_ok=True)
                rotate_by_size(
                    self.path,
                    max_bytes=self.max_bytes,
                    backup_count=self.backup_count,
                )
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.records_written += 1
                if key is not None:
                    self._seen.add(key)
            return True
        except Exception as exc:  # noqa: BLE001 - telemetry must not stop trading
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("网球事件账本写入失败：%s", exc)
            return False

    def record_trigger(
        self,
        *,
        event_id: str,
        match: dict,
        state: dict,
        score_change_at: float | None,
        trigger_at: float,
    ) -> None:
        feed_updated_at = match.get("updated_at")
        data = {
            "match_id": state.get("match_id"),
            "question": match.get("question") or "",
            "tournament": match.get("tournament") or "",
            "match_status": match.get("status"),
            "status_detail": state.get("status_detail"),
            "set_index": state.get("set_index"),
            "set_score": state.get("set_score"),
            "game_score": state.get("game_score"),
            "serving": state.get("serving"),
            "player1": state.get("player1"),
            "player2": state.get("player2"),
            "trigger_at": trigger_at,
            "score_change_at": score_change_at,
            "score_change_age_ms": (
                round((trigger_at - score_change_at) * 1000, 3)
                if score_change_at
                else None
            ),
            "feed_updated_at": feed_updated_at,
            "feed_age_ms": (
                round((trigger_at - float(feed_updated_at)) * 1000, 3)
                if isinstance(feed_updated_at, (int, float))
                else None
            ),
        }
        self._append("TRIGGER", event_id, data, dedupe_key="trigger")

    def record_market(
        self,
        *,
        event_id: str,
        market_id: str,
        question: str,
        outcomes: list,
        token_ids: list,
        lookup_started_at: float,
        market_ready_at: float,
        source: str,
    ) -> None:
        self._append(
            "MARKET",
            event_id,
            {
                "market_id": market_id,
                "question": question,
                "outcomes": outcomes,
                "token_ids": token_ids,
                "market_source": source,
                "market_lookup_ms": round(
                    (market_ready_at - lookup_started_at) * 1000, 3
                ),
            },
            dedupe_key="market",
        )

    def record_quote(
        self,
        *,
        event_id: str,
        quote_started_at: float,
        quote_finished_at: float,
        views: list[dict],
        selected: dict,
        other: dict,
        live_enabled: bool,
    ) -> None:
        self._append(
            "QUOTE",
            event_id,
            {
                "quote_started_at": quote_started_at,
                "quote_finished_at": quote_finished_at,
                "book_fetch_ms": round(
                    (quote_finished_at - quote_started_at) * 1000, 3
                ),
                "quotes": views,
                "selected": selected,
                "other": other,
                "live_enabled": live_enabled,
            },
            dedupe_key="quote",
        )

    def record_order_created(
        self,
        order: Any,
        *,
        request_started_at: float,
        response_at: float,
        source: str = "submit",
    ) -> None:
        intent = dict(getattr(order, "intent", {}) or {})
        event_id = str(intent.get("tie_event_id") or "")
        trigger_at = _float_or_none(intent.get("trigger_at"))
        self._append(
            "ORDER",
            event_id,
            {
                "source": source,
                "shadow_id": intent.get("shadow_id"),
                "game_score": intent.get("game_score"),
                "order_id": getattr(order, "order_id", ""),
                "token_id": getattr(order, "token_id", ""),
                "selected": getattr(order, "team", ""),
                "side": getattr(order, "side", ""),
                "status": getattr(order, "status", ""),
                "limit_price": getattr(order, "limit_price", None),
                "size": getattr(order, "size", None),
                "amount": getattr(order, "amount", None),
                "request_started_at": request_started_at,
                "response_at": response_at,
                "submit_rtt_ms": round((response_at - request_started_at) * 1000, 3),
                "trigger_to_request_ms": (
                    round((request_started_at - trigger_at) * 1000, 3)
                    if trigger_at
                    else None
                ),
                "request": _json_safe(getattr(order, "request", {})),
                "result": _json_safe(getattr(order, "result", {})),
            },
            dedupe_key=f"order:{getattr(order, 'order_id', '')}",
        )

    def record_order_update(
        self,
        order: Any,
        *,
        source: str,
        at: float | None = None,
    ) -> None:
        intent = dict(getattr(order, "intent", {}) or {})
        event_id = str(intent.get("tie_event_id") or "")
        if not event_id:
            return
        when = float(at or time.time())
        status = str(getattr(order, "status", "") or "")
        filled = round(float(getattr(order, "filled_size", 0) or 0), 6)
        average = round(float(getattr(order, "average_price", 0) or 0), 6)
        order_id = str(getattr(order, "order_id", "") or "")
        signature = (status, filled, average)
        if self._last_order_update.get(order_id) == signature:
            return
        self._last_order_update[order_id] = signature
        trigger_at = _float_or_none(intent.get("trigger_at"))
        score_change_at = _float_or_none(intent.get("score_change_at"))
        self._append(
            "ORDER_UPDATE",
            event_id,
            {
                "source": source,
                "shadow_id": intent.get("shadow_id"),
                "game_score": intent.get("game_score"),
                "order_id": order_id,
                "status": status,
                "filled_size": filled,
                "average_price": average,
                "updated_at": when,
                "trigger_to_update_ms": (
                    round((when - trigger_at) * 1000, 3) if trigger_at else None
                ),
                "score_change_to_update_ms": (
                    round((when - score_change_at) * 1000, 3)
                    if score_change_at
                    else None
                ),
            },
        )

    def record_error(
        self,
        *,
        event_id: str,
        phase: str,
        error: str,
        data: dict | None = None,
    ) -> None:
        self._append(
            "ERROR",
            event_id,
            {"phase": phase, "error": str(error), **(data or {})},
            dedupe_key=f"error:{phase}:{error}",
        )


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None
