# -*- coding: utf-8 -*-
"""回放"最初那套网球策略"，用**修好盘口采集之后**的干净数据。

背景（2026-10-23 用户："项目最开始策略最粗糙，但收益最高，能不能换回去"）
--------------------------------------------------------------------------------
最初（PRODUCT_DESIGN v1.0 / 2026-09-03，09-08 放宽后实际生效）：
    * 判定：盘分不等（取领先方）且当前盘局分领先 >= 1 局，最后一盘未分出胜负
    * 价格：`max_entry_price = 0.92`（只买还剩 >=3% 毛利）、`min_expected_profit = 0.03`
    * **没有价格下限** —— 0.3、0.5 的盘也照买
现在：局分差 >= 2、价格区间 0.60~0.96。

为什么必须重跑：2026-10-09 之前 `tools/tennis_model_ledger.MarketCache` 按 match_id
缓存 token，领先方一翻转就会拿到对手方的盘口，导致低档位出现大量假的 0.001 报价
（见 #128/#131）。所以"原始参数很赚"的旧回放不可信。

口径：
  * 只用 `trades/tennis_model_ledger.jsonl*` 里的**原始字段**重建规则
    （不依赖 `heuristic_lock`，那个字段跟随现行阈值）。
  * 每场只取**第一个**满足规则的快照（= 实盘"每场只触发一次"），
    再按价格窗口过滤 —— 也就是"如果当时用的是那套参数，会在哪里入场"。
  * 结算复用 `tennis_model_ledger.resolve_results`（Gamma，只认 0/1）。
  * ROI = 等额 1 单位名义金额买入领先方、持有到结算。

用法：
    python tools/tennis_original_rule_replay.py [--since 2026-10-09] [--no-network]
只读（唯一写入是共用的结算缓存 trades/tennis_model_results.json）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_model_ledger as ledger  # noqa: E402

TZ = datetime.timezone(datetime.timedelta(hours=8))


def set_is_decided(games_a: int, games_b: int) -> bool:
    """与 src/multi_signal.set_is_decided 同口径：>=6 且净胜 >=2。"""
    if max(games_a, games_b) >= 7 and abs(games_a - games_b) >= 2:
        return True
    return max(games_a, games_b) >= 6 and abs(games_a - games_b) >= 2


def rule_ok(row: dict, min_game_diff: int) -> bool:
    """重建 evaluate_tennis_lock 的判定（不含 drill / 名字检查）。"""
    if row.get("sets_level"):
        return False
    ls = int(row.get("leader_sets") or 0)
    us = int(row.get("underdog_sets") or 0)
    if ls <= us:
        return False
    lg = int(row.get("leader_games") or 0)
    ug = int(row.get("underdog_games") or 0)
    if set_is_decided(lg, ug):
        return False
    return (lg - ug) >= min_game_diff


def first_per_match(rows: list[dict], predicate) -> list[dict]:
    seen: dict[str, dict] = {}
    for row in rows:
        if not predicate(row):
            continue
        seen.setdefault(str(row.get("match_id") or ""), row)
    return list(seen.values())


def stats(rows: list[dict], cache: dict) -> dict:
    n = wins = 0
    cost = 0.0
    pnl = 0.0
    for row in rows:
        won = ledger._won(cache, row)
        ask = row.get("best_ask")
        if won is None or ask is None:
            continue
        ask = float(ask)
        if not (0.0 < ask < 1.0):
            continue
        n += 1
        cost += ask
        if won:
            wins += 1
            pnl += 1.0 - ask
        else:
            pnl -= ask
    if not n:
        return {"n": 0}
    return {"n": n, "win": wins / n, "cost": cost, "pnl": pnl,
            "roi": pnl / cost if cost else 0.0, "avg": cost / n}


def line(label: str, st: dict) -> None:
    if not st.get("n"):
        print("  %-30s %4d 场   (无可用样本)" % (label, 0))
        return
    print("  %-30s n=%3d 胜率=%5.1f%% 均价=%.3f ROI=%+7.2f%%  PnL=%+7.2f" % (
        label, st["n"], st["win"] * 100, st["avg"], st["roi"] * 100, st["pnl"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-09",
                    help="只统计这个日期（含）之后的快照；默认盘口采集修好的那天")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()

    rows = ledger.load_rows()
    rows = [r for r in rows
            if datetime.datetime.fromtimestamp(float(r.get("t") or 0), TZ)
            .strftime("%Y-%m-%d") >= args.since]
    if not rows:
        print("没有 %s 之后的快照" % args.since)
        return 1
    cache = ledger.resolve_results(rows, allow_network=not args.no_network)
    print("账本快照 %d 条（%s 起），覆盖 %d 场比赛"
          % (len(rows), args.since, len({str(r.get('match_id')) for r in rows})))
    print()

    # 原始规则（局分差 >=1）与现行规则（>=2）的入场点
    orig = first_per_match(rows, lambda r: rule_ok(r, 1))
    tight = first_per_match(rows, lambda r: rule_ok(r, 2))

    def px(r):
        v = r.get("best_ask")
        return float(v) if v is not None else None

    print("=== A) 最初那套价格窗口（<=0.92，无下限）===")
    line("原始规则 局差>=1", stats([r for r in orig if px(r) is not None and px(r) <= 0.92], cache))
    line("现行规则 局差>=2", stats([r for r in tight if px(r) is not None and px(r) <= 0.92], cache))
    line("原始规则 且 ask<0.60", stats([r for r in orig if px(r) is not None and px(r) < 0.60], cache))
    print()
    print("=== B) 现行价格窗口（0.60~0.96）===")
    line("原始规则 局差>=1", stats([r for r in orig if px(r) is not None and 0.60 <= px(r) <= 0.96], cache))
    line("现行规则 局差>=2", stats([r for r in tight if px(r) is not None and 0.60 <= px(r) <= 0.96], cache))
    print()
    print("=== C) 不带任何价格过滤（看规则本身）===")
    line("原始规则 局差>=1", stats(orig, cache))
    line("现行规则 局差>=2", stats(tight, cache))
    print()
    print("=== D) 原始规则的入场价分布 ===")
    prices = sorted(px(r) for r in orig if px(r) is not None)
    if prices:
        print("  n=%d  最小=%.2f  p25=%.2f  中位=%.2f  p75=%.2f  最大=%.2f  <0.60 占 %d/%d"
              % (len(prices), prices[0], prices[len(prices) // 4],
                 statistics.median(prices), prices[3 * len(prices) // 4], prices[-1],
                 sum(1 for p in prices if p < 0.60), len(prices)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
