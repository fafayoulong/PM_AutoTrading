# -*- coding: utf-8 -*-
"""网球锁定信号：翻盘率到底有多高、收紧阈值能换到什么。

用户问题（2026-10-09）："网球目前的锁定信号似乎被翻盘的概率有点大"，要不要改严。

口径
----
* 样本 = `trades/tennis_model_ledger.jsonl*`（2026-09-29 起，~30 秒一条快照），
  只取 `heuristic_lock=True` 的快照。
* **每个（规则/状态）分档里，每场只取第一条命中的快照** —— 对应"如果这条规则当时
  生效，实盘的入场时点就是这里"，且避免同一场比赛刷出几十条高度相关的快照。
  注意不能用"整场只取第一条锁定快照"：那必然落在"刚赢下第 1 盘、第 2 盘 1-0"
  这一个状态上，会让所有分档看起来都只有一种状态。
* 结算复用 `tools/tennis_model_ledger.resolve_results`（Gamma，只认 0/1）。
* 翻盘率 = 1 − 领先方最终胜率。**市场隐含翻盘率 = 1 − 盘口价**。

为什么要并排看"实际翻盘率"和"市场隐含翻盘率"
--------------------------------------------
翻盘率本身高不高没有意义 —— 价格 0.70 的信号翻盘 30% 是正常的。
能赚钱的前提是 **实际翻盘率 < 市场隐含翻盘率**（即真实优势）。收紧阈值一定能把
翻盘率压下去，但同时买入价也会上移；市场有效的话 ROI 不变，只是信号变少。

用法：
    python tools/tennis_lock_rule_review.py [--min-ask 0.30] [--no-network]
只读（唯一写入是共用的结算缓存 trades/tennis_model_results.json）。
"""

from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_model_ledger as ledger  # noqa: E402


def price_of(row: dict) -> float | None:
    for key in ("mid", "best_ask"):
        value = row.get(key)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 < value < 1.0:
            return value
    return None


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, center - half), min(1.0, center + half))


def pick_per_match(rows: list[dict], pred) -> list[dict]:
    """按 pred 过滤后，每场只留第一条（rows 已按时间排序）。"""
    seen: dict[str, dict] = {}
    for row in rows:
        if not pred(row):
            continue
        seen.setdefault(str(row.get("match_id") or ""), row)
    return list(seen.values())


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in rows if r["won"])
    prices = [r["price"] for r in rows]
    cost = sum(prices)
    pnl = sum((1.0 - r["price"]) if r["won"] else (-r["price"]) for r in rows)
    lo, hi = wilson(wins, n)
    return {
        "n": n,
        "win": wins / n,
        "win_ci": (lo, hi),
        "implied": sum(1.0 - p for p in prices) / n,
        "avg_price": cost / n,
        "roi": pnl / cost if cost else 0.0,
    }


def line(label: str, st: dict) -> None:
    if not st.get("n"):
        print("  %-34s %4d 场   (无样本)" % (label, 0))
        return
    lo, hi = st["win_ci"]
    gap = (1 - st["win"]) - st["implied"]
    flag = ""
    if lo > 1 - st["implied"]:
        flag = "  ★实际翻盘率显著低于市场"
    print("  %-34s n=%3d  翻盘率=%5.1f%% (胜率 %.1f~%.1f%%)  市场隐含=%5.1f%%  "
          "差=%+5.1fpp  均价=%.3f  ROI=%+6.2f%%%s"
          % (label, st["n"], (1 - st["win"]) * 100, lo * 100, hi * 100,
             st["implied"] * 100, -gap * 100, st["avg_price"], st["roi"] * 100, flag))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-ask", type=float, default=0.30,
                    help="丢掉盘口价低于此值的快照（#128：那一段字段不可信）")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()

    rows = ledger.load_rows()
    locks = [r for r in rows if r.get("heuristic_lock") and r.get("best_ask") is not None]
    locks.sort(key=lambda r: float(r.get("t") or 0))
    cache = ledger.resolve_results(locks, allow_network=not args.no_network)
    samples = []
    for row in locks:
        won = ledger._won(cache, row)
        price = price_of(row)
        if won is None or price is None or price < args.min_ask:
            continue
        samples.append({**row, "won": won, "price": price})
    matches = {str(r.get("match_id")) for r in samples}
    print("锁定快照 %d 条（%d 场）；已结算且有可信盘口价 %d 条（%d 场）"
          % (len(locks), len({str(r.get('match_id')) for r in locks}),
             len(samples), len(matches)))
    if args.min_ask > 0:
        print("（已过滤盘口价 < %.2f 的快照，见 PROJECT_DESIGN #128）" % args.min_ask)
    print()
    if not samples:
        print("没有可用样本")
        return 1

    print("=== 1) 按状态看翻盘率（现行规则的全部锁定信号）===")
    def sets_key(r):
        return "%d-%d" % (int(r.get("leader_sets") or 0), int(r.get("underdog_sets") or 0))

    def game_diff(r):
        return int(r.get("leader_games") or 0) - int(r.get("underdog_games") or 0)

    for label, pred in (
        ("全部（现状：领先盘 + 局分差≥1）", lambda r: True),
        ("盘分 1-0", lambda r: sets_key(r) == "1-0"),
        ("盘分 2-0", lambda r: sets_key(r) == "2-0"),
        ("盘分 2-1", lambda r: sets_key(r) == "2-1"),
        ("当前盘局分差 =1", lambda r: game_diff(r) == 1),
        ("当前盘局分差 =2", lambda r: game_diff(r) == 2),
        ("当前盘局分差 ≥3", lambda r: game_diff(r) >= 3),
        ("第 2 盘内", lambda r: int(r.get("set_index") or 0) == 2),
        ("第 3 盘及以后", lambda r: int(r.get("set_index") or 0) >= 3),
        ("领先方发球", lambda r: r.get("leader_serving") is True),
        ("领先方接发球", lambda r: r.get("leader_serving") is False),
        ("发球方未知", lambda r: r.get("leader_serving") is None),
    ):
        line(label, stats(pick_per_match(samples, pred)))
    print()

    print("=== 2) 候选收紧规则（同一批入场时点上的反事实）===")
    candidates = [
        ("A 现状：领先盘 + 局分差≥1", lambda r: True),
        ("B 局分差≥2", lambda r: game_diff(r) >= 2),
        ("C 局分差≥3", lambda r: game_diff(r) >= 3),
        ("D 领先 2 盘（2-0）", lambda r: sets_key(r) == "2-0"),
        ("E 领先 ≥2 盘或局分差≥3",
         lambda r: sets_key(r) in ("2-0", "2-1") or game_diff(r) >= 3),
        ("F 领先盘 + 局分差≥2 + 发球方已知",
         lambda r: game_diff(r) >= 2 and r.get("leader_serving") is not None),
        ("G 领先盘 + 局分差≥1 + 第3盘后",
         lambda r: int(r.get("set_index") or 0) >= 3),
        ("H 领先盘 + 局分差≥2 + 第3盘后",
         lambda r: game_diff(r) >= 2 and int(r.get("set_index") or 0) >= 3),
    ]
    for label, pred in candidates:
        line(label, stats(pick_per_match(samples, pred)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
