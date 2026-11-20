# -*- coding: utf-8 -*-
"""Polymarket CLOB WebSocket feed for real-time order book monitoring."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable

import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class BookUpdate:
    asset_id: str
    market: str
    timestamp: str
    bids: list[dict] = field(default_factory=list)
    asks: list[dict] = field(default_factory=list)
    hash: str = ""
    event_type: str = ""


class PolymarketWS:
    """Connects to Polymarket CLOB WebSocket and monitors token order books in real-time."""

    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._watching: dict[str, Callable] = {}  # asset_id -> callback
        self._prices: dict[str, float] = {}  # asset_id -> mid price
        self._running = False

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._running = True

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def watch_token(self, asset_id: str, callback: Callable):
        """Subscribe to real-time updates for a token ID."""
        self._watching[asset_id] = callback

    async def unwatch_token(self, asset_id: str):
        self._watching.pop(asset_id, None)

    @property
    def prices(self) -> dict[str, float]:
        return self._prices

    def get_price(self, asset_id: str) -> Optional[float]:
        return self._prices.get(asset_id)

    async def run(self):
        """Main WebSocket loop - connects and processes messages."""
        proxy = self.config.proxy_url

        while self._running:
            try:
                self._ws = await self._session.ws_connect(
                    WS_URL, proxy=proxy, timeout=aiohttp.ClientWSTimeout(ws_close=30)
                )
                logger.info("Polymarket WS connected: %s", WS_URL)

                # Subscribe to watched assets
                if self._watching:
                    sub_msg = {
                        "assets_ids": list(self._watching.keys()),
                        "type": "market",
                    }
                    await self._ws.send_str(json.dumps(sub_msg))
                    logger.info("Subscribed to %d assets", len(self._watching))

                async for msg in self._ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._process_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning("WS error: %s", msg.data)
                        break
            except Exception as exc:
                logger.warning("WS connection error: %s", exc)

            if self._running:
                logger.info("Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _process_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Messages can be a list of events or a single event
        events = data if isinstance(data, list) else [data]

        for event in events:
            event_type = event.get("event_type", "")

            if event_type == "book":
                asset_id = event.get("asset_id", "")
                bids = event.get("bids", [])
                asks = event.get("asks", [])

                # Calculate mid price
                if bids and asks:
                    best_bid = float(bids[-1].get("price", 0)) if isinstance(bids[-1], dict) else 0
                    best_ask = float(asks[0].get("price", 1)) if isinstance(asks[0], dict) else 1
                    mid = (best_bid + best_ask) / 2
                elif bids:
                    mid = float(bids[-1].get("price", 0)) if isinstance(bids[-1], dict) else 0
                elif asks:
                    mid = float(asks[0].get("price", 1)) if isinstance(asks[0], dict) else 1
                else:
                    continue

                self._prices[asset_id] = mid

                callback = self._watching.get(asset_id)
                if callback:
                    update = BookUpdate(
                        asset_id=asset_id,
                        market=event.get("market", ""),
                        timestamp=event.get("timestamp", ""),
                        bids=bids,
                        asks=asks,
                        hash=event.get("hash", ""),
                        event_type=event_type,
                    )
                    asyncio.create_task(callback(update))

            elif event_type == "price_change":
                asset_id = event.get("asset_id", "")
                price = event.get("price", "")
                if price:
                    self._prices[asset_id] = float(price)
