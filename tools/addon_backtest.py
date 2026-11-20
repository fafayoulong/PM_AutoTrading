# -*- coding: utf-8 -*-
"""影子补单的**历史回溯**：用已有的入场后盘口路径，回测"挂低 X 美分、等 N 秒"。

为什么要它（2026-09-30 用户："影子补单大概要什么时候才能出结果" → "直接跑第二条"）
--------------------------------------------------------------------------------
线上那条 `dip_add_shadow`（`trades/dip_add_shadow.jsonl`）要攒到 **几百笔** 才能
判定"加单是不是正期望"（加单每单位收益的标准差约 0.39：赢 +16%、输 −100% 的
非对称；判 3pp 要 ~680 笔 ≈ 6-7 周，判 1.5pp 要 ~2,700 笔 ≈ 半年）。

但 `trades/position_paths.jsonl` 里已经有 **400+ 个入场的完整盘口序列**
（每 ~8 秒一条：买一/卖一/中间价/卖一档量）以及这些仓位的最终结算结果 ——
**同一套问题今天就能回溯**，不用等真金白银攒样本。

口径（重要，别读错）
  * **成交判定**：挂单价 P = 入场时卖一 − X，窗口 N 秒内**卖一曾经 ≤ P** 视为成交
    （有人把卖单砸到我们的买价上）。取成交价 = P（挂单按自己的价成交）。
  * **基准入场价**用路径第一份快照的**卖一**（吃单能成交的价）—— 不用账本里的
    `entry_price`，因为实盘账本记的是"委托限价"（见 PROJECT_DESIGN #120 的记账 bug）。
  * **8 秒采样**会漏掉更短的插针 → **成交率是保守估计**（真实会更高），
    所以在结论里要按"上界效应"读。
  * 只统计**主模拟盘**（`state.json`）的仓位：实盘与它高度重叠（同一信号两个账本
    各记一笔），两本混算会把样本量虚增一倍。`--ledger live` 可单独跑实盘做交叉验证。
  * **"这次回撤有没有伴随比分变化"只有 2026-09-29 之后的数据**（模型账本才记状态），
    更早的只能看价格、看不到当时比分 → 本工具**不做** news/no-news 拆分。

用法：
    python tools/addon_backtest.py --report                 # 主模拟盘
    python tools/addon_backtest.py --report --ledger live   # 实盘交叉验证
    python tools/addon_backtest.py --selftest               # 不联网校验统计逻辑
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PATHS_GLOB = "trades/position_paths.jsonl*"
STATE_PATH = "trades/state.json"
LIVE_PATH = "trades/live_orders.jsonl"


def load_outcomes(ledger: str = "paper") -> dict[str, dict]:
    """position_id → {won, entry_time, entry_price}（只用已结算的）。"""
    out: dict[str, dict] = {}
    if ledger == "paper":
        try:
            state = json.loads(open(STATE_PATH, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            return out
        for pid, v in (state.get("positions") or {}).items():
            if v.get("status") in ("CLOSED_WIN", "CLOSED_LOSS"):
                out[str(pid)] = {
                    "won": v["status"] == "CLOSED_WIN",
                    "entry_time": float(v.get("entry_time") or 0),
                    "entry_price": float(v.get("entry_price") or 0),
                }
        return out
    for line in open(LIVE_PATH, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") != "POSITION":
            continue
        d = row["data"]
        if d.get("status") in ("CLOSED_WIN", "CLOSED_LOSS"):
            out[str(d["position_id"])] = {
                "won": d["status"] == "CLOSED_WIN",
                "entry_time": float(d.get("opened_at") or 0),
                "entry_price": float(d.get("entry_price") or 0),
            }
    return out


def load_paths(ledger: str = "paper") -> dict[str, list[tuple]]:
    """position_id → [(age_s, best_bid, best_ask)]，按 age 升序。"""
    paths: dict[str, list[tuple]] = {}
    for path in sorted(glob.glob(PATHS_GLOB)):
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
                if row.get("ledger") != ledger:
                    continue
                pid = str(row.get("position_id") or "")
                if not pid:
                    continue
                bid, ask = row.get("best_bid"), row.get("best_ask")
                paths.setdefault(pid, []).append(
                    (float(row.get("age_s") or 0),
                     float(bid) if bid is not None else None,
                     float(ask) if ask is not None else None)
                )
    for pid in paths:
        paths[pid].sort(key=lambda item: item[0])
    return paths


def first_touch(path: list[tuple], target: float, window: float):
    """窗口内卖一首次 ≤ target 的 (age, ask)；没有则 None。"""
    for age, _bid, ask in path:
        if age > window:
            break
        if ask is not None and ask <= target + 1e-9:
            return age, ask
    return None


def simulate(paths: dict, outcomes: dict, deltas, windows) -> dict:
    """对每个 (挂低档位, 等待窗口) 统计：样本、成交、成交时点、成交那批的输赢。"""
    result: dict[tuple, dict] = {
        (delta, window): {"total": 0, "fills": [], "base_ret": []}
        for delta in deltas for window in windows
    }
    for pid, path in paths.items():
        outcome = outcomes.get(pid)
        if not outcome or not path:
            continue
        ask0 = next((ask for _age, _bid, ask in path if ask is not None), None)
        if not ask0 or ask0 <= 0:
            continue
        won = outcome["won"]
        base_ret = ((1.0 - ask0) / ask0) if won else -1.0
        for delta in deltas:
            target = round(ask0 - delta, 4)
            if target <= 0.02:
                continue
            for window in windows:
                stat = result[(delta, window)]
                stat["total"] += 1
                stat["base_ret"].append(base_ret)
                hit = first_touch(path, target, window)
                if hit is not None:
                    age, price = hit
                    add_ret = ((1.0 - price) / price) if won else -1.0
                    stat["fills"].append({"age": age, "price": price, "won": won,
                                          "add_ret": add_ret, "base_ret": base_ret})
    return result


def summarize(stat: dict) -> dict:
    """把一次 (档位, 窗口) 的原始记录折成可读指标。"""
    total = stat["total"]
    fills = stat["fills"]
    if not total:
        return {"n": 0}
    base_mean = statistics.mean(stat["base_ret"]) if stat["base_ret"] else 0.0
    if not fills:
        return {"n": total, "fill_rate": 0.0, "fills": 0,
                "base_roi": base_mean, "add_roi": None, "combined_roi": base_mean,
                "win_rate_filled": None, "median_age": None}
    add_mean = statistics.mean(f["add_ret"] for f in fills)
    win_rate_filled = sum(1 for f in fills if f["won"]) / len(fills)
    # 组合：所有仓位都按基础 1 单位算；成交的那些再加 1 单位（总投入 = n + 成交数）
    total_pnl = sum(stat["base_ret"]) + sum(f["add_ret"] for f in fills)
    combined_roi = total_pnl / (total + len(fills))
    return {
        "n": total,
        "fills": len(fills),
        "fill_rate": len(fills) / total,
        "win_rate_all": sum(1 for r in stat["base_ret"] if r >= 0) / total,
        "win_rate_filled": win_rate_filled,
        "base_roi": base_mean,
        "add_roi": add_mean,
        "combined_roi": combined_roi,
        "median_age": statistics.median(f["age"] for f in fills),
    }


def report(ledger: str = "paper", deltas=(0.01, 0.02, 0.03, 0.05, 0.08),
           windows=(60.0, 300.0, 600.0)) -> int:
    outcomes = load_outcomes(ledger)
    paths = load_paths(ledger)
    common = [pid for pid in paths if pid in outcomes]
    if not common:
        print(f"没有可用样本（{ledger}）：路径 {len(paths)} 个 / 已结算 {len(outcomes)} 个")
        return 1
    wins = sum(1 for pid in common if outcomes[pid]["won"])
    print(f"账本={ledger}：有路径且有结算的仓位 **{len(common)}** 个"
          f"（胜 {wins} / 负 {len(common)-wins} = {wins/len(common)*100:.1f}%）")
    result = simulate(paths, outcomes, deltas, windows)
    for window in windows:
        print(f"\n=== 入场后 {window/60:.0f} 分钟内挂单 ===")
        print("   挂低    成交率    成交那批胜率    加单每单位期望     组合ROI      只做基础ROI   中位成交时点")
        base_all = None
        for delta in deltas:
            s = summarize(result[(delta, window)])
            if not s["n"]:
                continue
            base_all = s["base_roi"]
            add = f"{s['add_roi']*100:+6.2f}%" if s["add_roi"] is not None else "   —"
            filled = f"{s['win_rate_filled']*100:5.1f}%" if s["win_rate_filled"] is not None else "  —"
            age = f"{s['median_age']:.0f}s" if s["median_age"] is not None else "—"
            print(f"   {delta*100:4.1f}美分  {s['fill_rate']*100:6.1f}%   {filled}        "
                  f"{add}        {s['combined_roi']*100:+6.2f}%      {s['base_roi']*100:+6.2f}%      {age}")
    print("\n读法：")
    print("  · 加单每单位期望 = 只在成交的那些上加 1 单位、持有到结算的收益率。")
    print("    这是核心指标：> 0 才说明「挂在下方等回撤」本身有利可图。")
    print("  · 组合ROI = (基础仓位的总和 + 加单的总和) / (基础笔数 + 加单笔数)，")
    print("    与「只做基础ROI」直接对比 —— 前者更高才说明加单改善了整套策略。")
    print("  · 成交那批胜率 低于全体胜率 → 逆向选择（跌得深的更容易是坏单）。")
    print("  · 8 秒采样漏掉更短的插针 → 成交率是保守估计，真实会更高。")
    return 0


def selftest() -> int:
    """用合成路径校验统计逻辑。"""
    # A：入场 0.80，10 秒后跌到 0.78，最终赢
    # B：入场 0.80，一路不跌，最终赢
    # C：入场 0.80，立刻跌到 0.70，最终输
    paths = {
        "A": [(0, 0.79, 0.80), (10, 0.77, 0.78), (60, 0.85, 0.86)],
        "B": [(0, 0.79, 0.80), (30, 0.80, 0.81), (60, 0.90, 0.91)],
        "C": [(0, 0.79, 0.80), (5, 0.69, 0.70), (60, 0.10, 0.12)],
    }
    outcomes = {"A": {"won": True, "entry_time": 0, "entry_price": 0.8},
                "B": {"won": True, "entry_time": 0, "entry_price": 0.8},
                "C": {"won": False, "entry_time": 0, "entry_price": 0.8}}
    res = simulate(paths, outcomes, (0.02,), (60.0,))
    s = summarize(res[(0.02, 60.0)])
    ok1 = s["n"] == 3 and s["fills"] == 2 and abs(s["fill_rate"] - 2 / 3) < 1e-9
    ok2 = abs(s["win_rate_filled"] - 0.5) < 1e-9
    # 加单：A 赢（0.78 → +28.2%），C 输（−100%）→ 平均 −35.9%
    expected_add = ((1 - 0.78) / 0.78 - 1.0) / 2
    ok3 = abs(s["add_roi"] - expected_add) < 1e-9
    base = ((1 - 0.8) / 0.8 + (1 - 0.8) / 0.8 - 1.0) / 3
    ok4 = abs(s["base_roi"] - base) < 1e-9
    ok5 = s["median_age"] == 7.5
    checks = (("样本/成交数", ok1), ("成交那批胜率", ok2), ("加单期望", ok3),
              ("基础 ROI", ok4), ("中位成交时点", ok5))
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(ok for _, ok in checks) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ledger", choices=("paper", "live"), default="paper")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return report(args.ledger)


if __name__ == "__main__":
    raise SystemExit(main())
