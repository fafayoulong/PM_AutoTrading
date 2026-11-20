# -*- coding: utf-8 -*-
"""Binance real-time price feed via WebSocket for crypto price crossing detection."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class CryptoPrice:
    symbol: str  # "BTCUSDT"
    price: float
    timestamp: float
    high_24h: float = 0
    low_24h: float = 0
    volume_24h: float = 0


class BinanceFeed:
    """Connects to Binance WebSocket for real-time crypto prices."""

    WS_BASE = "wss://stream.binance.com:9443/stream"
    REST_BASE = "https://api.binance.com/api/v3"

    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.prices: dict[str, CryptoPrice] = {}
        self.price_history: dict[str, list[CryptoPrice]] = {}
        self._running = False
        self._callbacks = []

    def on_price_update(self, callback):
        """Register a callback that fires on every price update."""
        self._callbacks.append(callback)

    async def start(self, symbols: list[str] = None):
        """Start WebSocket connection for given symbols (default BTC, ETH, SOL)."""
        symbols = symbols or ["btcusdt", "ethusdt", "solusdt"]
        self._session = aiohttp.ClientSession()
        self._running = True

        streams = "/".join(f"{s}@miniTicker" for s in symbols)
        ws_url = f"{self.WS_BASE}?streams={streams}"
        proxy = self.config.proxy_url

        logger.info("Binance WS connecting: %s", ws_url)

        while self._running:
            try:
                self._ws = await self._session.ws_connect(
                    ws_url,
                    proxy=proxy,
                    timeout=aiohttp.ClientWSTimeout(ws_close=30),
                )
                logger.info("Binance WS connected")

                async for msg in self._ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._process_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning("Binance WS error: %s", msg.data)
                        break
            except Exception as exc:
                logger.warning("Binance WS connection error: %s", exc)

            if self._running:
                logger.info("Binance WS reconnecting in 5s...")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _process_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        payload = data.get("data", data)
        symbol = payload.get("s", "")
        price = float(payload.get("c", 0))
        if not symbol or not price:
            return

        crypto_price = CryptoPrice(
            symbol=symbol,
            price=price,
            timestamp=time.time(),
            high_24h=float(payload.get("h", 0)),
            low_24h=float(payload.get("l", 0)),
            volume_24h=float(payload.get("q", 0)),
        )

        self.prices[symbol] = crypto_price

        # Keep price history (last 1000 points)
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        history = self.price_history[symbol]
        history.append(crypto_price)
        if len(history) > 1000:
            self.price_history[symbol] = history[-1000:]

        for callback in self._callbacks:
            asyncio.create_task(callback(crypto_price))

    def get_price(self, symbol: str = "BTCUSDT") -> Optional[float]:
        p = self.prices.get(symbol.upper())
        return p.price if p else None

    def get_price_history(self, symbol: str = "BTCUSDT", last_n: int = 100) -> list[CryptoPrice]:
        return self.price_history.get(symbol.upper(), [])[-last_n:]

    async def fetch_active_crossing_markets(
        self,
        asset: str = "Bitcoin",
        date_str: str = None,
    ) -> list[dict]:
        """Fetch Polymarket crypto crossing markets for today."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        url = f"{self.config.polymarket_gamma_url}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": 50,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            async with self._session.get(
                url, params=params, proxy=self.config.proxy_url,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return []
                events = await resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch events: %s", exc)
            return []

        # Filter for crypto crossing markets
        results = []
        for event in events:
            title = event.get("title", "")
            if asset.lower() not in title.lower():
                continue
            if "above" not in title.lower():
                continue

            markets = event.get("markets", [])
            for m in markets:
                question = m.get("question", "")
                bid = float(m.get("bestBid", 0) or 0)
                ask = float(m.get("bestAsk", 1) or 1)

                # Extract strike price from question
                import re
                strike_match = re.search(r"\$([\d,]+(?:\.\d+)?)", question)
                strike = None
                if strike_match:
                    strike = float(strike_match.group(1).replace(",", ""))

                results.append({
                    "event_id": event.get("id", ""),
                    "event_title": title,
                    "question": question,
                    "strike": strike,
                    "best_bid": bid,
                    "best_ask": ask,
                    "end_date": event.get("endDate", ""),
                    "volume_24h": float(event.get("volume24hr", 0) or 0),
                })

        return results
