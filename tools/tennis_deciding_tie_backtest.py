# -*- coding: utf-8 -*-
"""Backtest the deciding-set tied-score strategy from the tennis model ledger.

Rule:
  * best-of-three, third set, sets tied 1-1;
  * current set games tied at 1-1 through 6-6;
  * each (match, game score) triggers once;
  * buy the cheaper outcome at its executable ask;
  * hold to binary settlement.

The historical ledger stores the leader-side token/book only. The opposite-side
ask is therefore inferred as ``1 - leader_bid``. This is exact under a
complementary binary book, but may differ by a tick from independently sampled
CLOB books. Recent shadow records, which sampled both books, are the validation
set for that approximation.

Usage:
    python tools/tennis_deciding_tie_backtest.py --no-network
    python tools/tennis_deciding_tie_backtest.py --tick-buffer 1
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_model_ledger as ledger  # noqa: E402


BJT = ZoneInfo("Asia/Shanghai")
TIE_SCORES = {f"{n}-{n}" for n in range(1, 7)}


def result_of(cache: dict, row: dict):
    value = cache.get(f"{row.get('token_id') or ''}|{row.get('leader') or ''}")
    if value in (0, 1, 0.0, 1.0):
        return int(value)
    return None


def choose_entry(row: dict, tick_buffer: float, min_ask: float):
    """Return (selected_side, ask, tick_size), or None.

    If the two implied asks are equal, prefer the leader side deterministically.
    """
    try:
        leader_ask = float(row["best_ask"])
        leader_bid = float(row["best_bid"])
        tick_size = float(row.get("tick_size") or 0.01)
    except (KeyError, TypeError, ValueError):
        return None
    underdog_ask = 1.0 - leader_bid
    if not all(0.0 < value < 1.0 for value in (leader_ask, underdog_ask)):
        return None
    if leader_bid >= leader_ask or tick_size <= 0:
        return None
    selected = "leader" if leader_ask <= underdog_ask else "underdog"
    ask = leader_ask if selected == "leader" else underdog_ask
    ask += tick_buffer * tick_size
    if ask < min_ask or not (0.0 < ask < 1.0):
        return None
    return selected, ask, tick_size


def build_events(rows, cache, *, fee_rate, tick_buffer, min_ask):
    candidate_keys = set()
    settled_keys = set()
    event_keys = set()
    events = []
    for row in rows:
        try:
            if int(row.get("set_index") or 0) != 3:
                continue
            if int(row.get("leader_sets") or 0) != 1:
                continue
            if int(row.get("underdog_sets") or 0) != 1:
                continue
            games_leader = int(row.get("leader_games") or 0)
            games_underdog = int(row.get("underdog_games") or 0)
        except (TypeError, ValueError):
            continue
        score = f"{games_leader}-{games_underdog}"
        if score not in TIE_SCORES:
            continue
        match_id = str(row.get("match_id") or "")
        if not match_id:
            continue
        key = (match_id, score)
        candidate_keys.add(key)
        if key in event_keys:
            continue
        leader_result = result_of(cache, row)
        if leader_result is None:
            continue
        settled_keys.add(key)
        chosen = choose_entry(row, tick_buffer, min_ask)
        if chosen is None:
            continue
        selected, ask, tick_size = chosen
        event_keys.add(key)
        won = bool(leader_result) if selected == "leader" else not bool(leader_result)
        stake = 1.0
        shares = stake / ask
        fee = stake * fee_rate * (1.0 - ask)
        cost = stake + fee
        pnl = shares - stake - fee if won else -stake - fee
        events.append({
            "t": float(row.get("t") or 0),
            "match_id": match_id,
            "score": score,
            "selected": selected,
            "ask": ask,
            "created_spread": float(row.get("spread") or 0),
            "won": won,
            "cost": cost,
            "pnl": pnl,
            "roi": pnl / cost,
            "tournament": str(row.get("tournament") or ""),
            "question": str(row.get("question") or ""),
        })
    return {
        "events": events,
        "candidates": len(candidate_keys),
        "settlement_missing": len(candidate_keys - settled_keys),
        "quote_missing": len(settled_keys - event_keys),
    }


def cluster_bootstrap(events, seed=17, rounds=10000):
    grouped = collections.defaultdict(list)
    for event in events:
        grouped[event["match_id"]].append(event)
    keys = list(grouped)
    if len(keys) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        picked = [keys[rng.randrange(len(keys))] for _ in keys]
        sample = [event for key in picked for event in grouped[key]]
        cost = sum(event["cost"] for event in sample)
        pnl = sum(event["pnl"] for event in sample)
        values.append(pnl / cost if cost else 0.0)
    values.sort()
    return values[int(0.025 * rounds)], values[int(0.975 * rounds) - 1]


def wilson(wins, n, z=1.96):
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def print_stats(label, events):
    if not events:
        print(f"{label:<18} n=0")
        return
    wins = sum(event["won"] for event in events)
    cost = sum(event["cost"] for event in events)
    pnl = sum(event["pnl"] for event in events)
    avg_ask = statistics.mean(event["ask"] for event in events)
    matches = len({event["match_id"] for event in events})
    win_lo, win_hi = wilson(wins, len(events))
    roi_lo, roi_hi = cluster_bootstrap(events)
    print(
        f"{label:<18} n={len(events):>4} matches={matches:>3} "
        f"win={wins/len(events):>6.1%} [{win_lo:>6.1%},{win_hi:>6.1%}] "
        f"avg={avg_ask:.3f} pnl=${pnl:>7.2f} ROI={pnl/cost:>7.2%} "
        f"cluster95=[{roi_lo:>7.1%},{roi_hi:>7.1%}]"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--fee-rate", type=float, default=0.05)
    parser.add_argument("--tick-buffer", type=float, default=0.0)
    parser.add_argument("--min-ask", type=float, default=0.0)
    parser.add_argument("--path", default="")
    args = parser.parse_args()

    rows = ledger.load_rows(args.path) if args.path else ledger.load_rows()
    if not rows:
        print("没有网球账本")
        return 1
    rows.sort(key=lambda row: float(row.get("t") or 0))
    cache = ledger.resolve_results(rows, allow_network=not args.no_network)
    result = build_events(
        rows,
        cache,
        fee_rate=max(0.0, args.fee_rate),
        tick_buffer=max(0.0, args.tick_buffer),
        min_ask=max(0.0, args.min_ask),
    )
    events = result["events"]
    print(
        f"账本 {len(rows)} 条 | 决胜盘平分候选 {result['candidates']} 个 "
        f"| 缺结算 {result['settlement_missing']} "
        f"| 缺有效报价 {result['quote_missing']} | 可回测 {len(events)} 个"
    )
    if args.tick_buffer:
        print(f"下单价额外增加 {args.tick_buffer:g} tick；手续费率 {args.fee_rate:.1%}")
    if args.min_ask:
        print(f"已过滤下单价 < {args.min_ask:.2f}")
    if not events:
        return 0
    first = min(event["t"] for event in events)
    last = max(event["t"] for event in events)
    print(
        f"范围 {datetime.fromtimestamp(first, BJT):%Y-%m-%d %H:%M} ~ "
        f"{datetime.fromtimestamp(last, BJT):%Y-%m-%d %H:%M}"
    )
    print()
    print("=== 总体 ===")
    print_stats("全部", events)

    print("\n=== 按平分比分 ===")
    grouped = collections.defaultdict(list)
    for event in events:
        grouped[event["score"]].append(event)
    for score in sorted(grouped, key=lambda value: int(value.split("-")[0])):
        print_stats(score, grouped[score])

    print("\n=== 按便宜方身份 ===")
    grouped = collections.defaultdict(list)
    for event in events:
        grouped[event["selected"]].append(event)
    for side, label in (("leader", "领先方/观察方"), ("underdog", "对手方")):
        if side in grouped:
            print_stats(label, grouped[side])

    print("\n=== 按入场价 ===")
    def price_band(price):
        if price < 0.30:
            return "<0.30"
        if price < 0.40:
            return "0.30-0.40"
        if price < 0.45:
            return "0.40-0.45"
        if price < 0.50:
            return "0.45-0.50"
        return ">=0.50"

    grouped = collections.defaultdict(list)
    for event in events:
        grouped[price_band(event["ask"])].append(event)
    for band in ("<0.30", "0.30-0.40", "0.40-0.45", "0.45-0.50", ">=0.50"):
        if band in grouped:
            print_stats(band, grouped[band])

    print("\n=== 按日期 ===")
    grouped = collections.defaultdict(list)
    for event in events:
        grouped[datetime.fromtimestamp(event["t"], BJT).strftime("%Y-%m-%d")].append(event)
    for date in sorted(grouped):
        print_stats(date, grouped[date])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
