# -*- coding: utf-8 -*-
"""交易链条上每一段「网络腿」的可比测量（本地 / VPS 跑同一份，输出同一格式）。

    python tools/measure_chain_latency.py --n 8
    python tools/measure_chain_latency.py --n 8 --token <clob token id>

测的是 bot 真正会打的那些端点，每项跑 n 次取中位/最小/最大：
    clob_book    读盘口（下单前必须做的一次 CLOB REST）
    clob_orders  查自己的挂单（探活/成交确认走的认证 GET）
    gamma_events 市场搜索（预匹配用）
    data_positions 交易所持仓对账
    espn_score   比分源
    ws_clob / ws_sports  WebSocket 域名握手（推送通道的建连成本）

注意：WS **推送**的延迟约等于一次应用层 RTT，这里只能测握手 + 域名可达；
真正的端到端推送延迟取决于服务端何时下发，不在本脚本范围内。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import httpx

try:  # 与 bot 同一口径：先读 .env，再取环境变量
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # pragma: no cover
    pass

PROXY = (os.getenv("PM_PROXY_URL", "") or "").strip() or None
WALLET = os.getenv("PM_FUNDER_ADDRESS") or os.getenv("PM_WALLET_ADDRESS") or ""
DEFAULT_TOKEN = os.getenv("PM_LATENCY_TOKEN") or ""


def timed(fn, n):
    ok, lat, err = 0, [], ""
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            fn()
            lat.append((time.perf_counter() - t0) * 1000)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {str(exc)[:60]}"
    return ok, lat, err


def report(label, ok, lat, err, n):
    if lat:
        print("  {:<16} {}/{} 次成功  中位 {:>8.1f} ms   最快 {:>7.1f}   最慢 {:>8.1f}{}".format(
            label, ok, n, statistics.median(lat), min(lat), max(lat),
            ("   " + err) if err and ok < n else ""))
    else:
        print("  {:<16} 全部失败：{}".format(label, err or "未知错误"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    args = ap.parse_args()
    n = max(1, args.n)

    client = httpx.Client(proxy=PROXY, timeout=15.0, http2=True)
    print(f"出口代理 = {PROXY or '（直连）'}")
    print(f"每个端点测 {n} 次：")

    if args.token:
        report("clob_book", *timed(
            lambda: client.get("https://clob.polymarket.com/book",
                               params={"token_id": args.token}), n), n)
    else:
        print("  clob_book        跳过（未提供 --token / PM_LATENCY_TOKEN）")

    def orders():
        if not WALLET:
            raise RuntimeError("缺 PM_FUNDER_ADDRESS")
        client.get("https://clob.polymarket.com/data/orders", params={"maker_address": WALLET})

    report("clob_orders", *timed(orders, n), n)
    report("gamma_events", *timed(
        lambda: client.get("https://gamma-api.polymarket.com/events",
                           params={"limit": 5, "active": "true", "closed": "false",
                                   "tag_slug": "tennis"}), n), n)
    report("data_positions", *timed(
        lambda: client.get("https://data-api.polymarket.com/v2/positions",
                           params={"user": WALLET, "limit": 5}), n), n)
    report("espn_score", *timed(
        lambda: client.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"), n), n)

    # WS 域名握手（推送通道建连）：用 HTTPS 打到同一主机，近似建连+TLS 成本
    report("ws_clob_域名", *timed(
        lambda: client.get("https://ws-subscriptions-clob.polymarket.com/"), n), n)
    report("ws_sports_域名", *timed(
        lambda: client.get("https://sports-api.polymarket.com/"), n), n)

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
