# -*- coding: utf-8 -*-
"""我们的网球胜率模型 vs 市场定价：谁把赢面预测得更准。

为什么用 Brier 而不是 ROI
-------------------------
ROI 里混着"我们只在哪一档下单""价差""下单价缓冲"这些交易层面的东西。
要回答"模型是否跑输市场定价"，应该直接比**同一时点、同一场比赛**的赢面预测：

    y        = 领先方最终是否赢（Gamma 结算价，只认 0/1）
    p_model  = src/tennis_model.py 的 DP 模型概率（账本字段 model_p）
    p_market = 同一时点的盘口中间价 mid（缺失时退回卖一）

指标：Brier = mean((p - y)^2)、LogLoss = -mean(y·ln p + (1-y)·ln(1-p))。
**越低越准。** 差值（市场 − 模型）用**按比赛聚类的 bootstrap**给区间：
同一场比赛的连续快照高度相关，按 tick 独立重采样会把区间算得虚窄。

样本：`trades/tennis_model_ledger.jsonl*`（2026-09-29 起，~30 秒一条快照）。
为免长比赛主导样本，每场最多均匀取 `--per-match` 条（默认 12）。

用法：
    python tools/tennis_model_vs_market.py [--per-match 12] [--no-network]
只读（唯一写入是共用的结算缓存 trades/tennis_model_results.json）。
"""

from __future__ import annotations

import argparse
import collections
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_model_ledger as ledger  # noqa: E402


def market_price(row: dict) -> float | None:
    mid = row.get("mid")
    if mid is not None:
        try:
            value = float(mid)
        except (TypeError, ValueError):
            value = None
        if value is not None and 0.0 < value < 1.0:
            return value
    ask = row.get("best_ask")
    if ask is not None:
        try:
            value = float(ask)
        except (TypeError, ValueError):
            return None
        if 0.0 < value < 1.0:
            return value
    return None


def subsample(rows: list[dict], per_match: int) -> list[dict]:
    by_match: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_match[str(row.get("match_id") or "")].append(row)
    out: list[dict] = []
    for _mid, group in by_match.items():
        group.sort(key=lambda r: float(r.get("t") or 0))
        if len(group) <= per_match:
            out.extend(group)
            continue
        step = len(group) / per_match
        out.extend(group[int(i * step)] for i in range(per_match))
    return out


def brier(samples: list[tuple[float, float]]) -> float:
    return sum((p - y) ** 2 for p, y in samples) / len(samples)


def logloss(samples: list[tuple[float, float]]) -> float:
    total = 0.0
    tiny = 1e-6
    for p, y in samples:
        p = min(max(p, tiny), 1 - tiny)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(samples)


def cluster_bootstrap(diffs_by_match: dict[str, list[float]], iters: int = 4000,
                      seed: int = 5) -> tuple[float, float]:
    keys = list(diffs_by_match)
    if len(keys) < 5:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        pick = [keys[rnd.randrange(len(keys))] for _ in range(len(keys))]
        vals = [v for k in pick for v in diffs_by_match[k]]
        means.append(sum(vals) / len(vals))
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters) - 1]


def verdict(lo: float, hi: float) -> str:
    if hi != hi:
        return "样本不足，判不出"
    if lo > 0:
        return "模型显著优于市场"
    if hi < 0:
        return "模型显著跑输市场"
    return "判不出（区间跨 0）"


def report(samples: list[dict], title: str) -> None:
    if len(samples) < 10:
        print("  %s：样本 %d 条，不足" % (title, len(samples)))
        return
    model_pairs = [(s["model"], s["y"]) for s in samples]
    market_pairs = [(s["market"], s["y"]) for s in samples]
    diffs: dict[str, list[float]] = collections.defaultdict(list)
    for s in samples:
        diffs[s["match"]].append((s["market"] - s["y"]) ** 2 - (s["model"] - s["y"]) ** 2)
    lo, hi = cluster_bootstrap(diffs)
    flat = [v for vs in diffs.values() for v in vs]
    print("  %-22s n=%5d  场=%3d  Brier 模型=%.4f 市场=%.4f  差(市场−模型)=%+.4f [%+.4f,%+.4f]  → %s"
          % (title, len(samples), len(diffs), brier(model_pairs), brier(market_pairs),
             sum(flat) / len(flat), lo, hi, verdict(lo, hi)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-match", type=int, default=12)
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()

    rows = ledger.load_rows()
    if not rows:
        print("账本为空")
        return 1
    picked = subsample(rows, max(1, args.per_match))
    cache = ledger.resolve_results(picked, allow_network=not args.no_network)

    samples: list[dict] = []
    for row in picked:
        won = ledger._won(cache, row)
        if won is None:
            continue
        model_p = row.get("model_p")
        if model_p is None:
            continue
        market = market_price(row)
        if market is None:
            continue
        samples.append({
            "y": 1.0 if won else 0.0,
            "model": float(model_p),
            "market": market,
            "match": str(row.get("match_id") or ""),
            "spread": row.get("spread"),
            "lead": int(row.get("leader_sets") or 0) - int(row.get("underdog_sets") or 0),
            "set_index": int(row.get("set_index") or 0),
        })
    print("账本 %d 条 → 每场最多 %d 条 → 有结算且有盘口价 %d 条（%d 场）"
          % (len(rows), args.per_match, len(samples), len({s["match"] for s in samples})))
    print("Brier/LogLoss 越低越准；差 = 市场的 Brier − 模型的 Brier，**正数 = 模型更准**")
    print()

    if not samples:
        print("没有可比较的样本")
        return 1
    mp = [(s["model"], s["y"]) for s in samples]
    kp = [(s["market"], s["y"]) for s in samples]
    print("=== 总体 ===")
    print("  模型   Brier=%.4f  LogLoss=%.4f" % (brier(mp), logloss(mp)))
    print("  市场   Brier=%.4f  LogLoss=%.4f" % (brier(kp), logloss(kp)))
    print()
    print("=== 按盘口价分档（模型的相对表现）===")
    for lo, hi in ((0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 1.01)):
        report([s for s in samples if lo <= s["market"] < hi], "[%.2f,%.2f)" % (lo, hi))
    print()
    print("=== 按领先盘数分档 ===")
    for lead in (-1, 0, 1, 2):
        report([s for s in samples if s["lead"] == lead], "领先盘差 %+d" % lead)
    print()
    print("=== 只看窄价差（spread<=0.02，盘口最可信）===")
    report([s for s in samples if s["spread"] is not None and float(s["spread"]) <= 0.02],
           "spread<=0.02")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
