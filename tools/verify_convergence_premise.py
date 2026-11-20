# -*- coding: utf-8 -*-
"""验证策略前提：**我们买入时，市场还没收敛完吗？**

前提的精确说法不是"我们赢得比价格多"，而是"入场那一刻的价格 < 当时的真实概率"。
前者只能用二元胜负判（需要约 400 笔才够 95% 置信），后者可以用**价格路径**判
（连续量，功效高得多）。

方法（横截面差分）：
  1. 每笔仓位取「入场时的模型边际」= model_prob − entry_price
     （足球用 Poisson 模型的 win_probability，网球用 DP 模型的 model_win_prob）
  2. 从 position_paths.jsonl 取这笔仓位**入场之后**的价格路径，算入场后 30/60/120 秒
     的价格漂移
  3. 按边际大小分组，比较各组的漂移

为什么必须分组比较、不能直接看"价格涨没涨"：
    锁定盘的真实概率**本来就会随时间上涨**（时间在流逝、剩余风险在缩小）。
    即使市场完全有效，入场后价格也会涨 —— 所以"观察到涨"不构成任何证据。
    但两个边际组的比赛状态分布相近、**时间衰减的贡献大致相同**，
    两组漂移的**差**就只剩下"市场在纠正自己的定价"这一项。

局限（照实写）：
  * 快照里没有当时的比分，所以窗口内如果进了球，价格会剧烈变动。
    这会给两侧都加噪声（不系统偏向某一组），所以看**中位数**、并同时报均值。
  * 不要求模型概率绝对准确，只要求"边际的排序"有效 —— 这个假设弱得多。

用法：
    python tools/verify_convergence_premise.py [--windows 30,60,120] [--min-bucket 8]
只读，不写任何文件。
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics


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


def signal_index():
    """event_id → 入场时的模型边际所需信息。"""
    out = {}
    for row in load("signal_events.jsonl"):
        if row.get("reason") != "OPENED":
            continue
        det = row.get("details") or {}
        prob = det.get("win_probability")          # 足球：Poisson 模型
        if prob is None:
            prob = det.get("model_win_prob")       # 网球：DP 模型
        if prob is None:
            continue
        eid = row.get("event_id")
        if eid:
            out[eid] = {
                "sport": row.get("sport") or det.get("sport"),
                "model_prob": float(prob),
                "entry_price": det.get("live_entry_price") or row.get("entry_price"),
            }
    return out


def mid_of(row):
    mid = row.get("mid")
    if mid:
        return float(mid)
    bid, ask = row.get("best_bid"), row.get("best_ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return None


def build(windows):
    """每个仓位一条：边际 + 各窗口的入场后漂移。"""
    sigs = signal_index()
    series = collections.defaultdict(list)
    for row in load("position_paths.jsonl"):
        if row.get("one_sided"):
            continue
        pid = row.get("position_id")
        if pid:
            series[pid].append(row)

    rows = []
    for pid, snaps in series.items():
        if len(snaps) < 5:
            continue
        snaps.sort(key=lambda r: float(r.get("age_s") or 0))
        base = snaps[0]
        base_mid = mid_of(base)
        if base_mid is None or base_mid <= 0:
            continue
        eid = base.get("event_id")
        info = sigs.get(eid)
        if not info or not info["entry_price"]:
            continue
        edge = info["model_prob"] - float(info["entry_price"])
        item = {
            "position_id": pid,
            "sport": str(info["sport"] or base.get("sport") or "?"),
            "edge": edge,
            "entry_price": float(info["entry_price"]),
            "base_mid": base_mid,
            "n_snaps": len(snaps),
        }
        for w in windows:
            hit = next((s for s in snaps if float(s.get("age_s") or 0) >= w), None)
            m = mid_of(hit) if hit else None
            item[f"drift_{w}"] = (m - base_mid) if m is not None else None
        rows.append(item)
    return rows


def bucket(edge):
    if edge >= 0.05:
        return "≥+5pp"
    if edge >= 0.02:
        return "+2~5pp"
    if edge >= 0.0:
        return "0~+2pp"
    return "<0"


ORDER = ["≥+5pp", "+2~5pp", "0~+2pp", "<0"]


def rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def split_stats(rows, window, label_hi="高边际一半", label_lo="低边际一半"):
    """按边际中位数切两半 —— 足球的闸门让边际全为正，分类桶会只剩一组，
    但"相对高低"永远存在，而且两半都是锁定盘，时间衰减的贡献仍然可比。"""
    key = f"drift_{window}"
    usable = [r for r in rows if r.get(key) is not None]
    if len(usable) < 8:
        return None
    med = statistics.median(r["edge"] for r in usable)
    hi = [r for r in usable if r["edge"] >= med]
    lo = [r for r in usable if r["edge"] < med]
    if len(hi) < 4 or len(lo) < 4:
        return None
    dh = [r[key] for r in hi]
    dl = [r[key] for r in lo]
    diff = statistics.mean(dh) - statistics.mean(dl)
    se = math.sqrt(statistics.pvariance(dh) / len(dh) + statistics.pvariance(dl) / len(dl))
    z = diff / se if se > 0 else 0.0
    rho = pearson(rank([r["edge"] for r in usable]), rank([r[key] for r in usable]))
    return {
        "n_hi": len(hi), "n_lo": len(lo), "median_edge": med,
        "hi_mean": statistics.mean(dh), "lo_mean": statistics.mean(dl),
        "diff": diff, "se": se, "z": z, "rho": rho,
    }


def summarize(label, rows, window):
    key = f"drift_{window}"
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "mean": statistics.mean(vals),
        "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        "vals": vals,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="30,60,120")
    ap.add_argument("--min-bucket", type=int, default=8)
    args = ap.parse_args()
    windows = [int(w) for w in str(args.windows).split(",") if w.strip()]

    rows = build(windows)
    print("=" * 100)
    print("A) 数据覆盖")
    if not rows:
        print("   没有可用样本（需要：signal_events 里有 OPENED + 模型概率，")
        print("   position_paths 里有该仓位的入场后快照）")
        return 1
    by_sport = collections.Counter(r["sport"] for r in rows)
    print(f"   可用仓位 {len(rows)} 笔；运动分布 {dict(by_sport)}")
    print(f"   入场后快照点中位 {statistics.median(r['n_snaps'] for r in rows):.0f} 个")
    edges = [r["edge"] for r in rows]
    print(f"   入场时模型边际：中位 {statistics.median(edges)*100:+.2f}pp "
          f"均值 {statistics.mean(edges)*100:+.2f}pp  为正的占 "
          f"{sum(1 for e in edges if e > 0)/len(edges)*100:.0f}%")
    print()

    for window in windows:
        print("-" * 100)
        print(f"B{window}) 入场后 {window} 秒的价格漂移，按「入场时模型边际」分组")
        print("   {:<10} {:>4} {:>13} {:>13} {:>13}".format(
            "边际分组", "n", "漂移中位", "漂移均值", "漂移均值(pp)"))
        groups = collections.defaultdict(list)
        for r in rows:
            groups[bucket(r["edge"])].append(r)
        stats = {}
        for key in ORDER:
            g = groups.get(key) or []
            if len(g) < args.min_bucket:
                continue
            s = summarize(key, g, window)
            if not s:
                continue
            stats[key] = s
            print("   {:<10} {:>4} {:>+12.4f} {:>+13.4f} {:>+12.2f}".format(
                key, s["n"], s["median"], s["mean"], s["mean"] * 100))
        high, low = stats.get("≥+5pp"), stats.get("<0")
        if high and low:
            diff = high["mean"] - low["mean"]
            se = math.sqrt((high["stdev"] ** 2) / high["n"] + (low["stdev"] ** 2) / low["n"])
            z = diff / se if se > 0 else 0.0
            print()
            print("   ★ 极端组对比（≥+5pp − <0）：{:+.4f}（{:+.2f}pp） z={:+.2f}".format(
                diff, diff * 100, z))
        # 分组桶在足球上会只剩一组（闸门要求边际≥2pp），所以这里再给一个
        # "按中位数切两半"的版本 + 秩相关 —— 它们用得上全部样本。
        split = split_stats(rows, window)
        if split:
            print()
            print("   ★ 按边际中位数切两半（中位边际 = {:+.2f}pp）：".format(
                split["median_edge"] * 100))
            print("       高边际一半(n={}) 漂移均值 {:+.4f}（{:+.2f}pp）".format(
                split["n_hi"], split["hi_mean"], split["hi_mean"] * 100))
            print("       低边际一半(n={}) 漂移均值 {:+.4f}（{:+.2f}pp）".format(
                split["n_lo"], split["lo_mean"], split["lo_mean"] * 100))
            print("       差值 = {:+.4f}（{:+.2f}pp）  标准误 {:.4f}  z={:+.2f}".format(
                split["diff"], split["diff"] * 100, split["se"], split["z"]))
            print("       秩相关 ρ(边际, 漂移) = {:+.3f}".format(split["rho"]))
            if split["z"] > 1.96 and split["rho"] > 0:
                print("       → 边际越大漂移越大：市场确实在买入后继续收敛（前提成立）")
            elif split["z"] < -1.96 and split["rho"] < 0:
                print("       → 反向前提（边际越大反而跌）")
            else:
                print("       → 两者没有显著关系：这批数据判不出「市场在补收敛」")
    print()
    print("=" * 100)
    print("读法：正的「高−低」= 边际越大的单子，买入后价格涨得越多 =")
    print("      市场在我们买入之后仍在继续收敛（前提成立）。")
    print("      注意：时间衰减两组都有，被差分扣掉了；残留的正差就是「市场在补课」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
