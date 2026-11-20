# -*- coding: utf-8 -*-
import asyncio
import json
from src.config import load_config
from src.polymarket_feed import PolymarketFeed


async def get_crypto_markets():
    config = load_config()
    pm = PolymarketFeed(config)
    await pm.start()

    url = "https://gamma-api.polymarket.com/events"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 50,
        "tag_slug": "crypto-prices",
        "order": "volume24hr",
        "ascending": "false",
    }
    async with pm._session.get(url, params=params, proxy=pm._proxy) as resp:
        events = await resp.json()

    for e in events:
        title = e.get("title", "")
        end_date = e.get("endDate", "?")
        markets = e.get("markets", [])
        print(f"Event: {title}")
        print(f"  End: {end_date}")
        for m in markets[:5]:
            q = m.get("question", "")[:80]
            bid = m.get("bestBid", "?")
            ask = m.get("bestAsk", "?")
            outcomes = m.get("outcomes", "[]")
            tokens_raw = m.get("clobTokenIds", "[]")
            print(f"  {q} | bid={bid} ask={ask}")
            print(f"    outcomes={outcomes}")
            print(f"    tokens={str(tokens_raw)[:80]}")
        print()

    await pm.stop()


asyncio.run(get_crypto_markets())
