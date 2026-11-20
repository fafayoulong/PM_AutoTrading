# -*- coding: utf-8 -*-
import asyncio
import aiohttp
import json

async def test_ws():
    session = aiohttp.ClientSession()
    ws_url = "wss://stream.binance.com:9443/ws/btcusdt@miniTicker"
    try:
        ws = await session.ws_connect(ws_url, proxy="http://127.0.0.1:7897", timeout=aiohttp.ClientWSTimeout(ws_close=5))
        msg = await ws.receive(timeout=5)
        data = json.loads(msg.data)
        symbol = data.get("s", "?")
        price = data.get("c", "?")
        print(f"Binance WS OK: {symbol} price={price}")
        await ws.close()
    except Exception as e:
        print(f"Binance WS error: {e}")
    await session.close()

asyncio.run(test_ws())
