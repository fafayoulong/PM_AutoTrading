# -*- coding: utf-8 -*-
"""Polymarket sports live-score WebSocket feed.

服务端在连接建立后主动推送所有运动的实时比分，无需订阅。
主要用途是补充 ESPN 不覆盖的电竞（CS2 / LoL / Valorant）。
"""

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)


class PolymarketSportsFeed:
    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        # 事件驱动：每条更新入队，由单独的消费者顺序处理，避免阻塞 WS 读取
        self.on_game_update: Optional[Callable[[dict], Awaitable[None]]] = None
        self._update_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._handler_tasks: set[asyncio.Task] = set()
        self.max_concurrent_handlers = 8
        self.games: dict[str, dict] = {}
        self.connected = False
        self.last_message_at: Optional[float] = None
        self.message_count = 0
        self.leagues_seen: dict[str, int] = {}
        self.consecutive_failures = 0
        self.last_error = ""
        self.last_error_at: Optional[float] = None

    # ---------- lifecycle ----------

    async def start(self):
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession(proxy=self.config.proxy_url)
        self._stop_event.clear()
        self._consumer_task = asyncio.create_task(self._consume_updates())
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._stop_event.set()
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.connected = False

    # ---------- internals ----------

    async def _run_loop(self):
        url = getattr(self.config, "polymarket_sports_ws_url", "")
        if not url:
            return
        while not self._stop_event.is_set():
            try:
                await self._connect_once(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}".strip(": ")
                self.last_error_at = time.time()
                logger.warning("Polymarket sports feed error: %s", self.last_error)
            if self._stop_event.is_set():
                break
            await asyncio.sleep(5)

    async def _consume_updates(self):
        """消费比赛更新。允许有限并发，避免一场比赛的联网耗时拖住其他比赛。"""
        while not self._stop_event.is_set():
            try:
                game = await asyncio.wait_for(self._update_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            handler = self.on_game_update
            if handler is None:
                continue
            if len(self._handler_tasks) >= self.max_concurrent_handlers:
                # 并发已满，丢弃本条；主循环仍会兜底
                continue
            task = asyncio.create_task(self._run_handler(handler, game))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _run_handler(self, handler, game: dict) -> None:
        try:
            await handler(game)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("体育实时流事件处理失败：%s: %s", type(exc).__name__, exc)

    def _enqueue(self, game: dict) -> None:
        if self.on_game_update is None:
            return
        try:
            self._update_queue.put_nowait(game)
        except asyncio.QueueFull:
            # 队列满时丢弃，主循环仍会兜底处理
            pass

    @staticmethod
    def _round_score(raw: object):
        """从 "012-005|1-0|Bo3" 里取出当前地图回合比分。"""
        try:
            head = str(raw or "").split("|")[0]
            left, right = head.split("-")
            return int(left), int(right)
        except (ValueError, AttributeError, IndexError):
            return None

    @classmethod
    def _is_score_regression(cls, prev: dict, new: dict) -> bool:
        """识别「同一地图内比分从真实值回退成 0-0」的脏数据。

        实测数据源会间歇性把进行中地图的比分发成 000-000，
        这会掩盖真实比分并可能让信号判定错过时机，因此忽略这种回退。
        """
        if str(prev.get("period") or "") != str(new.get("period") or ""):
            return False
        before = cls._round_score(prev.get("score"))
        after = cls._round_score(new.get("score"))
        if before is None or after is None:
            return False
        return after == (0, 0) and before != (0, 0)

    async def _connect_once(self, url: str):
        if self._session is None:
            return
        async with self._session.ws_connect(url, heartbeat=30) as ws:
            self.connected = True
            self.consecutive_failures = 0
            self.last_error = ""
            logger.info("Polymarket sports feed connected: %s", url)
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_text(ws, message.data)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        self.connected = False

    async def _handle_text(self, ws, raw: str):
        payload_text = raw.strip()
        if payload_text.lower() == "ping":
            await ws.send_str("pong")
            return
        if not payload_text.startswith("{") and not payload_text.startswith("["):
            return
        try:
            data = json.loads(payload_text)
        except json.JSONDecodeError:
            return

        self.last_message_at = time.time()
        self.message_count += 1
        items = data if isinstance(data, list) else [data]
        for item in items:
            game = item.get("payload", item) if isinstance(item, dict) else None
            if not isinstance(game, dict):
                continue
            game_id = game.get("gameId")
            if game_id is None:
                continue
            key = str(game_id)
            previous = dict(self.games.get(key) or {})
            regression = self._is_score_regression(previous, game)
            merged = dict(previous)
            merged.update(game)
            if regression:
                # 保留上一份真实比分，并标记本条为脏数据
                merged["score"] = previous.get("score")
                merged["score_regression"] = True
            merged["received_at"] = self.last_message_at
            self.games[key] = merged
            league = str(game.get("leagueAbbreviation") or "?")
            self.leagues_seen[league] = self.leagues_seen.get(league, 0) + 1
            if regression:
                # 状态没有真正变化，不触发事件驱动
                continue
            self._enqueue(merged)
        if self.message_count % 200 == 0:
            self._evict_stale()

    # ---------- public API ----------

    def snapshot(self, league: Optional[str] = None) -> list[dict]:
        rows = list(self.games.values())
        if league:
            target = league.lower()
            rows = [r for r in rows if str(r.get("leagueAbbreviation", "")).lower() == target]
        return rows

    def live_snapshot(self, league: Optional[str] = None, max_age_seconds: float = 600.0) -> list[dict]:
        """只返回「正在进行且最近有更新」的比赛，避免对过期缓存下手。"""
        now = time.time()
        return [
            row for row in self.snapshot(league)
            if row.get("live")
            and not row.get("ended")
            and (now - float(row.get("received_at") or 0)) <= max_age_seconds
        ]

    def _evict_stale(self) -> None:
        """清理长时间没有更新的比赛，避免内存无限增长与误判为进行中。"""
        now = time.time()
        stale = [
            key for key, row in self.games.items()
            if now - float(row.get("received_at") or now)
            > (1800.0 if row.get("ended") else 5400.0)
        ]
        for key in stale:
            self.games.pop(key, None)

    def health(self) -> dict:
        return {
            "CS2": {
                "sport": "CS2",
                "source": "Polymarket 实时流（Sportradar）",
                "connected": self.connected,
                "last_success_at": self.last_message_at,
                "last_error_at": self.last_error_at,
                "last_error": self.last_error,
                "consecutive_failures": self.consecutive_failures,
                "last_event_count": len(self.snapshot("cs2")),
                "last_latency_ms": None,
            }
        }
