# -*- coding: utf-8 -*-
"""网球胜率模型的复盘：模型准不准、比市场准在哪。

数据来源（都在项目根目录的 trades/ 下）：
  * signal_events.jsonl：OPENED 网球信号 + details.model_win_prob 等状态字段
  * paper_trades.jsonl / live_orders.jsonl：结果（pnl），按 token 去重后配对

输出三块：
  A. 样本量（带模型概率的已结算记录）
  B. 模型校准：按模型概率分档，对比「模型预测 vs 实际胜率」（带 Wilson 区间）
  C. 模型 vs 市场：model_win_prob − 入场价 的分布，以及按这个边际分组的实际 ROI

用法（VPS 或本地项目根目录）：
    python tools/tennis_model_review.py [--min-samples 40]
返回码：0 = 正常；3 = 样本还不够（供定时任务判断「要不要出结论」）。

只读，不写任何文件。
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics

MIN_SAMPLES = 40


def load(name):
    path = os.path.join("trades", name)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def wilson(wins, n, z=1.96):
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, center - half), min(1.0, center + half))


def build():
    """返回每个 token 一条：模型概率 / 入场价 / 结果（按 token 去重）。"""
    signals = {}
    for row in load("signal_events.jsonl"):
        if row.get("reason") != "OPENED":
            continue
        det = row.get("details") or {}
        if str(det.get("sport_tag") or "").lower() != "tennis":
            continue
        if det.get("model_win_prob") is None:
            continue
        token = str(det.get("token_id") or "")
        if token:
            signals[token] = det

    outcomes = {}
    for row in load("paper_trades.jsonl"):
        token = str(row.get("token_id") or "")
        if token and row.get("pnl") is not None:
            outcomes.setdefault(token, row)
    for row in load("live_orders.jsonl"):
        if row.get("kind") != "POSITION":
            continue
        data = row["data"]
        token = str(data.get("token_id") or "")
        if token and data.get("pnl") is not None:
            outcomes.setdefault(token, data)

    rows = []
    for token, det in signals.items():
        pos = outcomes.get(token)
        if not pos:
            continue
        price = float(pos.get("entry_price") or 0)
        cost = float(pos.get("cost") or 0) or price
        if price <= 0 or cost <= 0:
            continue
        rows.append({
            "token": token,
            "model": float(det["model_win_prob"]),
            "price": price,
            "win": float(pos["pnl"]) > 0,
            "roi": float(pos["pnl"]) / cost,
        })
    return rows


def band_prob(p):
    for lo in (0.95, 0.90, 0.85, 0.80):
        if p >= lo:
            return "{:.2f}+".format(lo)
    return "<0.80"


def report_line(label, n, actual, predicted):
    lo, hi = wilson(round(actual * n), n)
    gap_lo, gap_hi = lo - predicted, hi - predicted
    verdict = "模型偏低" if gap_lo > 0 else ("模型偏高" if gap_hi < 0 else "分不出")
    print("  {:<12} {:>4} {:>9.1f}% {:>9.1f}% {:>+9.1f}pp  [{:>5.1f},{:>5.1f}]  {}".format(
        label, n, actual * 100, predicted * 100, (actual - predicted) * 100,
        gap_lo * 100, gap_hi * 100, verdict))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    args = ap.parse_args()

    rows = build()
    n = len(rows)
    print("=" * 96)
    print("A) 样本量")
    print("   带模型概率且已结算的网球记录：{} 条（按 token 去重）".format(n))
    if n < args.min_samples:
        print("   [!] 少于门槛 {} 条 —— 现在下结论会被小样本噪声带偏。".format(args.min_samples))
    print()

    print("B) 模型校准（模型说多少，实际做到多少）")
    print("  {:<12} {:>4} {:>10} {:>10} {:>12}  {}".format(
        "模型概率档", "n", "实际胜率", "模型预测", "偏差", "95% 区间"))
    groups = collections.defaultdict(list)
    for r in rows:
        groups[band_prob(r["model"])].append(r)
    keys = sorted(groups, key=lambda k: -float(k.rstrip("+")) if k != "<0.80" else 0)
    for key in keys:
        g = groups[key]
        if len(g) < 5:
            continue
        wins = sum(1 for r in g if r["win"])
        report_line(key, len(g), wins / len(g), statistics.mean(r["model"] for r in g))
    if rows:
        wins = sum(1 for r in rows if r["win"])
        print()
        report_line("总体", n, wins / n, statistics.mean(r["model"] for r in rows))
    print()

    print("C) 模型 vs 市场价（model_win_prob − 入场价；正数 = 模型认为市场低估了）")
    edges = [r["model"] - r["price"] for r in rows]
    if edges:
        print("   中位 {:+.2f}pp | 均值 {:+.2f}pp | 为正的比例 {:.0f}%".format(
            statistics.median(edges) * 100, statistics.mean(edges) * 100,
            sum(1 for e in edges if e > 0) / len(edges) * 100))
        print()
        print("  {:<22} {:>4} {:>9} {:>10}  {}".format(
            "按边际分组", "n", "实际胜率", "平均 ROI", "提示"))
        for label, cond in (("边际 ≥ +3pp", lambda e: e >= 0.03),
                            ("0 ~ +3pp", lambda e: 0 <= e < 0.03),
                            ("−3 ~ 0pp", lambda e: -0.03 <= e < 0),
                            ("边际 < −3pp", lambda e: e < -0.03)):
            g = [r for r, e in zip(rows, edges) if cond(e)]
            if len(g) < 5:
                continue
            w = sum(1 for r in g if r["win"])
            roi = statistics.mean(r["roi"] for r in g)
            hint = ""
            if roi > 0 and w / len(g) > statistics.mean(r["price"] for r in g):
                hint = "<-- 值得考虑接进闸门"
            print("  {:<22} {:>4} {:>8.1f}% {:>+9.2f}%  {}".format(
                label, len(g), w / len(g) * 100, roi * 100, hint))
    print()
    print("=" * 96)
    if n < args.min_samples:
        print("结论：样本还不够，本次只记录，不下判断。")
        return 3
    print("结论：样本已够，请按上面三张表给出「模型准不准 / 该不该接进闸门」的判断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
