# -*- coding: utf-8 -*-
"""足球数据源赛跑采样器：**量化「市场比 ESPN 快多少」**。

为什么要它（2026-09-19 用户："有没有更快的足球信息源"）：
  换数据源之前得先知道现在差多少。足球现在**只用 ESPN**（其他 9 个运动都接了
  Polymarket 自己的实时流，足球是唯一没接的）。这里对同一场进行中的比赛，
  用**同一个进程、同一个 tick** 同时记录：
    * ESPN 的比分与分钟（来自 NBAFeed.fetch_soccer_games）
    * Polymarket 主胜盘的买一/卖一（来自 ClobBookSource）
  然后看"进球后谁先动、差几秒"。

用法：
    python tools/soccer_latency_sampler.py                 # 常驻采样（systemd 里跑）
    python tools/soccer_latency_sampler.py --once          # 采一轮就退出（冒烟用）
    python tools/soccer_latency_sampler.py --report        # 读样本出结论

样本文件：trades/soccer_latency_samples.jsonl（超过 64MB 自动轮转一份 .1）

⚠️ 分辨率说明：ESPN 侧靠轮询，间隔 = --interval（默认 5 秒），所以测出来的
"谁先动"精度是 ±5 秒。对"到底差几毫秒还是几十秒"这个问题够用。
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config  # noqa: E402
from src.atomic_io import rotate_by_size  # noqa: E402
from src.nba_feed import NBAFeed  # noqa: E402
from src.polymarket_feed import PolymarketFeed  # noqa: E402
from src.position_paths import ClobBookSource, summarize_book  # noqa: E402
from src.sports_overlay import PM_LEAGUES  # noqa: E402
from src.sports_ws_feed import PolymarketSportsFeed  # noqa: E402

SAMPLE_PATH = "trades/soccer_latency_samples.jsonl"
MAX_BYTES = 64 * 1024 * 1024
# 2026-10-27：固定 `.1` 会覆盖上一代 → 改成带时间戳 + 保留 12 份（≈ 65 天）。
BACKUP_COUNT = 12


def append_sample(row: dict) -> None:
    os.makedirs(os.path.dirname(SAMPLE_PATH), exist_ok=True)
    try:
        rotate_by_size(SAMPLE_PATH, max_bytes=MAX_BYTES, backup_count=BACKUP_COUNT)
    except OSError:
        pass
    with open(SAMPLE_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def sample_once(feed: NBAFeed, pm: PolymarketFeed, clob: ClobBookSource,
                      token_cache: dict, stream=None, pm_seen: dict | None = None) -> int:
    games = await feed.fetch_soccer_games()
    live = [g for g in games if str(getattr(g, "status", "")) == "in"]
    now = time.time()
    written = 0
    for game in live:
        gid = str(getattr(game, "game_id", ""))
        if not gid:
            continue
        token = token_cache.get(gid)
        if token is None:
            try:
                market = await pm.search_soccer_winner_market(
                    str(getattr(game, "league_tag", "soccer") or "soccer"),
                    str(getattr(game, "home_team_name", "") or getattr(game, "home_team", "")),
                    str(getattr(game, "away_team_name", "") or getattr(game, "away_team", "")),
                    None,
                )
                token = str(market.token_ids[0]) if market and market.token_ids else ""
            except Exception:  # noqa: BLE001 - 取不到盘口就跳过这场
                token = ""
            token_cache[gid] = token
        if not token:
            continue
        try:
            book, source = await clob.fetch(token)
        except Exception:  # noqa: BLE001
            book, source = None, ""
        snap = summarize_book(book) if book else None
        append_sample({
            "t": now,
            "src": "espn",
            "game_id": gid,
            "league": getattr(game, "league_name", "") or getattr(game, "league", ""),
            "home": getattr(game, "home_team", ""),
            "away": getattr(game, "away_team", ""),
            "home_score": getattr(game, "home_score", None),
            "away_score": getattr(game, "away_score", None),
            "minute": getattr(game, "minute", None),
            "feed_updated_at": getattr(game, "updated_at", None),
            "token": token[-12:],
            "bid": (snap or {}).get("best_bid"),
            "ask": (snap or {}).get("best_ask"),
            "book_source": source,
        })
        written += 1
    # 2026-09-20：**同时记录 Polymarket 自己的实时流**（push、亚秒级）——
    # 它就是市场在用的那条数据。有了它才能量出「ESPN 比市场慢多少秒」，
    # 从而区分"是我们源慢"还是"这些盘口根本不跟进球重定价"。
    # `received_at` 是那条消息的到达时间，所以第一次看到新比分时它就等于"市场报出这个球的时刻"。
    if stream is not None and pm_seen is not None:
        try:
            for row in stream.snapshot():
                league = str(row.get("leagueAbbreviation") or "").lower()
                # 2026-09-20：这儿**先不过滤**。初版用 `PM_LEAGUES` 过滤，而那个映射表里
                # 根本没有足球条目 → 一条都记不到。而我们其实不知道 PM 流用什么缩写
                # 命名 EPL/LALIGA/意甲… 这些联赛，所以先把**所有**联赛都记下来看数据，
                # 拿到真实缩写之后再决定映射与过滤。
                gid = str(row.get("gameId") or "")
                score = str(row.get("score") or "")
                if not gid or not score or pm_seen.get(gid) == score:
                    continue
                pm_seen[gid] = score
                append_sample({
                    "t": float(row.get("received_at") or now),
                    "src": "pm_stream",
                    "game_id": gid,
                    "league": league,
                    "score": score,
                    "live": row.get("live"),
                    "ended": row.get("ended"),
                })
                written += 1
        except Exception as exc:  # noqa: BLE001 - 采样失败不能影响主循环
            print(f"[sampler] PM 流采样失败：{type(exc).__name__}: {exc}", flush=True)
    return written


async def run_forever(interval: float) -> int:
    config = load_config()
    feed = NBAFeed(config)
    pm = PolymarketFeed(config)
    clob = ClobBookSource(config)
    stream = PolymarketSportsFeed(config)
    await feed.start()
    await pm.start()
    await clob.start()
    try:
        await stream.start()
        print("[sampler] PM 实时流已启动（用来对比市场自己的比分源）", flush=True)
    except Exception as exc:  # noqa: BLE001 - 流起不来也要继续采 ESPN+价格
        print(f"[sampler] PM 实时流启动失败，只采 ESPN+价格：{type(exc).__name__}: {exc}",
              flush=True)
        stream = None
    token_cache: dict[str, str] = {}
    pm_seen: dict[str, str] = {}
    print(f"[sampler] 每 {interval:.0f}s 采一轮 → {SAMPLE_PATH}", flush=True)
    try:
        while True:
            try:
                n = await sample_once(feed, pm, clob, token_cache,
                                      stream=stream, pm_seen=pm_seen)
                if n:
                    print(f"[sampler] {time.strftime('%H:%M:%S')} 记录 {n} 场", flush=True)
            except Exception as exc:  # noqa: BLE001 - 采样失败不能退出
                print(f"[sampler] 本轮失败：{type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(interval)
    finally:
        if stream is not None:
            try:
                await stream.stop()
            except Exception:  # noqa: BLE001
                pass
        await clob.stop()
        await pm.stop()
        await feed.stop()


def _mid(row):
    b, a = row.get("bid"), row.get("ask")
    if b is None or a is None:
        return None
    return (float(b) + float(a)) / 2


def report() -> int:
    sources = sorted(glob.glob(SAMPLE_PATH + "*"))
    if not sources:
        print(f"还没有样本文件 {SAMPLE_PATH}")
        return 1
    pm_rows: list[dict] = []
    rows = []
    for source in sources:
      with open(source, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 2026-09-20：样本里现在混了三类行 —— espn（比分+价格）、pm_stream（市场自己的比分）、
            # 以及改动前写的历史行（没有 src）。只把带价格的那类拿来算价格漂移。
            if d.get("src") == "pm_stream":
                pm_rows.append(d)
                continue
            if d.get("bid") is not None and d.get("home_score") is not None:
                rows.append(d)
    if not rows:
        print("样本为空（可能一直没有进行中的足球比赛）")
        return 1

    by_game = {}
    for r in rows:
        by_game.setdefault(r["game_id"], []).append(r)
    print(f"样本 {len(rows)} 条，覆盖 {len(by_game)} 场比赛")

    # 2026-09-20 重写：旧口径是"比分变化后第一次价格变动"，很容易被一次无关跳动带偏
    # （实测最大 1467 秒，显然与那个球无关）。改成看**方向 + 幅度**：
    # 进球后，价格应该朝"进球一方"的方向走；如果**进球前的价格已经等于进球后的水平**，
    # 那才叫"市场已经收敛完了"（也就是用户怀疑的情形）。
    events = []
    for gid, series in by_game.items():
        series.sort(key=lambda r: r["t"])
        for i in range(1, len(series)):
            prev, cur = series[i - 1], series[i]
            ps = (prev["home_score"], prev["away_score"])
            cs = (cur["home_score"], cur["away_score"])
            if ps == cs:
                continue
            home_goal = cs[0] > ps[0]
            mid_before = _mid(prev)
            if mid_before is None:
                continue
            row = {"game": gid, "t": cur["t"], "goal": "主队" if home_goal else "客队",
                   "score": f"{cs[0]}-{cs[1]}", "sign": 1.0 if home_goal else -1.0,
                   "before": mid_before}
            # ★ 决定性的一列：**我们还没看到比分变化之前**（-40s ~ -5s），
            # 价格是不是已经在朝进球方向动了？如果动了 → 市场比我们的数据源**更早**
            # 知道了这个球（那就是"市场比我们快"，不是"我们在同一瞬间"）。
            early = [r for r in series
                     if r["t"] <= cur["t"] - 20 and _mid(r) is not None]
            row["pre"] = ((mid_before - _mid(early[-1])) * row["sign"]) if early else None
            for w in (5, 15, 30, 60, 120):
                hit = next((r for r in series if r["t"] >= cur["t"] + w and _mid(r) is not None), None)
                row[f"m{w}"] = (None if hit is None
                                else ( _mid(hit) - mid_before) * row["sign"])
            events.append(row)

    print()
    if not events:
        print("还没有抓到「比分变化 + 之后的价格」配对")
        return 1
    print(f"共 {len(events)} 次进球事件（价格按中价，方向已折算成「朝进球一方」）")
    # ★ 核心对比：PM 流（市场自己的源，push）报出比分 vs ESPN 报出比分，谁更早？
    if pm_rows:
        by_pm: dict[str, list] = {}
        for r in pm_rows:
            by_pm.setdefault(str(r.get("game_id")), []).append(r)
        print(f"  PM 实时流样本 {len(pm_rows)} 条，覆盖 {len(by_pm)} 场比赛"
              f"（联赛：{sorted({str(r.get('league')) for r in pm_rows})[:6]}）")
        deltas = []
        for gid, series in by_pm.items():
            series.sort(key=lambda r: r["t"])
            for i in range(1, len(series)):
                if series[i]["score"] == series[i - 1]["score"]:
                    continue
                t_pm, sc = series[i]["t"], series[i]["score"]
                # 在 ESPN 样本里找同一个比分第一次出现的时刻
                espn_series = sorted(
                    [r for r in rows if str(r.get("game_id")) == gid], key=lambda r: r["t"])
                hit = next((r for r in espn_series
                            if f"{r['home_score']}-{r['away_score']}" == sc), None)
                if hit:
                    deltas.append(hit["t"] - t_pm)
        if deltas:
            print("  ★ ESPN 报出比分 − PM 流报出比分（正数 = **ESPN 更慢**）：")
            print("     中位 {:+.1f}s  最小 {:+.1f}s  最大 {:+.1f}s  （n={}）".format(
                statistics.median(deltas), min(deltas), max(deltas), len(deltas)))
        else:
            print("     还没有「同一个比分两边都记到」的配对（需要 PM 流覆盖我们这些联赛）")
    else:
        print("  （还没有 PM 流样本：要么流没连上，要么它不覆盖我们这些联赛）")
    pre = [e["pre"] for e in events if e.get("pre") is not None]
    if pre:
        print("  ★ 进球前 20 秒以上就已经在朝进球方向的变动：中位 {:+.4f}，"
              "其中 >0 的占 {:.0f}%（n={}）".format(
                  statistics.median(pre), sum(1 for v in pre if v > 0) / len(pre) * 100, len(pre)))
    print("  窗口      有效样本   朝进球方向的中位变动   变动>0 的比例")
    for w in (5, 15, 30, 60, 120):
        vals = [e[f"m{w}"] for e in events if e.get(f"m{w}") is not None]
        if not vals:
            continue
        pos = sum(1 for v in vals if v > 0)
        print("  +{:<4}s   {:>6}     {:>+10.4f}            {:>5.0f}%".format(
            w, len(vals), statistics.median(vals), pos / len(vals) * 100))
    print()
    print("  读法：如果「+30s 的中位变动」明显大于 0、且 >0 的比例高 → 市场是**进球后才动**的")
    print("        （说明 ESPN 不慢，甚至比市场快）；")
    print("        如果各窗口都在 0 附近、>0 只有一半 → 进球前价格就已经到位了，")
    print("        那才支持「市场早已收敛、我们的源太慢」这个怀疑。")
    print()
    print("  明细（前 12 条）：")
    for e in events[:12]:
        print("    {} {} {} 进前中价 {:.3f} → +30s {:+.4f} +60s {:+.4f}".format(
            e["game"][-6:], e["goal"], e["score"], e["before"],
            e.get("m30") if e.get("m30") is not None else float("nan"),
            e.get("m60") if e.get("m60") is not None else float("nan")))
    return 0


def _report_legacy() -> int:
    lags = []
    for gid, series in by_game.items():
        series.sort(key=lambda r: r["t"])
        score_change_at = None
        base_price = None
        for r in series:
            score = (r["home_score"], r["away_score"])
            price = (r.get("bid"), r.get("ask"))
            prev = series[series.index(r) - 1] if series.index(r) else None
            prev_score = (prev["home_score"], prev["away_score"]) if prev else None
            if prev_score is not None and score != prev_score:
                score_change_at = r["t"]
                base_price = price
                continue
            if score_change_at and base_price and price != base_price:
                lags.append({"game": gid, "lag": r["t"] - score_change_at,
                             "score": f"{score[0]}-{score[1]}",
                             "from": base_price, "to": price})
                score_change_at = None

    print()
    if not lags:
        print("还没抓到「比分变化 → 盘口变化」的配对（需要比赛里有进球）")
        return 1
    vals = [l["lag"] for l in lags]
    print(f"配对 {len(lags)} 次：盘口在 ESPN 报出比分**之后**动的中位延迟 "
          f"{statistics.median(vals):.1f}s（最小 {min(vals):.1f}s / 最大 {max(vals):.1f}s）")
    print("（注意：ESPN 侧是 5 秒轮询，所以 <5s 的读数只能说明「同一轮内」）")
    print()
    for l in sorted(lags, key=lambda x: x["lag"])[:10]:
        print("  {} {} {} → {}".format(l["game"][-6:], l["score"], l["from"], l["to"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        return report()
    if args.once:
        async def one():
            config = load_config()
            feed, pm, clob = NBAFeed(config), PolymarketFeed(config), ClobBookSource(config)
            await feed.start(); await pm.start(); await clob.start()
            try:
                n = await sample_once(feed, pm, clob, {})
                print(f"采到 {n} 场进行中的足球比赛")
            finally:
                await clob.stop(); await pm.stop(); await feed.stop()
        asyncio.run(one())
        return 0
    return asyncio.run(run_forever(max(2.0, args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
