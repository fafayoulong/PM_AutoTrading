# -*- coding: utf-8 -*-
import asyncio
from src.config import load_config
from src.crypto_feed import BinanceFeed


async def test():
    config = load_config()
    feed = BinanceFeed(config)
    task = asyncio.create_task(feed.start(["btcusdt"]))

    # Wait for a few price updates
    await asyncio.sleep(3)

    btc = feed.get_price("BTCUSDT")
    eth = feed.get_price("ETHUSDT")
    print(f"BTC: ${btc:,.0f}" if btc else "BTC: no data")
    print(f"ETH: ${eth:,.2f}" if eth else "ETH: no data")

    history = feed.get_price_history("BTCUSDT", last_n=5)
    print(f"History: {len(history)} points")
    for p in history:
        print(f"  {p.symbol} {p.price:.2f}")

    # Fetch crossing markets
    markets = await feed.fetch_active_crossing_markets("Bitcoin")
    print(f"\nBitcoin crossing markets: {len(markets)}")
    for m in markets[:10]:
        print(f"  {m['question'][:70]} | bid={m['best_bid']:.3f} ask={m['best_ask']:.3f} strike={m['strike']}")

    feed._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if feed._session and not feed._session.closed:
        await feed._session.close()


asyncio.run(test())
