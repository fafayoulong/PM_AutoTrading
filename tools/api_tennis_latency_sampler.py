# -*- coding: utf-8 -*-
"""Read-only API-Tennis point stream vs Polymarket price latency sampler.

API-Tennis WebSocket supplies point-by-point tennis state. For each event that
can be matched to a Polymarket moneyline market, this process polls the public
CLOB book for the first player's token and records price snapshots around the
arrival of every new point state.

It never imports the live trader and never submits an order.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.atomic_io import atomic_write_json, rotate_by_size
from src.config import Config, load_config
from src.polymarket_feed import PolymarketFeed


OUTPUT_PATH = Path("trades/api_tennis_latency_samples.jsonl")
STATUS_PATH = Path("trades/api_tennis_latency_status.json")
PLAYER_CACHE_PATH = Path("trades/api_tennis_players.json")
SHADOW_PATH = Path("trades/api_tennis_shadow.jsonl")
HF_PATH_PATH = Path("trades/api_tennis_hf_paths.jsonl")
OUTPUT_MAX_BYTES = 64 * 1024 * 1024
OUTPUT_BACKUPS = 10
HF_PATH_MAX_BYTES = 64 * 1024 * 1024
HF_PATH_BACKUPS = 10
API_BASE = "https://api.api-tennis.com/tennis/"
WS_URL = "wss://wss.api-tennis.com/live"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLL_SECONDS = 2.0
TOKEN_IDLE_SECONDS = 1800.0
HORIZONS = (1, 3, 5, 10, 15, 30, 60, 120)
HF_QUOTE_HISTORY = 5000
SHADOW_POLICY_TP = (0.02, 0.03, 0.05)
SHADOW_POLICY_SL = (0.02, 0.03, 0.05)
SHADOW_STAKE_USDC = max(
    0.01, float(os.getenv("APITENNIS_SHADOW_STAKE_USDC", "2.0") or 2.0)
)
SHADOW_EXIT_SECONDS = max(
    10.0, float(os.getenv("APITENNIS_SHADOW_EXIT_SECONDS", "180") or 180.0)
)
SHADOW_HORIZONS = (5, 15, 30, 60, 120, 180)
SHADOW_FEE_RATE = max(
    0.0, float(os.getenv("APITENNIS_SHADOW_FEE_RATE", "0.05") or 0.05)
)
SHADOW_ENABLED = str(
    os.getenv("APITENNIS_SHADOW_ENABLED", "1") or "1"
).strip().lower() not in {"0", "false", "no", "off"}
POINT_RANKS = {"0": 0, "15": 1, "30": 2, "40": 3, "A": 4, "AD": 4}


def _normalize(text: object) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    ascii_text = raw.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _name_tokens(text: object) -> set[str]:
    return {
        token
        for token in _normalize(text).split()
        if len(token) >= 3
    }


def outcome_index(outcomes: list[str], player: str) -> int:
    """Match abbreviated API-Tennis names against Polymarket full names."""
    wanted = _name_tokens(player)
    if not wanted:
        return -1
    scores: list[int] = []
    for outcome in outcomes:
        tokens = _name_tokens(outcome)
        scores.append(len(wanted & tokens))
    best = max(scores, default=0)
    if best <= 0 or scores.count(best) != 1:
        return -1
    return scores.index(best)


def latest_point_state(row: dict) -> dict:
    point_sets = row.get("pointbypoint") or []
    latest_set = point_sets[-1] if point_sets and isinstance(point_sets[-1], dict) else {}
    points = latest_set.get("points") or []
    latest_point = points[-1] if points and isinstance(points[-1], dict) else {}
    return {
        "set_number": latest_set.get("set_number"),
        "game_number": latest_set.get("number_game"),
        "score": latest_point.get("score"),
        "point_number": latest_point.get("number_point"),
        "break_point": latest_point.get("break_point"),
        "set_point": latest_point.get("set_point"),
        "match_point": latest_point.get("match_point"),
    }


def state_signature(row: dict) -> tuple:
    point = latest_point_state(row)
    return (
        str(row.get("event_final_result") or ""),
        str(row.get("event_game_result") or ""),
        str(row.get("event_serve") or ""),
        str(row.get("event_status") or ""),
        str(point.get("set_number") or ""),
        str(point.get("game_number") or ""),
        str(point.get("point_number") or ""),
        str(point.get("score") or ""),
        str(point.get("break_point") or ""),
        str(point.get("set_point") or ""),
        str(point.get("match_point") or ""),
    )


def append_row(row: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rotate_by_size(
        OUTPUT_PATH,
        max_bytes=OUTPUT_MAX_BYTES,
        backup_count=OUTPUT_BACKUPS,
    )
    with OUTPUT_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


def book_quote(book: dict) -> dict | None:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    try:
        bid = max(float(level["price"]) for level in bids if level.get("price"))
        ask = min(float(level["price"]) for level in asks if level.get("price"))
    except (TypeError, ValueError):
        return None
    if not (0.0 < bid <= ask < 1.0):
        return None
    return {
        "best_bid": bid,
        "best_ask": ask,
        "mid": (bid + ask) / 2.0,
    }


def book_mid(book: dict) -> float | None:
    quote = book_quote(book)
    return float(quote["mid"]) if quote else None


def _pair(text: object, parser) -> tuple[int, int] | None:
    parts = [part.strip() for part in str(text or "").split("-")]
    if len(parts) != 2:
        return None
    try:
        return parser(parts[0]), parser(parts[1])
    except (TypeError, ValueError):
        return None


def _point_rank(text: str) -> int:
    try:
        return POINT_RANKS[str(text).strip().upper()]
    except KeyError as exc:
        raise ValueError(text) from exc


def _rank_direction(
    previous: tuple[int, int], current: tuple[int, int]
) -> int:
    p1_prev, p2_prev = previous
    p1_cur, p2_cur = current
    if p1_cur > p1_prev and p2_cur == p2_prev:
        return 1
    if p2_cur > p2_prev and p1_cur == p1_prev:
        return -1
    # Advantage to deuce: the player without advantage won the point.
    if p1_prev > p2_prev and p1_cur == p2_cur:
        return -1
    if p2_prev > p1_prev and p1_cur == p2_cur:
        return 1
    # Deuce to advantage.
    if p1_prev == p2_prev and p1_cur > p2_cur:
        return 1
    if p1_prev == p2_prev and p2_cur > p1_cur:
        return -1
    return 0


def _game_wins(row: dict) -> tuple[int, int] | None:
    scores = row.get("scores") or []
    for item in reversed(scores):
        if not isinstance(item, dict):
            continue
        try:
            return int(item.get("score_first")), int(item.get("score_second"))
        except (TypeError, ValueError):
            continue
    return None


def scoring_side(previous: dict | None, current: dict) -> tuple[int, str]:
    """Return (+1 first player, -1 second player, event type)."""
    if previous is None:
        return 0, "initial"
    prev_set = _pair(previous.get("event_final_result"), int)
    cur_set = _pair(current.get("event_final_result"), int)
    if prev_set is not None and cur_set is not None and prev_set != cur_set:
        if cur_set[0] > prev_set[0] and cur_set[1] == prev_set[1]:
            return 1, "set"
        if cur_set[1] > prev_set[1] and cur_set[0] == prev_set[0]:
            return -1, "set"

    prev_point = latest_point_state(previous)
    cur_point = latest_point_state(current)
    if (
        prev_point.get("set_number") == cur_point.get("set_number")
        and prev_point.get("game_number") != cur_point.get("game_number")
    ):
        prev_games = _game_wins(previous)
        cur_games = _game_wins(current)
        if prev_games is not None and cur_games is not None:
            if cur_games[0] > prev_games[0] and cur_games[1] == prev_games[1]:
                return 1, "game"
            if cur_games[1] > prev_games[1] and cur_games[0] == prev_games[0]:
                return -1, "game"

    try:
        prev_number = int(prev_point.get("point_number") or 0)
        cur_number = int(cur_point.get("point_number") or 0)
    except (TypeError, ValueError):
        prev_number = cur_number = 0

    # `pointbypoint` is authoritative. `event_game_result` can transiently
    # disagree with it, so never turn that field correction into a new trade.
    prev_rank = _pair(prev_point.get("score"), _point_rank)
    cur_rank = _pair(cur_point.get("score"), _point_rank)
    if prev_rank is not None and cur_rank is not None:
        if cur_number < prev_number:
            return 0, "stale"
        if cur_number > prev_number:
            change = _rank_direction(prev_rank, cur_rank)
            return (change, "point") if change else (0, "unchanged")
        return 0, "duplicate"

    # Fallback only when the point-by-point score is unavailable and the
    # point number has advanced.
    if cur_number > prev_number:
        prev_game = _pair(previous.get("event_game_result"), _point_rank)
        cur_game = _pair(current.get("event_game_result"), _point_rank)
        if prev_game is not None and cur_game is not None:
            change = _rank_direction(prev_game, cur_game)
            return (change, "point") if change else (0, "unchanged")
    return 0, "unchanged"


class ClobMarketStream:
    """Event-driven public CLOB book observer for sub-second path measurements.

    The sampler keeps the existing REST poller for horizon snapshots, while this
    stream records every best bid/ask change received from Polymarket. It never
    authenticates and never submits orders.
    """

    def __init__(self, history_size: int = HF_QUOTE_HISTORY):
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._last_used: dict[str, float] = {}
        self.quotes: dict[str, deque[tuple[float, float, float, float]]] = (
            defaultdict(lambda: deque(maxlen=max(100, int(history_size))))
        )
        self.latest: dict[str, dict] = {}
        self.connected = False
        self.messages = 0
        self.last_message_at = 0.0
        self.last_error = ""

    async def start(self) -> None:
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=None),
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = None
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.connected = False

    def ensure(self, token_id: str) -> None:
        token_id = str(token_id or "")
        if not token_id:
            return
        self._last_used[token_id] = time.time()
        if token_id in self._wanted:
            return
        self._wanted.add(token_id)
        if self.connected and token_id not in self._subscribed:
            asyncio.create_task(self._subscribe([token_id]))

    async def _run(self) -> None:
        assert self._session is not None
        while not self._stop.is_set():
            try:
                async with self._session.ws_connect(
                    CLOB_MARKET_WS_URL,
                    heartbeat=None,
                    timeout=aiohttp.ClientWSTimeout(ws_close=10),
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = ""
                    self._subscribed.clear()
                    if self._wanted:
                        await self._subscribe(sorted(self._wanted), initial=True)
                    self._heartbeat = asyncio.create_task(self._heartbeat_loop())
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            text = str(message.data or "").strip()
                            if text.upper() == "PING":
                                await ws.send_str("PONG")
                                continue
                            self._handle_text(text)
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            finally:
                self.connected = False
                self._ws = None
                self._subscribed.clear()
                if self._heartbeat is not None:
                    heartbeat = self._heartbeat
                    self._heartbeat = None
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            if not self._stop.is_set():
                await asyncio.sleep(3.0)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set() and self._ws is not None:
            await asyncio.sleep(10.0)
            try:
                missing = sorted(self._wanted - self._subscribed)
                if missing:
                    await self._subscribe(missing)
                await self._ws.send_str("PING")
                removed = self._prune_idle()
                if removed:
                    await self._ws.send_str(
                        json.dumps(
                            {"assets_ids": removed, "operation": "unsubscribe"}
                        )
                    )
                    self._subscribed.difference_update(removed)
            except Exception:  # noqa: BLE001
                return

    async def _subscribe(self, token_ids: list[str], *, initial: bool = False) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        ids = [str(item) for item in token_ids if item]
        if not ids:
            return
        try:
            async with self._send_lock:
                payload = {"assets_ids": ids}
                payload["type" if initial else "operation"] = (
                    "market" if initial else "subscribe"
                )
                await ws.send_str(json.dumps(payload))
                self._subscribed.update(ids)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"subscribe: {type(exc).__name__}: {exc}"[:300]

    def _prune_idle(self) -> list[str]:
        cutoff = time.time() - TOKEN_IDLE_SECONDS
        stale = [token for token, when in self._last_used.items() if when < cutoff]
        for token in stale:
            self._last_used.pop(token, None)
            self._wanted.discard(token)
            self._subscribed.discard(token)
            self.quotes.pop(token, None)
            self.latest.pop(token, None)
        return stale

    def _handle_text(self, raw: str) -> None:
        text = str(raw or "").strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        self.messages += 1
        self.last_message_at = time.time()
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "")
            if event_type == "book":
                asset_id = str(event.get("asset_id") or "")
                bids = event.get("bids") or []
                asks = event.get("asks") or []
                try:
                    best_bid = max(float(item["price"]) for item in bids)
                    best_ask = min(float(item["price"]) for item in asks)
                except (KeyError, TypeError, ValueError):
                    continue
                self._record(asset_id, best_bid, best_ask)
            elif event_type == "price_change":
                for change in event.get("price_changes") or []:
                    if not isinstance(change, dict):
                        continue
                    try:
                        best_bid = float(change["best_bid"])
                        best_ask = float(change["best_ask"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    self._record(str(change.get("asset_id") or ""), best_bid, best_ask)

    def _record(self, token_id: str, best_bid: float, best_ask: float) -> None:
        if not token_id or not (0.0 < best_bid <= best_ask < 1.0):
            return
        when = time.time()
        mid = (best_bid + best_ask) / 2.0
        rows = self.quotes[token_id]
        if rows:
            _, prev_bid, prev_ask, _ = rows[-1]
            if abs(prev_bid - best_bid) < 1e-12 and abs(prev_ask - best_ask) < 1e-12:
                return
        rows.append((when, best_bid, best_ask, mid))
        self.latest[token_id] = {
            "t": when,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
        }

    def quotes_between(self, token_id: str, start: float, end: float) -> list[dict]:
        output = []
        for when, bid, ask, mid in self.quotes.get(str(token_id or "")) or []:
            if when < start or when > end:
                continue
            output.append(
                {
                    "t": when,
                    "best_bid": bid,
                    "best_ask": ask,
                    "mid": mid,
                }
            )
        return output

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "messages": self.messages,
            "last_message_at": self.last_message_at or None,
            "tracked_tokens": len(self._wanted),
            "subscribed_tokens": len(self._subscribed),
            "quoted_tokens": sum(bool(rows) for rows in self.quotes.values()),
            "last_error": self.last_error,
        }


class PricePoller:
    """One public CLOB book poller per first-player outcome token."""

    def __init__(self, interval: float = POLL_SECONDS):
        self.interval = max(1.0, float(interval))
        self.session: aiohttp.ClientSession | None = None
        self.history: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=600)
        )
        self.quotes: dict[str, deque[tuple[float, float, float, float]]] = (
            defaultdict(lambda: deque(maxlen=600))
        )
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_used: dict[str, float] = {}
        self.errors: dict[str, str] = {}

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=5),
            )

    async def stop(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        if self.session is not None:
            await self.session.close()
            self.session = None

    def ensure(self, token_id: str) -> None:
        token_id = str(token_id or "")
        if not token_id:
            return
        self.last_used[token_id] = time.time()
        if token_id in self.tasks:
            return
        self.tasks[token_id] = asyncio.create_task(self._run(token_id))

    async def _run(self, token_id: str) -> None:
        assert self.session is not None
        try:
            while time.time() - self.last_used.get(token_id, 0.0) < TOKEN_IDLE_SECONDS:
                try:
                    async with self.session.get(
                        CLOB_BOOK_URL, params={"token_id": token_id}
                    ) as response:
                        if response.status == 200:
                            quote = book_quote(await response.json())
                            if quote is not None:
                                when = time.time()
                                self.history[token_id].append(
                                    (when, float(quote["mid"]))
                                )
                                self.quotes[token_id].append(
                                    (
                                        when,
                                        float(quote["best_bid"]),
                                        float(quote["best_ask"]),
                                        float(quote["mid"]),
                                    )
                                )
                                self.errors.pop(token_id, None)
                        else:
                            self.errors[token_id] = f"HTTP {response.status}"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.errors[token_id] = f"{type(exc).__name__}"
                await asyncio.sleep(self.interval)
        finally:
            self.tasks.pop(token_id, None)

    async def fetch_quote(self, token_id: str) -> dict | None:
        token_id = str(token_id or "")
        if not token_id or self.session is None:
            return None
        try:
            async with self.session.get(
                CLOB_BOOK_URL, params={"token_id": token_id}
            ) as response:
                if response.status != 200:
                    self.errors[token_id] = f"HTTP {response.status}"
                    return None
                quote = book_quote(await response.json())
        except Exception as exc:  # noqa: BLE001
            self.errors[token_id] = type(exc).__name__
            return None
        if quote is None:
            return None
        when = time.time()
        self.history[token_id].append((when, float(quote["mid"])))
        self.quotes[token_id].append(
            (
                when,
                float(quote["best_bid"]),
                float(quote["best_ask"]),
                float(quote["mid"]),
            )
        )
        return {"t": when, **quote}

    def value_at(self, token_id: str, when: float) -> float | None:
        rows = self.history.get(token_id) or []
        if not rows:
            return None
        times = [row[0] for row in rows]
        index = bisect.bisect_right(times, when) - 1
        return rows[index][1] if index >= 0 else None

    def value_after(self, token_id: str, when: float) -> float | None:
        rows = self.history.get(token_id) or []
        if not rows:
            return None
        times = [row[0] for row in rows]
        index = bisect.bisect_left(times, when)
        return rows[index][1] if index < len(rows) else None

    def quote_after(self, token_id: str, when: float) -> dict | None:
        rows = self.quotes.get(token_id) or []
        if not rows:
            return None
        times = [row[0] for row in rows]
        index = bisect.bisect_left(times, when)
        if index >= len(rows):
            return None
        stamp, bid, ask, mid = rows[index]
        return {
            "t": stamp,
            "best_bid": bid,
            "best_ask": ask,
            "mid": mid,
        }

    def quote_at(self, token_id: str, when: float) -> dict | None:
        rows = self.quotes.get(token_id) or []
        if not rows:
            return None
        times = [row[0] for row in rows]
        index = bisect.bisect_right(times, when) - 1
        if index < 0:
            return None
        stamp, bid, ask, mid = rows[index]
        return {
            "t": stamp,
            "best_bid": bid,
            "best_ask": ask,
            "mid": mid,
        }


@dataclass
class Binding:
    found: bool
    checked_at: float
    question: str = ""
    outcomes: tuple[str, ...] = ()
    token_ids: tuple[str, ...] = ()
    first_index: int = -1
    second_index: int = -1
    first_full_name: str = ""
    second_full_name: str = ""


def analyze_high_frequency_path(
    entry: dict,
    quotes: list[dict],
    *,
    exit_seconds: float,
    fee_rate: float,
) -> dict:
    """Summarize one entry's high-frequency path and fixed TP/SL outcomes."""
    entry_time = float(entry.get("quote_t") or entry.get("t") or 0.0)
    end_time = entry_time + max(1.0, float(exit_seconds))
    points = sorted(
        (
            quote
            for quote in quotes
            if entry_time <= float(quote.get("t") or 0.0) <= end_time
            and quote.get("best_bid") is not None
            and quote.get("best_ask") is not None
        ),
        key=lambda quote: float(quote["t"]),
    )
    summary = {
        "quote_count": len(points),
        "path_seconds": (
            round(float(points[-1]["t"]) - entry_time, 3) if points else None
        ),
        "mfe_mid_c": None,
        "mfe_mid_at": None,
        "mae_mid_c": None,
        "mae_mid_at": None,
        "mfe_bid_c": None,
        "mfe_bid_at": None,
        "mae_bid_c": None,
        "mae_bid_at": None,
        "range_c": None,
        "policies": {},
    }
    if not points:
        return summary

    entry_ask = float(entry.get("entry_price") or 0.0)
    entry_mid = entry.get("entry_mid")
    try:
        entry_mid = float(entry_mid)
    except (TypeError, ValueError):
        entry_mid = (float(points[0]["best_bid"]) + float(points[0]["best_ask"])) / 2.0
    mids = [float(quote.get("mid", (float(quote["best_bid"]) + float(quote["best_ask"])) / 2.0)) for quote in points]
    bids = [float(quote["best_bid"]) for quote in points]
    mid_deltas = [(mid, quote) for mid, quote in zip(mids, points)]
    mfe_mid, mfe_mid_quote = max(mid_deltas, key=lambda item: item[0])
    mae_mid, mae_mid_quote = min(mid_deltas, key=lambda item: item[0])
    mfe_idx = max(range(len(bids)), key=lambda idx: bids[idx])
    mae_idx = min(range(len(bids)), key=lambda idx: bids[idx])
    summary.update(
        {
            "mfe_mid_c": round((mfe_mid - entry_mid) * 100.0, 4),
            "mfe_mid_at": round(float(mfe_mid_quote["t"]) - entry_time, 3),
            "mae_mid_c": round((mae_mid - entry_mid) * 100.0, 4),
            "mae_mid_at": round(float(mae_mid_quote["t"]) - entry_time, 3),
            "mfe_bid_c": round((bids[mfe_idx] - entry_ask) * 100.0, 4),
            "mfe_bid_at": round(float(points[mfe_idx]["t"]) - entry_time, 3),
            "mae_bid_c": round((bids[mae_idx] - entry_ask) * 100.0, 4),
            "mae_bid_at": round(float(points[mae_idx]["t"]) - entry_time, 3),
            "range_c": round((max(mids) - min(mids)) * 100.0, 4),
        }
    )

    stake = float(entry.get("stake_usdc") or 0.0)
    shares = float(entry.get("shares") or 0.0)
    entry_fee = float(entry.get("entry_fee_usdc") or 0.0)
    cost = stake + entry_fee
    if not cost or not shares or not entry_ask:
        return summary

    for take_profit in SHADOW_POLICY_TP:
        for stop_loss in SHADOW_POLICY_SL:
            chosen = None
            reason = "TIME"
            for quote in points:
                bid = float(quote["best_bid"])
                if bid <= entry_ask - stop_loss - 1e-12:
                    chosen = quote
                    reason = "STOP"
                    break
                if bid >= entry_ask + take_profit - 1e-12:
                    chosen = quote
                    reason = "TAKE_PROFIT"
                    break
            if chosen is None:
                chosen = points[-1]
            exit_price = float(chosen["best_bid"])
            exit_fee = shares * fee_rate * exit_price * (1.0 - exit_price)
            pnl = shares * exit_price - stake - entry_fee - exit_fee
            key = f"tp{int(round(take_profit * 100))}_sl{int(round(stop_loss * 100))}"
            summary["policies"][key] = {
                "take_profit_c": round(take_profit * 100.0, 2),
                "stop_loss_c": round(stop_loss * 100.0, 2),
                "exit_reason": reason,
                "hold_seconds": round(float(chosen["t"]) - entry_time, 3),
                "exit_price": round(exit_price, 6),
                "exit_fee_usdc": round(exit_fee, 6),
                "pnl": round(pnl, 6),
                "roi": round(pnl / cost, 6) if cost else None,
            }
    return summary


class ApiTennisShadowPortfolio:
    """Virtual two-dollar buy of the player who just won the point/game/set."""

    def __init__(
        self,
        prices: PricePoller,
        *,
        high_frequency: ClobMarketStream | None = None,
        stake: float = SHADOW_STAKE_USDC,
        exit_seconds: float = SHADOW_EXIT_SECONDS,
        fee_rate: float = SHADOW_FEE_RATE,
    ):
        self.prices = prices
        self.high_frequency = high_frequency
        self.enabled = SHADOW_ENABLED
        self.stake = max(0.01, float(stake))
        self.exit_seconds = max(10.0, float(exit_seconds))
        self.fee_rate = max(0.0, float(fee_rate))
        self.entries: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.last_error = ""
        self.last_error_at = 0.0
        self._load()

    def _load(self) -> None:
        if not SHADOW_PATH.exists():
            return
        with SHADOW_PATH.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry_id = str(row.get("entry_id") or "")
                if not entry_id:
                    continue
                if row.get("kind") == "entry":
                    self.entries[entry_id] = row
                elif row.get("kind") == "settlement":
                    self.settlements[entry_id] = row

    def _append(self, row: dict) -> None:
        SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        rotate_by_size(
            SHADOW_PATH,
            max_bytes=OUTPUT_MAX_BYTES,
            backup_count=OUTPUT_BACKUPS,
        )
        with SHADOW_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _append_hf_path(self, row: dict) -> None:
        HF_PATH_PATH.parent.mkdir(parents=True, exist_ok=True)
        rotate_by_size(
            HF_PATH_PATH,
            max_bytes=HF_PATH_MAX_BYTES,
            backup_count=HF_PATH_BACKUPS,
        )
        with HF_PATH_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def start(self) -> None:
        now = time.time()
        for entry in self.entries.values():
            if str(entry.get("entry_id")) in self.settlements:
                continue
            remaining = max(
                0.0,
                float(entry.get("t") or now) + self.exit_seconds - now,
            )
            self._schedule(entry, remaining)

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def observe(
        self,
        *,
        previous: dict,
        current: dict,
        binding: Binding,
        received_at: float,
        event_uid: str,
    ) -> dict | None:
        side, event_type = scoring_side(previous, current)
        if not self.enabled or not side:
            return None
        entry_id = f"APTS_{event_uid}"
        if entry_id in self.entries:
            return None
        index = binding.first_index if side > 0 else binding.second_index
        if not (0 <= index < len(binding.token_ids)):
            return None
        token_id = binding.token_ids[index]
        selected = binding.outcomes[index]
        point = latest_point_state(current)
        set_score = str(current.get("event_final_result") or "").replace(" ", "")
        point_score = str(point.get("score") or "").replace(" ", "").upper()
        serve = str(current.get("event_serve") or "")
        scorer_serving = (
            (side > 0 and serve == "First Player")
            or (side < 0 and serve == "Second Player")
        )
        self.prices.ensure(token_id)
        if self.high_frequency is not None:
            self.high_frequency.ensure(token_id)
        quote = await self.prices.fetch_quote(token_id)
        if quote is None:
            self.last_error = f"NO_QUOTE:{selected}"
            self.last_error_at = time.time()
            return None
        ask = float(quote["best_ask"])
        shares = self.stake / ask
        fee = self.stake * self.fee_rate * (1.0 - ask)
        entry = {
            "kind": "entry",
            "entry_id": entry_id,
            "t": received_at,
            "beijing_time": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(received_at)
            ),
            "event_key": current.get("event_key"),
            "event_type": event_type,
            "question": binding.question,
            "first_player": binding.first_full_name,
            "second_player": binding.second_full_name,
            "selected": selected,
            "token_id": token_id,
            "set_score": current.get("event_final_result"),
            "game_score": current.get("event_game_result"),
            "point_score": latest_point_state(current).get("score"),
            "serve": current.get("event_serve"),
            "context": {
                "deciding_set": set_score in {"1-1", "2-2"},
                "break_point": _flag(point.get("break_point")),
                "deuce_advantage": "A" in point_score or point_score == "40-40",
                "scorer_serving": scorer_serving,
            },
            "stake_usdc": round(self.stake, 6),
            "entry_price": round(ask, 6),
            "entry_mid": round(float(quote.get("mid") or 0.0), 6),
            "quote_t": round(float(quote.get("t") or received_at), 6),
            "shares": round(shares, 6),
            "entry_fee_usdc": round(fee, 6),
            "exit_horizon_seconds": self.exit_seconds,
            "status": "OPEN",
        }
        self._append(entry)
        self.entries[entry_id] = entry
        self.last_error = ""
        self.last_error_at = 0.0
        self._schedule(entry, self.exit_seconds)
        return entry

    def _schedule(self, entry: dict, delay: float) -> None:
        task = asyncio.create_task(self._settle_after(entry, delay))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _settle_after(self, entry: dict, delay: float) -> None:
        entry_id = str(entry.get("entry_id") or "")
        if not entry_id or entry_id in self.settlements:
            return
        if delay > 0:
            await asyncio.sleep(delay)
        token_id = str(entry.get("token_id") or "")
        entry_time = float(entry.get("t") or 0.0)
        stake = float(entry.get("stake_usdc") or 0.0)
        shares = float(entry.get("shares") or 0.0)
        fee = float(entry.get("entry_fee_usdc") or 0.0)
        cost = stake + fee
        virtual_exits: dict[str, dict] = {}
        horizons = tuple(
            sorted(set(SHADOW_HORIZONS + (int(round(self.exit_seconds)),)))
        )
        for horizon in horizons:
            target = entry_time + horizon
            quote = self.prices.quote_at(token_id, target)
            if quote is None:
                quote = self.prices.quote_after(token_id, target)
            if quote is None:
                continue
            age_seconds = float(quote["t"]) - target
            if age_seconds > 5.0:
                # The service did not have quote history covering this horizon
                # (usually a position loaded across a restart). Do not label a
                # much later quote as the 5/15/... second exit.
                continue
            exit_price = float(quote["best_bid"])
            pnl = shares * exit_price - stake - fee
            virtual_exits[str(horizon)] = {
                "exit_price": round(exit_price, 6),
                "quote_t": quote["t"],
                "age_seconds": round(age_seconds, 3),
                "pnl": round(pnl, 6),
                "roi": round(pnl / cost, 6) if cost > 0 else None,
            }
        actual = virtual_exits.get(str(int(self.exit_seconds)))
        if actual is None:
            self.last_error = f"NO_EXIT_QUOTE:{entry.get('selected')}"
            self.last_error_at = time.time()
            return
        exit_price = float(actual["exit_price"])
        pnl = shares * exit_price - stake - fee
        path_metrics = None
        hf_quotes: list[dict] = []
        if self.high_frequency is not None:
            start = float(entry.get("quote_t") or entry_time)
            hf_quotes = self.high_frequency.quotes_between(
                token_id, start, start + self.exit_seconds
            )
            path_metrics = analyze_high_frequency_path(
                entry,
                hf_quotes,
                exit_seconds=self.exit_seconds,
                fee_rate=self.fee_rate,
            )
        settlement = {
            "kind": "settlement",
            "entry_id": entry_id,
            "t": time.time(),
            "exit_price": round(exit_price, 6),
            "pnl": round(pnl, 6),
            "roi": round(pnl / cost, 6) if cost > 0 else None,
            "virtual_exits": virtual_exits,
            "path_metrics": path_metrics,
            "status": "CLOSED",
        }
        self._append(settlement)
        self.settlements[entry_id] = settlement
        entry["status"] = "CLOSED"
        if hf_quotes:
            try:
                self._append_hf_path(
                    {
                        "kind": "path",
                        "entry_id": entry_id,
                        "t": entry_time,
                        "token_id": token_id,
                        "question": entry.get("question"),
                        "event_type": entry.get("event_type"),
                        "set_score": entry.get("set_score"),
                        "game_score": entry.get("game_score"),
                        "point_score": entry.get("point_score"),
                        "context": entry.get("context") or {},
                        "entry_price": entry.get("entry_price"),
                        "entry_mid": entry.get("entry_mid"),
                        "path_metrics": path_metrics,
                        "quotes": hf_quotes,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"HF_PATH_WRITE:{type(exc).__name__}: {exc}"
                self.last_error_at = time.time()

    def _summary(self, rows: list[dict]) -> dict:
        settled = [
            self.settlements[str(row.get("entry_id"))]
            for row in rows
            if str(row.get("entry_id")) in self.settlements
        ]
        wins = sum(float(row.get("pnl") or 0.0) > 0 for row in settled)
        cost = sum(
            float(self.entries[str(row["entry_id"])].get("stake_usdc") or 0.0)
            + float(self.entries[str(row["entry_id"])].get("entry_fee_usdc") or 0.0)
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
        if self.last_error and self.last_error_at and time.time() - self.last_error_at > 300:
            self.last_error = ""
            self.last_error_at = 0.0
        by_event_type = {
            event_type: self._summary(
                [
                    row
                    for row in self.entries.values()
                    if row.get("event_type") == event_type
                ]
            )
            for event_type in ("point", "game", "set")
        }
        by_context: dict[str, dict] = {}
        context_predicates = {
            "deciding_set": lambda row: bool(
                (row.get("context") or {}).get("deciding_set")
            ),
            "non_deciding_set": lambda row: (
                "context" in row
                and not bool((row.get("context") or {}).get("deciding_set"))
            ),
            "break_point": lambda row: bool(
                (row.get("context") or {}).get("break_point")
            ),
            "deuce_advantage": lambda row: bool(
                (row.get("context") or {}).get("deuce_advantage")
            ),
            "scorer_serving": lambda row: bool(
                (row.get("context") or {}).get("scorer_serving")
            ),
            "scorer_receiving": lambda row: (
                "scorer_serving" in (row.get("context") or {})
                and not bool((row.get("context") or {}).get("scorer_serving"))
            ),
        }
        for label, predicate in context_predicates.items():
            by_context[label] = self._summary(
                [row for row in self.entries.values() if predicate(row)]
            )
        horizon_summary: dict[str, dict] = {}
        for horizon in SHADOW_HORIZONS:
            values: list[float] = []
            costs: list[float] = []
            for entry_id, settlement in self.settlements.items():
                exit_row = (settlement.get("virtual_exits") or {}).get(str(horizon))
                entry = self.entries.get(entry_id)
                if not exit_row or entry is None:
                    continue
                try:
                    if float(exit_row.get("age_seconds")) > 5.0:
                        continue
                except (TypeError, ValueError):
                    continue
                values.append(float(exit_row.get("pnl") or 0.0))
                costs.append(
                    float(entry.get("stake_usdc") or 0.0)
                    + float(entry.get("entry_fee_usdc") or 0.0)
                )
            wins = sum(value > 0 for value in values)
            pnl = sum(values)
            cost = sum(costs)
            horizon_summary[str(horizon)] = {
                "n": len(values),
                "wins": wins,
                "losses": len(values) - wins,
                "win_rate": round(wins / len(values), 6) if values else None,
                "pnl_usdc": round(pnl, 6),
                "roi": round(pnl / cost, 6) if cost > 0 else None,
            }
        path_rows = [
            settlement.get("path_metrics") or {}
            for settlement in self.settlements.values()
        ]
        path_rows = [row for row in path_rows if row.get("quote_count")]
        path_summary = {"n": len(path_rows)}
        for field in (
            "quote_count",
            "path_seconds",
            "mfe_mid_c",
            "mae_mid_c",
            "mfe_bid_c",
            "mae_bid_c",
            "range_c",
        ):
            values = [
                float(row[field])
                for row in path_rows
                if row.get(field) is not None
            ]
            path_summary[f"median_{field}"] = (
                round(statistics.median(values), 4) if values else None
            )
        path_summary["mfe_mid_5c_rate"] = (
            round(
                sum(float(row.get("mfe_mid_c") or 0.0) >= 5.0 for row in path_rows)
                / len(path_rows),
                6,
            )
            if path_rows
            else None
        )
        path_summary["mae_mid_5c_rate"] = (
            round(
                sum(float(row.get("mae_mid_c") or 0.0) <= -5.0 for row in path_rows)
                / len(path_rows),
                6,
            )
            if path_rows
            else None
        )
        policy_summary: dict[str, dict] = {}
        for take_profit in SHADOW_POLICY_TP:
            for stop_loss in SHADOW_POLICY_SL:
                key = f"tp{int(round(take_profit * 100))}_sl{int(round(stop_loss * 100))}"
                values: list[float] = []
                costs: list[float] = []
                reasons: dict[str, int] = defaultdict(int)
                for entry_id, settlement in self.settlements.items():
                    row = (
                        (settlement.get("path_metrics") or {})
                        .get("policies", {})
                        .get(key)
                    )
                    entry = self.entries.get(entry_id)
                    if row is None or entry is None:
                        continue
                    values.append(float(row.get("pnl") or 0.0))
                    reasons[str(row.get("exit_reason") or "UNKNOWN")] += 1
                    costs.append(
                        float(entry.get("stake_usdc") or 0.0)
                        + float(entry.get("entry_fee_usdc") or 0.0)
                    )
                policy_summary[key] = {
                    "count": len(values),
                    "wins": sum(value > 0 for value in values),
                    "take_profit": reasons.get("TAKE_PROFIT", 0),
                    "stop_loss": reasons.get("STOP", 0),
                    "time_exit": reasons.get("TIME", 0),
                    "pnl_usdc": round(sum(values), 6),
                    "roi": (
                        round(sum(values) / sum(costs), 6)
                        if costs and sum(costs) > 0
                        else None
                    ),
                }
        recent: list[dict] = []
        for entry in sorted(
            self.entries.values(),
            key=lambda row: float(row.get("t") or 0),
            reverse=True,
        )[:30]:
            settlement = self.settlements.get(str(entry.get("entry_id") or ""))
            recent.append(
                {
                    "t": entry.get("t"),
                    "event_type": entry.get("event_type"),
                    "question": entry.get("question"),
                    "selected": entry.get("selected"),
                    "set_score": entry.get("set_score"),
                    "game_score": entry.get("game_score"),
                    "point_score": entry.get("point_score"),
                    "entry_price": entry.get("entry_price"),
                    "shares": entry.get("shares"),
                    "status": "CLOSED" if settlement else "OPEN",
                    "exit_price": settlement.get("exit_price") if settlement else None,
                    "pnl": settlement.get("pnl") if settlement else None,
                    "roi": settlement.get("roi") if settlement else None,
                    "mfe_mid_c": (
                        (settlement.get("path_metrics") or {}).get("mfe_mid_c")
                        if settlement
                        else None
                    ),
                    "mae_mid_c": (
                        (settlement.get("path_metrics") or {}).get("mae_mid_c")
                        if settlement
                        else None
                    ),
                    "hf_quotes": (
                        (settlement.get("path_metrics") or {}).get("quote_count")
                        if settlement
                        else None
                    ),
                }
            )
        return {
            "enabled": self.enabled,
            "stake_usdc": self.stake,
            "exit_horizon_seconds": self.exit_seconds,
            "fee_rate": self.fee_rate,
            "hf_path": str(HF_PATH_PATH),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at or None,
            **self._summary(list(self.entries.values())),
            "by_event_type": by_event_type,
            "by_context": by_context,
            "horizon_summary": horizon_summary,
            "path_summary": path_summary,
            "policy_summary": policy_summary,
            "recent": recent,
        }


class MarketResolver:
    def __init__(self, config: Config, api_key: str):
        self.config = config
        self.api_key = api_key
        self.pm = PolymarketFeed(config)
        self.cache: dict[str, Binding] = {}
        self.inflight: dict[str, asyncio.Task] = {}
        self.session: aiohttp.ClientSession | None = None
        self.player_names: dict[str, str] = {}
        self._load_player_cache()

    def _load_player_cache(self) -> None:
        try:
            payload = json.loads(PLAYER_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.player_names = {
                    str(key): str(value)
                    for key, value in payload.items()
                    if value
                }
        except Exception:  # noqa: BLE001
            self.player_names = {}

    async def start(self) -> None:
        await self.pm.start()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=15),
        )

    async def stop(self) -> None:
        for task in list(self.inflight.values()):
            task.cancel()
        if self.inflight:
            await asyncio.gather(*self.inflight.values(), return_exceptions=True)
        if self.session is not None:
            await self.session.close()
            self.session = None
        await self.pm.stop()

    async def _full_name(self, player_key: object, fallback: str) -> str:
        key = str(player_key or "")
        if not key:
            return fallback
        cached = self.player_names.get(key)
        if cached:
            return cached
        if self.session is None:
            return fallback
        try:
            async with self.session.get(
                API_BASE,
                params={
                    "method": "get_players",
                    "player_key": key,
                    "APIkey": self.api_key,
                },
            ) as response:
                if response.status != 200:
                    return fallback
                payload = await response.json()
            rows = payload.get("result") or []
            if not isinstance(rows, list) or not rows:
                return fallback
            full = str(rows[0].get("player_full_name") or "").strip()
            if full:
                self.player_names[key] = full
                atomic_write_json(PLAYER_CACHE_PATH, self.player_names)
                return full
        except Exception:  # noqa: BLE001
            pass
        return fallback

    async def resolve(self, row: dict) -> Binding | None:
        event_key = str(row.get("event_key") or "")
        if not event_key:
            return None
        cached = self.cache.get(event_key)
        if cached and time.time() - cached.checked_at < (
            1800 if cached.found else 120
        ):
            return cached if cached.found else None
        task = self.inflight.get(event_key)
        if task is not None:
            return await task
        task = asyncio.create_task(self._resolve(row))
        self.inflight[event_key] = task
        try:
            binding = await task
        finally:
            self.inflight.pop(event_key, None)
        return binding if binding and binding.found else None

    async def _resolve(self, row: dict) -> Binding:
        event_key = str(row.get("event_key") or "")
        first = str(row.get("event_first_player") or "")
        second = str(row.get("event_second_player") or "")
        binding = Binding(False, time.time())
        if "/" in first or "/" in second:
            # Doubles names are too ambiguous for the first sampler version.
            self.cache[event_key] = binding
            return binding
        first_full = await self._full_name(row.get("first_player_key"), first)
        second_full = await self._full_name(row.get("second_player_key"), second)
        market = await self.pm.search_sports_markets(
            "tennis", first_full, second_full
        )
        if market is None or len(market.outcomes) != 2 or len(market.token_ids) != 2:
            self.cache[event_key] = binding
            return binding
        first_index = outcome_index(list(market.outcomes), first_full or first)
        second_index = outcome_index(list(market.outcomes), second_full or second)
        if first_index < 0 or second_index < 0 or first_index == second_index:
            self.cache[event_key] = binding
            return binding
        binding = Binding(
            True,
            time.time(),
            question=str(market.question),
            outcomes=tuple(str(item) for item in market.outcomes),
            token_ids=tuple(str(item) for item in market.token_ids),
            first_index=first_index,
            second_index=second_index,
            first_full_name=first_full,
            second_full_name=second_full,
        )
        self.cache[event_key] = binding
        return binding


class Tracker:
    def __init__(self, key: str, config: Config):
        self.key = key
        self.config = config
        self.prices = PricePoller()
        self.high_frequency = ClobMarketStream()
        self.shadow = ApiTennisShadowPortfolio(
            self.prices, high_frequency=self.high_frequency
        )
        self.resolver = MarketResolver(config, key)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self.last_states: dict[str, tuple] = {}
        self.last_rows: dict[str, dict] = {}
        self.finalizers: set[asyncio.Task] = set()
        self.worker: asyncio.Task | None = None
        self.ws_connected = False
        self.last_ws_at = 0.0
        self.last_state_at = 0.0
        self.messages = 0
        self.point_states = 0
        self.matched_states = 0
        self.unmatched_states = 0
        self.ws_point_states = 0
        self.rest_point_states = 0
        self.last_source = ""
        self.api_events: dict[str, dict] = {}
        self.status_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.prices.start()
        await self.high_frequency.start()
        await self.shadow.start()
        await self.resolver.start()
        self.worker = asyncio.create_task(self._work())
        self.status_task = asyncio.create_task(self._status_loop())

    async def stop(self) -> None:
        for task in list(self.finalizers):
            task.cancel()
        if self.finalizers:
            await asyncio.gather(*self.finalizers, return_exceptions=True)
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if self.status_task:
            self.status_task.cancel()
            try:
                await self.status_task
            except asyncio.CancelledError:
                pass
        await self.resolver.stop()
        await self.shadow.stop()
        await self.high_frequency.stop()
        await self.prices.stop()

    def submit(
        self,
        row: dict,
        received_at: float | None = None,
        source: str = "ws",
    ) -> None:
        self.messages += 1
        self.last_ws_at = time.time()
        try:
            self.queue.put_nowait(
                (dict(row), float(received_at or time.time()), str(source))
            )
        except asyncio.QueueFull:
            pass

    async def _work(self) -> None:
        while True:
            row, received_at, source = await self.queue.get()
            try:
                await self._handle(row, received_at, source)
            except Exception as exc:  # noqa: BLE001
                append_row(
                    {
                        "kind": "error",
                        "t": time.time(),
                        "error": f"{type(exc).__name__}: {exc}",
                        "event_key": row.get("event_key"),
                    }
                )
            finally:
                self.queue.task_done()

    async def _handle(self, row: dict, received_at: float, source: str) -> None:
        event_key = str(row.get("event_key") or "")
        state = state_signature(row)
        if not event_key or self.last_states.get(event_key) == state:
            return
        previous = self.last_rows.get(event_key)
        self.last_states[event_key] = state
        self.last_rows[event_key] = dict(row)
        self.point_states += 1
        if source == "ws":
            self.ws_point_states += 1
        elif source == "rest":
            self.rest_point_states += 1
        self.last_source = source
        self.last_state_at = received_at
        summary = {
            "event_key": event_key,
            "first_player": row.get("event_first_player"),
            "second_player": row.get("event_second_player"),
            "first_player_key": row.get("first_player_key"),
            "second_player_key": row.get("second_player_key"),
            "tournament": row.get("tournament_name"),
            "status": row.get("event_status"),
            "final_result": row.get("event_final_result"),
            "game_result": row.get("event_game_result"),
            "last_state_at": received_at,
            "matched": False,
            "source": source,
        }
        self.api_events[event_key] = summary
        binding = await self.resolver.resolve(row)
        if binding is None:
            self.unmatched_states += 1
            return
        self.matched_states += 1
        summary["matched"] = True
        summary["market_question"] = binding.question
        summary["first_full_name"] = binding.first_full_name
        summary["second_full_name"] = binding.second_full_name
        first_token = binding.token_ids[binding.first_index]
        second_token = binding.token_ids[binding.second_index]
        self.prices.ensure(first_token)
        self.prices.ensure(second_token)
        self.high_frequency.ensure(first_token)
        self.high_frequency.ensure(second_token)
        point = latest_point_state(row)
        state_text = "|".join(state)
        event_uid = (
            f"APT_{event_key}_"
            f"{hashlib.sha1(state_text.encode()).hexdigest()[:12]}"
        )
        event = {
            "kind": "point",
            "event_uid": event_uid,
            "t": received_at,
            "beijing_time": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(received_at)
            ),
            "event_key": event_key,
            "source": source,
            "first_player": binding.first_full_name or row.get("event_first_player"),
            "second_player": binding.second_full_name or row.get("event_second_player"),
            "api_first_player": row.get("event_first_player"),
            "api_second_player": row.get("event_second_player"),
            "tournament": row.get("tournament_name"),
            "event_type": row.get("event_type_type"),
            "status": row.get("event_status"),
            "final_result": row.get("event_final_result"),
            "game_result": row.get("event_game_result"),
            "serve": row.get("event_serve"),
            **point,
            "question": binding.question,
            "first_outcome": binding.outcomes[binding.first_index],
            "first_token_id": first_token,
            "price_pre10": self.prices.value_at(first_token, received_at - 10),
            "price_pre5": self.prices.value_at(first_token, received_at - 5),
            "price_at": self.prices.value_at(first_token, received_at),
        }
        if previous is not None and source == "ws":
            await self.shadow.observe(
                previous=previous,
                current=row,
                binding=binding,
                received_at=received_at,
                event_uid=event_uid,
            )
        append_row(event)
        task = asyncio.create_task(self._finalize(event))
        self.finalizers.add(task)
        task.add_done_callback(self.finalizers.discard)

    def _write_status(self) -> None:
        atomic_write_json(
            STATUS_PATH,
            {
                "t": time.time(),
                "pid": os.getpid(),
                "ws_connected": self.ws_connected,
                "last_ws_at": self.last_ws_at,
                "last_state_at": self.last_state_at,
                "messages": self.messages,
                "point_states": self.point_states,
                "matched_states": self.matched_states,
                "unmatched_states": self.unmatched_states,
                "ws_point_states": self.ws_point_states,
                "rest_point_states": self.rest_point_states,
                "last_source": self.last_source,
                "polled_tokens": len(self.prices.tasks),
                "resolved_markets": sum(
                    1 for binding in self.resolver.cache.values() if binding.found
                ),
                "api_events": sorted(
                    self.api_events.values(),
                    key=lambda row: float(row.get("last_state_at") or 0),
                    reverse=True,
                )[:30],
                "price_errors": dict(self.prices.errors),
                "book_stream": self.high_frequency.status(),
                "shadow": self.shadow.status(),
            },
        )

    async def _status_loop(self) -> None:
        while True:
            self._write_status()
            await asyncio.sleep(10)

    async def _finalize(self, event: dict) -> None:
        await asyncio.sleep(max(HORIZONS) + 3)
        token_id = event["first_token_id"]
        when = float(event["t"])
        prices = {
            f"p{horizon}": self.prices.value_after(token_id, when + horizon)
            for horizon in HORIZONS
        }
        append_row(
            {
                "kind": "result",
                "event_uid": event["event_uid"],
                "t": time.time(),
                "event_key": event["event_key"],
                "first_player": event["first_player"],
                "second_player": event["second_player"],
                "question": event["question"],
                "status": event["status"],
                "final_result": event["final_result"],
                "game_result": event["game_result"],
                "serve": event["serve"],
                "score": event.get("score"),
                "break_point": event.get("break_point"),
                "set_point": event.get("set_point"),
                "match_point": event.get("match_point"),
                "price_pre10": event.get("price_pre10"),
                "price_pre5": event.get("price_pre5"),
                "price_at": event.get("price_at"),
                **prices,
            }
        )


async def api_ws_loop(tracker: Tracker, key: str) -> None:
    query = urlencode({"APIkey": key, "timezone": "Asia/Shanghai"})
    url = f"{WS_URL}?{query}"
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                async with session.ws_connect(url, heartbeat=20) as ws:
                    tracker.ws_connected = True
                    tracker.last_ws_at = time.time()
                    tracker._write_status()
                    async for message in ws:
                        received_at = time.time()
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(message.data)
                        except json.JSONDecodeError:
                            continue
                        rows = payload if isinstance(payload, list) else [payload]
                        for row in rows:
                            if isinstance(row, dict):
                                tracker.submit(row, received_at, source="ws")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                tracker.ws_connected = False
                tracker._write_status()
                append_row(
                    {
                        "kind": "connection_error",
                        "t": time.time(),
                        "source": "api_tennis_ws",
                        "error": type(exc).__name__,
                    }
                )
                await asyncio.sleep(5)


async def api_rest_refresh(tracker: Tracker, key: str) -> None:
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        while True:
            try:
                async with session.get(
                    API_BASE,
                    params={"method": "get_livescore", "APIkey": key},
                ) as response:
                    received_at = time.time()
                    if response.status == 200:
                        payload = await response.json()
                        rows = payload.get("result") or []
                        for row in rows:
                            if isinstance(row, dict):
                                tracker.submit(row, received_at, source="rest")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)


def report() -> int:
    rows: list[dict] = []
    for path in sorted(OUTPUT_PATH.parent.glob(OUTPUT_PATH.name + "*")):
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") == "result":
                    rows.append(row)
    print(f"API-Tennis 可测事件：{len(rows)} 条")
    if not rows:
        return 0
    pre = [
        abs(float(row["price_at"]) - float(row["price_pre5"]))
        for row in rows
        if row.get("price_at") is not None and row.get("price_pre5") is not None
    ]
    if pre:
        print(
            "API-Tennis收到事件前5秒盘口绝对变化中位：{:.4f}，"
            ">0.01占{:.0%}（n={}）".format(
                statistics.median(pre),
                sum(value > 0.01 for value in pre) / len(pre),
                len(pre),
            )
        )
    for horizon in HORIZONS:
        moves = [
            abs(float(row[f"p{horizon}"]) - float(row["price_at"]))
            for row in rows
            if row.get("price_at") is not None and row.get(f"p{horizon}") is not None
        ]
        if moves:
            print(
                "收到后+{}秒绝对价格变化中位 {:.4f}，>0.01占{:.0%}（n={}）".format(
                    horizon,
                    statistics.median(moves),
                    sum(value > 0.01 for value in moves) / len(moves),
                    len(moves),
                )
            )
    return 0


async def run(key: str, seconds: float = 0.0) -> int:
    tracker = Tracker(key, load_config())
    await tracker.start()
    tasks = [
        asyncio.create_task(api_ws_loop(tracker, key)),
        asyncio.create_task(api_rest_refresh(tracker, key)),
    ]
    try:
        if seconds > 0:
            await asyncio.sleep(seconds)
        else:
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await tracker.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--seconds", type=float, default=0.0)
    args = parser.parse_args()
    if args.report:
        return report()
    key = (os.getenv("APITENNIS_KEY") or "").strip()
    if not key:
        print("缺少 APITENNIS_KEY")
        return 2
    return asyncio.run(run(key, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
