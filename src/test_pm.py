# -*- coding: utf-8 -*-
"""Test Polymarket search with different ordering."""

import asyncio
import json

from src.config import load_config
from src.polymarket_feed import PolymarketFeed


async def test():
    config = load_config()
    pm = PolymarketFeed(config)
    await pm.start()

    url = "https://gamma-api.polymarket.com/events"
    for ascending in ["false", "true"]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "tag_slug": "mlb",
            "order": "endDate",
            "ascending": ascending,
        }
        async with pm._session.get(url, params=params, proxy=pm._proxy) as resp:
            events = await resp.json()
            print(f"\n=== ascending={ascending}: {len(events)} events ===")

            full_games = []
            for e in events:
                title = e.get("title", "").lower()
                skip_kws = ["inning", "props", "player", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "first five"]
                if "vs." in title and not any(kw in title for kw in skip_kws):
                    full_games.append(e)
            print(f"Full games: {len(full_games)}")

            for e in full_games[:8]:
                m = e.get("markets", [{}])[0]
                bid = m.get("bestBid", "?")
                ask = m.get("bestAsk", "?")
                print(f"  {e['title']} | bid={bid} ask={ask}")

    await pm.stop()


asyncio.run(test())
