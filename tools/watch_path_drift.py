# -*- coding: utf-8 -*-
"""入场前价格漂移的成分拆解：**多少来自"比分变了"，多少来自"什么都没发生"**。

为什么要它（2026-09-28 用户："我怀疑比市场慢是因为 ESPN 太慢"）
--------------------------------------------------------------
换数据源之前必须先知道"我们晚"到底晚在哪一段。这块只看**入场之前**的价格路径
（`trades/watch_paths.jsonl`，2026-09-28 新增的入场前采集），把窗口内的价格变化
按"采样那一刻盘口状态有没有变"拆成两半：

  * **news 段**：状态说明（`state_note`，形如 `SET_LEAD sets 1-0; games 1-0`）变了的
    采样区间 → 这段的钱是"比分/盘分推进"推动的，比源快慢直接相关；
  * **no-news 段**：状态没变、价格却动了 → 这段**不是源慢**，是市场自己在收敛
    （更多人挂单、价差收窄、或者用了比逐局更细的信息）。

实测第一例（2026-09-28 11:37-11:40，Zheng Wushuang vs Mei Yamaguchi）：
卖一 0.82 → 0.86 的 4 分钱全部发生在 `games 0-0` 没变的那一段，于是一个很自然的
结论就出来了 —— **换一个快 10 秒的比分源，这 4 分钱一分也拿不回来**。

用法：
    python tools/watch_path_drift.py                # 全部样本
    python tools/watch_path_drift.py --hours 24     # 只看最近 24 小时入场的仓位
    python tools/watch_path_drift.py --selftest     # 不读真实文件，校验拆解逻辑
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WATCH_PATH = "trades/watch_paths.jsonl"
PAPER_PATH = "trades/paper_trades.jsonl"
LIVE_PATH = "trades/live_orders.jsonl"


def decompose_window(samples: list[dict], entry_time: float) -> dict | None:
    """把「第一条采样 → 入场」这段拆成 news / no-news 两部分。

    `samples` 必须是同一个盘口、按时间升序的采样（含 `t`/`mid`/`ask`/`state_note`）。
    纯函数，方便单测。
    """
    window = [s for s in samples if float(s.get("t") or 0) <= entry_time]
    if len(window) < 2:
        return None
    first, last = window[0], window[-1]

    def price(row):
        for key in ("mid", "ask", "best_ask"):
            value = row.get(key)
            if value is not None:
                return float(value)
        return None

    p_first, p_last = price(first), price(last)
    if p_first is None or p_last is None:
        return None

    no_news = 0.0
    news = 0.0
    score_changes = 0
    for prev, cur in zip(window, window[1:]):
        before, after = price(prev), price(cur)
        if before is None or after is None:
            continue
        delta = after - before
        if str(prev.get("state_note") or "") != str(cur.get("state_note") or ""):
            news += delta
            score_changes += 1
        else:
            no_news += delta
    return {
        "token_id": first.get("token_id"),
        "game_id": first.get("game_id"),
        "outcome": first.get("outcome"),
        "samples": len(window),
        "span_seconds": float(last["t"]) - float(first["t"]),
        "pre_move": p_last - p_first,
        "news_move": news,
        "no_news_move": no_news,
        "score_changes": score_changes,
    }


def load_positions(hours: float | None) -> list[dict]:
    """两个账本的已开仓位（token_id + 入场时刻），用于定位"入场前"这个时点。"""
    cutoff = time.time() - hours * 3600 if hours else 0.0
    # 两个日志都是**追加快照**：同一个 position_id 会先写一条 OPEN、结算时再追一条
    # CLOSED（实盘还有 ORDER/DEDUPE 快照）。所以必须按 position_id 去重，
    # 否则同一笔仓位会被算两次，明细里就会出现"一模一样的两行"。
    out: dict[tuple[str, str], dict] = {}
    if os.path.exists(PAPER_PATH):
        with open(PAPER_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = str(r.get("token_id") or "")
                when = float(r.get("entry_time") or 0)
                if token and when >= cutoff:
                    out[("paper", str(r.get("position_id") or f"{token}:{when}"))] = {
                        "ledger": "paper", "token_id": token, "entry_time": when,
                        "team": r.get("team"), "sport": r.get("sport"),
                    }
    if os.path.exists(LIVE_PATH):
        with open(LIVE_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("kind") != "POSITION":
                    continue
                data = r.get("data") or {}
                token = str(data.get("token_id") or "")
                when = float(data.get("opened_at") or 0)
                if token and when >= cutoff:
                    out[("live", str(data.get("position_id") or f"{token}:{when}"))] = {
                        "ledger": "live", "token_id": token, "entry_time": when,
                        "team": data.get("team"), "sport": data.get("sport"),
                    }
    return list(out.values())


def load_watch_samples() -> dict[str, list[dict]]:
    by_token: dict[str, list[dict]] = {}
    if not os.path.exists(WATCH_PATH):
        return by_token
    with open(WATCH_PATH, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = str(row.get("token_id") or "")
            if token:
                by_token.setdefault(token, []).append(row)
    for rows in by_token.values():
        rows.sort(key=lambda r: float(r.get("t") or 0))
    return by_token


def report(hours: float | None, detail: bool) -> int:
    samples = load_watch_samples()
    if not samples:
        print(f"还没有入场前采样（{WATCH_PATH} 为空或不存在）")
        return 1
    positions = load_positions(hours)
    print(f"入场前采样覆盖 {len(samples)} 个盘口 · 仓位 {len(positions)} 笔"
          + (f"（最近 {hours:.0f} 小时）" if hours else ""))
    results = []
    for pos in positions:
        rows = samples.get(pos["token_id"])
        if not rows:
            continue
        item = decompose_window(rows, pos["entry_time"])
        if item:
            item.update({"ledger": pos["ledger"], "team": pos.get("team"),
                         "sport": pos.get("sport")})
            results.append(item)
    if not results:
        print("还没有「入场前有 ≥2 条采样」的仓位 —— 采集刚上线，等下一批入场。")
        return 0

    if detail:
        print("\n逐笔（单位：美分）:")
        for r in sorted(results, key=lambda x: x["pre_move"]):
            print(f"   {r['ledger']:5s} {str(r['team'])[:18]:18s} {str(r['sport']):7s} "
                  f"窗口 {r['span_seconds']:5.0f}s / {r['samples']:3d} 条 · "
                  f"入场前涨跌 {r['pre_move']*100:+5.2f} · "
                  f"其中比分变了 {r['news_move']*100:+5.2f} / 比分没变 {r['no_news_move']*100:+5.2f} · "
                  f"窗口内比分变化 {r['score_changes']} 次")

    pre = [r["pre_move"] for r in results]
    no_news = [r["no_news_move"] for r in results]
    news = [r["news_move"] for r in results]
    moved = [r for r in results if abs(r["pre_move"]) >= 0.005]
    pure_convergence = [r for r in moved if abs(r["news_move"]) < 0.005]
    print(f"\n样本 {len(results)} 笔（入场前窗口中位 {statistics.median(r['span_seconds'] for r in results):.0f}s）")
    print(f"  入场前净漂移     中位 {statistics.median(pre)*100:+.2f} 美分")
    print(f"  其中 比分变化段  中位 {statistics.median(news)*100:+.2f} 美分")
    print(f"  其中 比分未变段  中位 {statistics.median(no_news)*100:+.2f} 美分")
    if moved:
        print(f"\n入场前动过 ≥0.5 美分的 {len(moved)} 笔里，"
              f"**完全没有比分变化**的占 {len(pure_convergence)}/{len(moved)}"
              f"（{len(pure_convergence)/len(moved)*100:.0f}%）")
    zero_news = [r for r in results if r["score_changes"] == 0]
    print(f"入场前窗口内一次比分变化都没有的仓位：{len(zero_news)}/{len(results)}"
          f"（{len(zero_news)/len(results)*100:.0f}%）")
    print("\n读法：如果「比分未变段」占了大头，那么「我们比市场慢」就不是数据源速度问题，"
          "\n     换源也拿不回这段；该改的是触发时机（入场前价格已经走完）。")
    return 0


def selftest() -> int:
    """校验拆解逻辑：构造一段"比分没变、价格却涨了 3 分"的窗口。"""
    base = 1000.0
    def row(offset, mid, note):
        return {"t": base + offset, "mid": mid, "state_note": note, "token_id": "T1"}
    samples = [
        row(0, 0.820, "SET_LEAD sets 1-0; games 0-0"),
        row(30, 0.840, "SET_LEAD sets 1-0; games 0-0"),
        row(60, 0.855, "SET_LEAD sets 1-0; games 0-0"),
        row(90, 0.870, "SET_LEAD sets 1-0; games 1-0"),
        row(120, 0.880, "SET_LEAD sets 1-0; games 1-0"),
    ]
    item = decompose_window(samples, entry_time=base + 120)
    # 分段归属：**状态发生变化的那个区间**算 news（这里是 0.855→0.870 那一下）。
    # 所以 no_news = 0.020+0.015+0.010 = 0.045、news = 0.015。
    ok = (item is not None
          and abs(item["pre_move"] - 0.060) < 1e-9
          and abs(item["no_news_move"] - 0.045) < 1e-9
          and abs(item["news_move"] - 0.015) < 1e-9
          and item["score_changes"] == 1)
    print(f"selftest: {item} → {'OK' if ok else 'FAIL'}")
    only_one = decompose_window(samples[:1], entry_time=base)
    ok2 = only_one is None
    print(f"selftest2（只有 1 条采样 → 跳过）: {only_one} → {'OK' if ok2 else 'FAIL'}")
    return 0 if (ok and ok2) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return report(args.hours, args.detail)


if __name__ == "__main__":
    raise SystemExit(main())
