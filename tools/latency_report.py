# -*- coding: utf-8 -*-
"""下单链路耗时报告：从 live_order_events.jsonl 统计「信号→提交」各段耗时。

用法（在 VPS 或本地项目根目录）：
    python tools/latency_report.py            # 最近 10 笔
    python tools/latency_report.py --n 30
    python tools/latency_report.py --file trades/live_order_events.jsonl

统计的口径（都用事件自带的 `at` 时间戳）：
    LIVE_SUBMIT_ENTRY → PRE_ORDER      预订单准备（拿盘口/算价/额度检查）
    PRE_ORDER         → ORDER_SUBMITTED 真正提交（网络 + 签名）
    合计               → 从决定下单到交易所接单
"""

from __future__ import annotations

import argparse
import json
import os
import statistics


def rows(path: str):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="trades/live_order_events.jsonl")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--since", type=float, default=0.0,
                    help="只统计该 epoch 之后的事件（用于对比迁移前后）")
    args = ap.parse_args()

    path = args.file
    if not os.path.exists(path):
        print(f"找不到 {path}")
        return 1

    submit_at = None
    pre_at = None
    done = []
    failures = 0
    submitted_at: dict[str, float] = {}
    confirms: list[float] = []
    for row in rows(path):
        kind = row.get("kind")
        at = float(row.get("at") or 0)
        if at < args.since:
            continue
        data = row.get("data") or {}
        if kind == "LIVE_SUBMIT_ENTRY":
            submit_at, pre_at = at, None
        elif kind == "PRE_ORDER" and submit_at:
            pre_at = at
        elif kind == "ORDER_SUBMITTED":
            oid = str(data.get("order_id") or "")
            if oid:
                submitted_at[oid] = at
            if submit_at:
                done.append((
                    at - submit_at,
                    (at - pre_at) if pre_at else None,
                    (pre_at - submit_at) if pre_at else None,
                    kind,
                    data.get("sport"),
                    data.get("leader_team") or data.get("team") or "",
                    data.get("price"),
                ))
                submit_at = pre_at = None
        elif kind == "ORDER_UPDATE" and str(data.get("status", "")).upper() in {
                "FILLED", "MATCHED", "CLOSED"}:
            oid = str(data.get("order_id") or "")
            started = submitted_at.pop(oid, None)
            if started:
                confirms.append(at - started)
        elif kind == "SUBMIT_FAILED" and submit_at:
            done.append((
                at - submit_at,
                (at - pre_at) if pre_at else None,
                (pre_at - submit_at) if pre_at else None,
                kind,
                data.get("sport"),
                data.get("leader_team") or data.get("team") or "",
                data.get("price"),
            ))
            failures += 1
            submit_at = pre_at = None

    if not done:
        print("还没有「决定下单 → 提交」的完整记录（可能一直没触发实盘信号）")
        return 0

    print(f"最近 {min(args.n, len(done))} 笔（文件共 {len(done)} 笔；其中 {failures} 笔失败）")
    print()
    print("  {:>9}  {:>10}  {:>10}  {:<10} {:<7} {}".format(
        "合计(s)", "准备(s)", "提交(s)", "结果", "运动", "标的"))
    for total, submit, prep, kind, sport, team, price in done[-args.n:]:
        print("  {:>9.3f}  {:>10}  {:>10}  {:<10} {:<7} {} @{}".format(
            total,
            f"{prep:.3f}" if prep is not None else "-",
            f"{submit:.3f}" if submit is not None else "-",
            "OK" if kind == "ORDER_SUBMITTED" else "失败",
            sport or "-", (team or "")[:24], price))

    ok = [d for d in done if d[3] == "ORDER_SUBMITTED"]
    if ok:
        totals = [d[0] for d in ok]
        print()
        print("成功提交 {}/{} 笔：合计中位 {:.3f}s（最小 {:.3f}s / 最大 {:.3f}s）".format(
            len(ok), len(done), statistics.median(totals), min(totals), max(totals)))
        subs = [d[1] for d in ok if d[1] is not None]
        preps = [d[2] for d in ok if d[2] is not None]
        if preps:
            print("  其中「准备」中位 {:.3f}s、「提交」中位 {:.3f}s".format(
                statistics.median(preps), statistics.median(subs) if subs else 0))
    if confirms:
        print("  成交确认（ORDER_SUBMITTED → FILLED）{} 笔：中位 {:.3f}s（最小 {:.3f}s / 最大 {:.3f}s）"
              .format(len(confirms), statistics.median(confirms), min(confirms), max(confirms)))

    # ---- 端到端：从「信号落库」到「交易所接单」----
    # 这是用户真正关心的数字。用 signal_events 里同 token 的 OPENED 信号作为起点，
    # 取订单提交时刻减去它（含：读盘口 / 闸门 / 额度检查 / 签名 / POST）。
    sig_path = os.path.join(os.path.dirname(path) or ".", "signal_events.jsonl")
    if os.path.exists(sig_path) and done:
        by_token: dict[str, float] = {}
        for row in rows(sig_path):
            if row.get("reason") != "OPENED":
                continue
            tok = (row.get("details") or {}).get("token_id")
            if tok:
                by_token[str(tok)] = float(row.get("t") or 0)
        # 重新扫一遍订单事件，拿 token 和提交时刻
        e2e: list[float] = []
        pending_tok = None
        for row in rows(path):
            kind, at = row.get("kind"), float(row.get("at") or 0)
            data = row.get("data") or {}
            if at < args.since:
                continue
            if kind == "LIVE_SUBMIT_ENTRY":
                pending_tok = str(data.get("token_id") or "")
            elif kind == "ORDER_SUBMITTED" and pending_tok:
                started = by_token.get(pending_tok)
                if started:
                    e2e.append(at - started)
                pending_tok = None
        if e2e:
            print("  端到端（信号落库 → 交易所接单）{} 笔：中位 {:.3f}s"
                  "（最小 {:.3f}s / 最大 {:.3f}s）".format(
                      len(e2e), statistics.median(e2e), min(e2e), max(e2e)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
