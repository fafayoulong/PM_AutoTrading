# -*- coding: utf-8 -*-
"""MLB 局面采集的复盘：**出局数 / 垒上有人** 能不能区分"真锁"和"假锁"。

问题（2026-10-28）：MLB 的 "8 局 + 2 分" 规则看不到垒上有没有人、几出局。
市场给 0.88 时往往正是在说"2 分领先但满垒 0 出局"，而我们的规则照买 →
实盘 <0.90 那 13 笔胜率只有 76.9%（−9.84%）。

本工具用 `trades/mlb_situation_ledger.jsonl`（ESPN situation × PM 盘口）回答：
  * 同样的 (局数, 分差) 下，按 outs / 垒上分组，真实胜率差多少？
  * 市场价给的隐含概率，和真实胜率差多少（= 有没有可利用的边际）？

用法：
    python tools/mlb_situation_review.py [--min-bucket 15]
只读（会把结算结果缓存在 trades/mlb_situation_results.json）。
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
import sys
import urllib.request

RESULT_CACHE = "trades/mlb_situation_results.json"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event="
SAMPLE_GLOB = "trades/mlb_situation_ledger.jsonl*"


def load_rows() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(SAMPLE_GLOB)):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.sort(key=lambda r: float(r.get("t") or 0))
    # 每个（比赛, 局, 上下半, 出局数, 垒上局面）只取第一条 —— 同局面会重复采很多次
    seen: dict[tuple, dict] = {}
    for row in rows:
        key = (row.get("game_id"), row.get("inning"), row.get("half"),
               row.get("outs"), bool(row.get("on_first")), bool(row.get("on_second")),
               bool(row.get("on_third")))
        seen.setdefault(key, row)
    return list(seen.values())


def load_results(cache: dict, game_ids: set[str]) -> dict:
    todo = [g for g in game_ids if g and g not in cache]
    for gid in todo:
        try:
            req = urllib.request.Request(SUMMARY + gid, headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
            winner = ""
            for side in comp.get("competitors") or []:
                if side.get("winner"):
                    winner = str((side.get("team") or {}).get("displayName") or "")
            if winner:
                cache[gid] = winner
        except Exception:  # noqa: BLE001
            continue
    try:
        with open(RESULT_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return cache


def report(rows: list[dict], min_bucket: int) -> None:
    print(f"局面样本 {len(rows)} 条（已按 局/半局/出局/垒上 去重）")
    if not rows:
        print("还没有数据 —— MLB 赛季接近结束，需要等比赛进行中才会采到。")
        return
    print()
    print("=== 第 8 局及以后、领先 2 分：按出局数与垒上分 ===")
    print(f"{'outs':>4} {'垒上':>6} {'n':>5} {'真实胜率':>8} {'市场均价':>8} {'边际':>7} {'ROI':>7}")
    for outs in (0, 1, 2):
        for bases_label, pred in (("空", lambda r: not any(r.get(k) for k in ("on_first", "on_second", "on_third"))),
                                  ("有人", lambda r: any(r.get(k) for k in ("on_first", "on_second", "on_third")))):
            sel = [r for r in rows
                   if int(r.get("inning") or 0) >= 8 and int(r.get("diff") or 0) >= 2
                   and r.get("outs") == outs and pred(r) and r.get("won") is not None
                   and r.get("best_ask")]
            if len(sel) < min_bucket:
                continue
            wins = sum(1 for r in sel if r["won"])
            p = wins / len(sel)
            asks = [float(r["best_ask"]) for r in sel]
            avg = statistics.mean(asks)
            rets = [(1 - a) / a if r["won"] else -1.0 for r, a in zip(sel, asks)]
            print(f"{outs:>4} {bases_label:>6} {len(sel):>5} {p*100:>7.1f}% {avg:>8.3f} "
                  f"{(p-avg)*100:>+6.1f}pp {statistics.mean(rets)*100:>+6.2f}%")
    print()
    print("=== 按 (局数, 分差) 汇总（不管垒上）===")
    print(f"{'局':>3} {'分差':>4} {'n':>5} {'真实胜率':>8} {'市场均价':>8} {'边际':>7}")
    for inning in (6, 7, 8):
        for diff in (1, 2, 3):
            sel = [r for r in rows if int(r.get("inning") or 0) == inning
                   and int(r.get("diff") or 0) == diff and r.get("won") is not None
                   and r.get("best_ask")]
            if len(sel) < min_bucket:
                continue
            p = sum(1 for r in sel if r["won"]) / len(sel)
            avg = statistics.mean(float(r["best_ask"]) for r in sel)
            print(f"{inning:>3} {diff:>4} {len(sel):>5} {p*100:>7.1f}% {avg:>8.3f} {(p-avg)*100:>+6.1f}pp")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-bucket", type=int, default=15)
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()
    rows = load_rows()
    cache = {}
    if os.path.exists(RESULT_CACHE):
        try:
            cache = json.loads(open(RESULT_CACHE, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            cache = {}
    if not args.no_network:
        cache = load_results(cache, {str(r.get("game_id") or "") for r in rows})
    for row in rows:
        winner = cache.get(str(row.get("game_id") or ""))
        row["won"] = (str(row.get("leader") or "") == winner) if winner else None
    report(rows, args.min_bucket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
