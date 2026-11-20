# -*- coding: utf-8 -*-
"""离线回放：早期那套网球参数（价格上限 0.92 + 没有价格下限）在今天的数据上还剩多少边际。

背景（2026-10-09 用户："最开始只玩网球时收益还可以，用的是早期参数"）
--------------------------------------------------------------------------------
PRODUCT_DESIGN.md v1.0（2026-09-03）里的原始网球参数是：
    max_entry_price     = 0.92      （只买还剩 >=3% 毛利的盘）
    min_expected_profit = 0.03
    live_min_entry_price = **不存在**（没有价格下限闸门）
    单笔 500 USDC 名义金额
2026-09-12 的 0.30 买入事故之后加了下限闸门（默认 0.60），上限后来也放宽到 0.99。
问题是：那 09-09~09-11 的 +17.17% 里，有多少是真边际、有多少是 bug 红利？

口径（重要）
  * 样本 = `trades/tennis_model_ledger.jsonl*`（2026-09-29 上线，~30 秒一条网球快照，
    每条都带实时盘口 + `heuristic_lock`（= 现行锁定规则 `evaluate_tennis_lock`））。
  * **每场只取第一个 `heuristic_lock=True` 的快照** —— 对应实盘"每场只触发一次、
    触发后不再重试"的语义，所以闸门对比用的是同一个入场时点。
  * 结算 = 复用 `tools/tennis_model_ledger.resolve_results`（Gamma + 同一份缓存
    `trades/tennis_model_results.json`），只认结算价 0/1，拿不到就跳过。
  * ROI = 等额 1 单位名义金额买入领先方、持有到结算：赢 `(1-cost)/cost`，输 `-1`。
    `--buffer-ticks 1` 可按"下单价 = 卖一 + 1 tick"复算（默认 0，即按卖一）。

局限（照实说）
  * 账本从 **2026-09-29** 才开始，回放窗口约 10 天，不是 09-12 之后的全窗口。
  * 快照 ~30 秒一格，入场价是"锁定后第一格"的价格，与真实触发瞬间可能差几秒。
  * 只能评估**现行规则**的子集（更严的规则如"领先 >=2 局"），
    评估不了更松的规则（账本里没有那些时点）。

用法：
    python tools/tennis_gate_replay.py [--no-network] [--buffer-ticks 1]
只读（唯一写入是结算缓存 `trades/tennis_model_results.json`）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_model_ledger as ledger  # noqa: E402

LEDGER_GLOB = "trades/tennis_model_ledger.jsonl*"


def load_lock_entries() -> list[dict]:
    """每场一条：第一个 heuristic_lock 快照。"""
    rows: list[dict] = []
    for path in sorted(glob.glob(LEDGER_GLOB)):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not row.get("heuristic_lock"):
                    continue
                if row.get("best_ask") is None or not row.get("token_id"):
                    continue
                rows.append(row)
    rows.sort(key=lambda r: float(r.get("t") or 0))
    first: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("match_id") or "")
        if key and key not in first:
            first[key] = row
    return list(first.values())


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math_sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, center - half), min(1.0, center + half))


def math_sqrt(x: float) -> float:
    return x ** 0.5 if x > 0 else 0.0


def boot_ci(vals: list[float], iters: int = 4000, seed: int = 11) -> tuple[float, float]:
    if len(vals) < 3:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    n = len(vals)
    means = [sum(vals[rnd.randrange(n)] for _ in range(n)) / n for _ in range(iters)]
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters) - 1]


def evaluate(rows: list[dict], cache: dict, buffer_ticks: float = 0.0) -> dict:
    settled = 0
    wins = 0
    rets: list[float] = []
    costs: list[float] = []
    unresolved = 0
    for row in rows:
        won = ledger._won(cache, row)
        if won is None:
            unresolved += 1
            continue
        ask = float(row.get("best_ask"))
        tick = float(row.get("tick_size") or 0.01)
        cost = ask + buffer_ticks * tick
        if not (0.0 < cost < 1.0):
            continue
        settled += 1
        if won:
            wins += 1
            rets.append((1.0 - cost) / cost)
        else:
            rets.append(-1.0)
        costs.append(cost)
    if not settled:
        return {"n": 0, "unresolved": unresolved}
    lo, hi = wilson(wins, settled)
    clo, chi = boot_ci(rets)
    return {
        "n": settled,
        "unresolved": unresolved,
        "wins": wins,
        "win_rate": wins / settled,
        "win_ci": (lo, hi),
        "avg_cost": statistics.mean(costs),
        "roi": statistics.mean(rets),
        "roi_ci": (clo, chi),
    }


def print_stat(label: str, st: dict, note: str = "") -> None:
    if not st or not st.get("n"):
        print("  %-26s %4d 笔   (无可结算样本)" % (label, 0))
        return
    lo, hi = st["win_ci"]
    clo, chi = st["roi_ci"]
    ci = ("[%+.1f%%,%+.1f%%]" % (clo * 100, chi * 100)
          if chi == chi else "n/a")
    print("  %-26s n=%3d  win=%5.1f%% (%.1f~%.1f)  avg=%.3f  ROI=%+7.2f%% %s%s"
          % (label, st["n"], st["win_rate"] * 100, lo * 100, hi * 100,
             st["avg_cost"], st["roi"] * 100, ci,
             ("  " + note) if note else ""))


BANDS = [(0.00, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.86),
         (0.86, 0.92), (0.92, 0.97), (0.97, 1.01)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true", help="只用结算缓存，不联网补查")
    ap.add_argument("--buffer-ticks", type=float, default=0.0,
                    help="下单价 = 卖一 + N×tick（实盘默认 1）")
    args = ap.parse_args()

    entries = load_lock_entries()
    if not entries:
        print("没有可用的锁定快照（tennis_model_ledger 可能还没采到）")
        return 1
    cache = ledger.resolve_results(entries, allow_network=not args.no_network)
    resolved = sum(1 for r in entries if ledger._won(cache, r) is not None)
    print("锁定样本 %d 场（每场取第一个锁定快照），已结算 %d 场，"
          "结算缓存 %s" % (len(entries), resolved, len([k for k in cache if k != "_notes"])))
    if args.buffer_ticks:
        print("下单价按「卖一 + %g tick」复算" % args.buffer_ticks)
    print()

    b = args.buffer_ticks

    def ask_of(r: dict) -> float:
        return float(r["best_ask"])

    print("=== 1) 价格闸门对比（锁定规则不变 = 现行「盘分领先 + 当前盘领先≥1 局」）===")
    configs = [
        ("A 原始：≤0.92，无下限", lambda r: ask_of(r) <= 0.92),
        ("B 现行：0.60~0.99", lambda r: 0.60 <= ask_of(r) <= 0.99),
        ("C 只去下限：≤0.99", lambda r: ask_of(r) <= 0.99),
        ("D 只去上限：0.60~0.92", lambda r: 0.60 <= ask_of(r) <= 0.92),
    ]
    for label, pred in configs:
        print_stat(label, evaluate([r for r in entries if pred(r)], cache, b))
    print()
    print("  —— 闸门新增 / 砍掉的两组单子单独看 ——")
    print_stat("E 被 0.60 下限挡住(<0.60)",
               evaluate([r for r in entries if ask_of(r) < 0.60], cache, b))
    print_stat("F 被 0.92 上限挡住(0.92~0.99)",
               evaluate([r for r in entries if 0.92 < ask_of(r) <= 0.99], cache, b))
    print()

    print("=== 2) 入场价分档（现行规则，全部锁定样本）===")
    for lo, hi in BANDS:
        sel = [r for r in entries if lo <= ask_of(r) < hi]
        print_stat("[%.2f,%.2f)" % (lo, hi), evaluate(sel, cache, b))
    print()

    print("=== 3) 规则 x 价格 交叉（领先≥2 局是更严的子集）===")
    for rule_label, rule_pred in (
        ("现行规则 领先≥1 局", lambda r: True),
        ("收紧规则 领先≥2 局",
         lambda r: (int(r.get("leader_games") or 0) - int(r.get("underdog_games") or 0)) >= 2),
    ):
        sub = [r for r in entries if rule_pred(r)]
        print("  -- %s（%d 场锁定）" % (rule_label, len(sub)))
        print_stat("   A ≤0.92 无下限", evaluate([r for r in sub if ask_of(r) <= 0.92], cache, b))
        print_stat("   B 0.60~0.99", evaluate([r for r in sub if 0.60 <= ask_of(r) <= 0.99], cache, b))
        print_stat("   C ≤0.99 无下限", evaluate([r for r in sub if ask_of(r) <= 0.99], cache, b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
