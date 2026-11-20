# -*- coding: utf-8 -*-
"""网球数据源赛跑采样器：**量化「PM 实时流比 ESPN 快多少秒」**。

为什么要它（2026-09-28 用户："有没有比 ESPN 更快的信息源，我怀疑现在比市场
慢就是因为 ESPN 太慢"）：
  足球早就有这个测量（`tools/soccer_latency_sampler.py`），网球一直没有。
  而"ESPN 慢"直接决定了要不要花钱换源，不能靠猜。

它做什么：同一个进程、同一个 tick，同时记录
  * ESPN 网球 scoreboard（`NBAFeed.fetch_tennis_matches`，atp + wta 两个地址）
  * Polymarket 自己的实时流（`PolymarketSportsFeed`，网球 league: atp / wta /
    challenger / wta challenger）—— **这条就是市场在用的数据**
再用项目自己的匹配器（`SportsStreamOverlay.match_row`，认得姓名顺序相反）把两边
配到同一场比赛上，记录每次"分数推进"是**谁先报出来的**。

关键口径
--------
* PM 侧的时间戳用推送自带的 `received_at`（亚秒级）→ 近似等于"市场看到这个比分的
  时刻"（市场也是收这条流）。
* ESPN 侧只能靠轮询 → 看到时刻的精度是 ±interval（默认 2 秒）。
  所以测出来的 delta 是 **PM 领先秒数的上界**（其中含最多一个 interval 的量化误差）。
* 报告里 delta = ESPN 首次看到该比分 − PM 首次报出该比分：
    > 0  → PM 更快（我们走 ESPN 就是慢这么多）
    ≈ 0  → 两边同时到
    < 0  → 反了，ESPN 更快

用法：
    python tools/tennis_latency_sampler.py                # 常驻采样（systemd: tennis-sampler）
    python tools/tennis_latency_sampler.py --once         # 采一轮就退出（冒烟）
    python tools/tennis_latency_sampler.py --report       # 读样本出结论
    python tools/tennis_latency_sampler.py --selftest     # 不联网，用合成数据校验报告逻辑

样本文件：trades/tennis_latency_samples.jsonl（超过 64MB 自动轮转一份 .1）
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
from src.sports_overlay import SportsStreamOverlay  # noqa: E402
from src.sports_ws_feed import PolymarketSportsFeed  # noqa: E402

SAMPLE_PATH = "trades/tennis_latency_samples.jsonl"
MAX_BYTES = 64 * 1024 * 1024
# 2026-10-27：固定 `.1` 会覆盖上一代 → 改成带时间戳 + 保留 10 份。
BACKUP_COUNT = 10
# 同一场比赛多久没有新东西也记一条：用于确认"这场还在被两路覆盖"，不参与延迟计算
HEARTBEAT_SECONDS = 120.0


def append_sample(row: dict) -> None:
    os.makedirs(os.path.dirname(SAMPLE_PATH), exist_ok=True)
    try:
        rotate_by_size(SAMPLE_PATH, max_bytes=MAX_BYTES, backup_count=BACKUP_COUNT)
    except OSError:
        pass
    with open(SAMPLE_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def canonical_pm_state(entry) -> tuple | None:
    """把 PM 的逐盘局分折成与 ESPN 同向的 p1/p2 元组。

    方向来自校验阶段（`StreamEntry.orientation`）：
      away-home → PM 的 away 就是 ESPN 的 player1
      home-away → PM 的 away 是 ESPN 的 player2
    还没校验（orientation 为空）时返回 None —— 宁可不记，也不猜方向。
    """
    sets = getattr(entry, "set_scores", None)
    orientation = getattr(entry, "orientation", None)
    if not sets or orientation not in ("away-home", "home-away"):
        return None
    if orientation == "away-home":
        return tuple((int(a), int(b)) for a, b in sets)
    return tuple((int(b), int(a)) for a, b in sets)


def espn_state(row: dict) -> tuple:
    p1 = (row.get("player1") or {}).get("linescores") or []
    p2 = (row.get("player2") or {}).get("linescores") or []
    return tuple((int(a), int(b)) for a, b in zip(p1, p2))


async def sample_once(feed: NBAFeed, overlay: SportsStreamOverlay, stream,
                      last_row: dict) -> int:
    """采一轮：摄入 PM 推送 → 逐场同时记录 ESPN 与 PM 的比分状态。"""
    written = 0
    now = time.time()

    # 1) 先摄入 PM 推送（ingest 内部按运动过滤；我们只关心 TENNIS）
    pingested: dict[str, float] = {}
    game_ids: dict[tuple[str, str], str] = {}
    if stream is not None:
        try:
            for row in stream.snapshot():
                sport = overlay.ingest(row)
                if sport == "TENNIS":
                    gid = str(row.get("gameId") or "")
                    if gid:
                        pingested[gid] = float(row.get("received_at") or now)
                    # PM 侧比分一变就单独记一条（用推送自带时间戳，亚秒精度）
                    entry = overlay.entry_for_payload("TENNIS", row)
                    if entry is None:
                        continue
                    game_ids[(entry.away_key, entry.home_key)] = gid
                    state = canonical_pm_state(entry)
                    if state is None:
                        continue
                    key = (entry.away_key, entry.home_key)
                    prev = last_row.get(("pm", key))
                    if prev == state:
                        continue
                    append_sample({
                        "t": float(row.get("received_at") or now),
                        "src": "pm",
                        "match_key": "|".join(key),
                        "away": entry.away_name,
                        "home": entry.home_name,
                        "league": str(row.get("leagueAbbreviation") or ""),
                        "score": str(row.get("score") or ""),
                        "state": [list(x) for x in state],
                        "orientation": entry.orientation,
                        "live": row.get("live"),
                        "ended": row.get("ended"),
                    })
                    last_row[("pm", key)] = state
                    written += 1
        except Exception as exc:  # noqa: BLE001 - 采样失败不能影响主循环
            print(f"[tennis-sampler] PM 流采样失败：{type(exc).__name__}: {exc}", flush=True)

    # 2) ESPN 侧逐场记录（比分变化 → 记；长时间没动静 → 记心跳）
    try:
        matches = await feed.fetch_tennis_matches()
    except Exception as exc:  # noqa: BLE001
        print(f"[tennis-sampler] ESPN 抓取失败：{type(exc).__name__}: {exc}", flush=True)
        return written

    for row in matches:
        entry = overlay.match_row("TENNIS", row)
        if entry is None:
            continue
        # 顺手跑一次覆盖：`apply_to_row` 内部会做**口径校验**（用 ESPN 的逐盘局分
        # 给 PM 数据定方向 → `orientation`）。没有 orientation，PM 的 (away, home)
        # 就折不成 (p1, p2)，上面那条"带亚秒级时间戳的 PM 样本"永远写不出来 ——
        # 而那条恰恰是测量精度的来源（pair 行只有 2 秒分辨率）。
        # 这里的 overlay 只属于采样器进程，不会影响机器人自己的覆盖层。
        overlay.apply_to_row("TENNIS", row)
        key = (entry.away_key, entry.home_key)
        espn = espn_state(row)
        pm = canonical_pm_state(entry)
        prev = last_row.get(("pair", key))
        changed = prev != (espn, pm)
        last_t = last_row.get(("pair_t", key), 0.0)
        if not changed and now - last_t < HEARTBEAT_SECONDS:
            continue
        append_sample({
            "t": now,
            "src": "pair",
            "match_key": "|".join(key),
            "espn_match_id": str(row.get("match_id") or row.get("game_id") or ""),
            "espn": {
                "p1": (row.get("player1") or {}).get("name"),
                "p2": (row.get("player2") or {}).get("name"),
                "state": [list(x) for x in espn],
                "updated_at": row.get("updated_at"),
                "status": row.get("status"),
            },
            "pm": None if pm is None else {
                "game_id": game_ids.get(key, ""),
                "state": [list(x) for x in pm],
                "received_at": entry.received_at,
                "orientation": entry.orientation,
                "score": entry.raw_score,
                "live": entry.live,
                "ended": entry.ended,
                "validated": bool(getattr(entry, "validated", False)),
            },
        })
        last_row[("pair", key)] = (espn, pm)
        last_row[("pair_t", key)] = now
        written += 1
    return written


async def run_forever(interval: float) -> int:
    config = load_config()
    config.pm_stream_override_sports = tuple(
        set(config.pm_stream_override_sports) | {"TENNIS"}
    )
    feed = NBAFeed(config)
    overlay = SportsStreamOverlay(config)
    stream = PolymarketSportsFeed(config)
    await feed.start()
    try:
        await stream.start()
        print("[tennis-sampler] PM 实时流已启动（市场自己的比分源）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[tennis-sampler] PM 实时流启动失败：{type(exc).__name__}: {exc}", flush=True)
        stream = None
    print(f"[tennis-sampler] 每 {interval:.1f}s 采一轮 → {SAMPLE_PATH}", flush=True)
    last_row = restore_last_states(SAMPLE_PATH)
    if last_row:
        print(f"[tennis-sampler] 已从样本文件恢复 {len(last_row)//3} 场比赛的去重状态", flush=True)
    try:
        while True:
            try:
                n = await sample_once(feed, overlay, stream, last_row)
                if n:
                    print(f"[tennis-sampler] {time.strftime('%H:%M:%S')} 记录 {n} 条", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[tennis-sampler] 本轮失败：{type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(interval)
    finally:
        if stream is not None:
            try:
                await stream.stop()
            except Exception:  # noqa: BLE001
                pass
        await feed.stop()


# ---------- 报告 ----------

def restore_last_states(path: str) -> dict:
    """重启后从样本文件恢复去重状态：否则重启会把"当前比分"再记一遍。

    之前只有内存版去重表，每次重启都会为每场比赛多写一条"假变化"，
    在报告里表现为 -98s / -109s 这种不可能出现的离群值（2026-09-28 实测）。
    """
    last_row: dict = {}
    if not os.path.exists(path):
        return last_row
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key_text = str(row.get("match_key") or "")
                if not key_text:
                    continue
                away, _, home = key_text.partition("|")
                key = (away, home)
                if row.get("src") == "pm" and row.get("state") is not None:
                    last_row[("pm", key)] = tuple(tuple(x) for x in row["state"])
                elif row.get("src") == "pair" and row.get("espn", {}).get("state") is not None:
                    espn = tuple(tuple(x) for x in row["espn"]["state"])
                    pm = row.get("pm") or {}
                    pm_state = (tuple(tuple(x) for x in pm["state"])
                                if pm.get("state") is not None else None)
                    last_row[("pair", key)] = (espn, pm_state)
                    last_row[("pair_t", key)] = float(row.get("t") or 0)
    except OSError as exc:
        print(f"[tennis-sampler] 恢复去重状态失败（忽略）：{exc}", flush=True)
    return last_row

def _load_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    # 读全部归档（带时间戳的历史 + 旧的 `.1`）
    for candidate in sorted(glob.glob(path + "*")):
        if not os.path.exists(candidate):
            continue
        with open(candidate, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.sort(key=lambda r: float(r.get("t") or 0))
    return rows


def pair_deltas(pair_rows: list[dict], pm_rows: list[dict],
                max_wait: float = 600.0) -> dict:
    """算出「PM 先报出某个比分」到「ESPN 也报出同一比分」的秒数差。

    纯函数（不联网、不读文件），方便单测。
    """
    espn_timeline: dict[str, list[tuple[float, tuple]]] = {}
    for r in pair_rows:
        state = r.get("espn", {}).get("state")
        if state is None:
            continue
        espn_timeline.setdefault(r["match_key"], []).append(
            (float(r["t"]), tuple(tuple(x) for x in state))
        )
    pm_events: dict[str, list[tuple[float, tuple]]] = {}
    for r in pm_rows:
        state = r.get("state")
        if state is None:
            continue
        pm_events.setdefault(r["match_key"], []).append(
            (float(r["t"]), tuple(tuple(x) for x in state))
        )

    deltas: list[tuple[float, str]] = []
    never_seen = 0
    for key, events in pm_events.items():
        events.sort()
        timeline = sorted(espn_timeline.get(key, []))
        if not timeline:
            never_seen += max(0, len(events) - 1)  # 起始状态不算
            continue
        first_state = events[0][1]
        # 只认**没见过的**状态：采样器重启后会丢掉内存里的去重表，把"当前比分"
        # 当成一次新变化再记一遍（实测制造出 -98s / -109s 两个假离群值）。
        # 比分是单调的，同一个 state 重复出现必然是重放，丢掉。
        seen_states = {first_state}
        for ts, state in events:
            if state in seen_states:
                continue  # 起始状态（两路对齐那一条）与重启重放都不算
            seen_states.add(state)
            hit = None
            for ets, estate in timeline:
                # 比分是单调的 → 同一个 state 在整场里只会出现一次，
                # 所以"ESPN 看到这个 state 的时刻"就是唯一解，正负都要收
                # （ESPN 更早时 delta 为负，同样是有信息量的一侧）。
                if estate == state:
                    hit = ets
                    break
            if hit is None or abs(hit - ts) > max_wait:
                never_seen += 1
                continue
            deltas.append((hit - ts, key))
    return {"deltas": deltas, "never_seen": never_seen,
            "matches": len(espn_timeline)}


def report(path: str = SAMPLE_PATH, detail: bool = False) -> int:
    if not os.path.exists(path):
        print(f"还没有样本文件 {path}")
        return 1
    rows = _load_rows(path)
    pair_rows = [r for r in rows if r.get("src") == "pair"]
    pm_rows = [r for r in rows if r.get("src") == "pm"]
    print(f"样本 {len(rows)} 条：pair={len(pair_rows)}（ESPN×PM 同 tick）  pm={len(pm_rows)}（PM 推送）")
    if rows:
        span = float(rows[-1]["t"]) - float(rows[0]["t"])
        print(f"时间范围：{time.strftime('%m-%d %H:%M', time.localtime(rows[0]['t']))}"
              f" ~ {time.strftime('%m-%d %H:%M', time.localtime(rows[-1]['t']))}"
              f"（{span / 3600:.1f} 小时）")
    result = pair_deltas(pair_rows, pm_rows)
    deltas = [d for d, _ in result["deltas"]]
    print(f"可配对的比赛：{result['matches']} 场；可测量的比分推进：{len(deltas)} 次；"
          f"ESPN 始终没对上：{result['never_seen']} 次")
    if not deltas:
        print("\n还没有可测量的比分推进 —— 需要至少一场两路都覆盖、且比分推进过的比赛。")
        return 0
    if detail:
        print("\n逐次比分推进（delta = ESPN − PM，正 = PM 更快）:")
        for delta, key in sorted(result["deltas"]):
            names = key.replace("|", " vs ")
            print(f"   {delta:+6.1f}s   {names}")
    deltas.sort()
    positive = sum(1 for d in deltas if d > 1.0)
    negative = sum(1 for d in deltas if d < -1.0)
    print(f"\nPM 先报出（>1s）: {positive}/{len(deltas)}"
          f"   ESPN 先报出（<-1s）: {negative}/{len(deltas)}"
          f"   同时（±1s）: {len(deltas) - positive - negative}")
    print(f"ESPN − PM 中位 {statistics.median(deltas):+.1f}s"
          f"  均值 {statistics.mean(deltas):+.1f}s"
          f"  p25 {deltas[len(deltas)//4]:+.1f}s"
          f"  p75 {deltas[len(deltas)*3//4]:+.1f}s")
    print("\n读法：正值 = PM 更早报出这个比分（我们走 ESPN 就慢了这么多）。"
          "\n     注意 ESPN 侧是轮询采样，含最多一个采样间隔的量化误差。")
    return 0


def selftest() -> int:
    """不联网校验报告逻辑：构造「PM 先 12 秒」这种已知答案，看能不能算出来。"""
    pair_rows = [
        {"src": "pair", "t": 1000.0, "match_key": "a|b",
         "espn": {"state": [[6, 0], [0, 0]]}},
        {"src": "pair", "t": 1010.0, "match_key": "a|b",
         "espn": {"state": [[6, 0], [0, 0]]}},
        {"src": "pair", "t": 1022.0, "match_key": "a|b",
         "espn": {"state": [[6, 0], [1, 0]]}},
        {"src": "pair", "t": 1034.0, "match_key": "a|b",
         "espn": {"state": [[6, 0], [1, 1]]}},
    ]
    pm_rows = [
        {"src": "pm", "t": 1000.0, "match_key": "a|b", "state": [[6, 0], [0, 0]]},
        {"src": "pm", "t": 1010.5, "match_key": "a|b", "state": [[6, 0], [1, 0]]},
        {"src": "pm", "t": 1030.0, "match_key": "a|b", "state": [[6, 0], [1, 1]]},
    ]
    res = pair_deltas(pair_rows, pm_rows)
    deltas = sorted(d for d, _ in res["deltas"])
    ok = deltas == [4.0, 11.5]
    print(f"selftest: deltas={deltas} never_seen={res['never_seen']} → "
          f"{'OK' if ok else 'FAIL'}")
    # 第二例：ESPN 永远没跟上 → 计入 never_seen
    res2 = pair_deltas(pair_rows, [
        {"src": "pm", "t": 1000.0, "match_key": "a|b", "state": [[6, 0], [0, 0]]},
        {"src": "pm", "t": 2000.0, "match_key": "a|b", "state": [[6, 0], [9, 9]]},
    ])
    ok2 = res2["never_seen"] == 1 and not res2["deltas"]
    print(f"selftest2: never_seen={res2['never_seen']} deltas={len(res2['deltas'])} → "
          f"{'OK' if ok2 else 'FAIL'}")
    return 0 if (ok and ok2) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--detail", action="store_true", help="报告里逐条列出每次比分推进")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--path", default=SAMPLE_PATH)
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.report:
        return report(args.path, detail=args.detail)
    if args.once:
        config = load_config()
        config.pm_stream_override_sports = tuple(
            set(config.pm_stream_override_sports) | {"TENNIS"}
        )
        feed = NBAFeed(config)
        overlay = SportsStreamOverlay(config)
        stream = PolymarketSportsFeed(config)

        async def smoke():
            await feed.start()
            try:
                await stream.start()
                return await sample_once(feed, overlay, stream, {})
            finally:
                await feed.stop()

        n = asyncio.run(smoke())
        print(f"采了一轮，写了 {n} 条 → {SAMPLE_PATH}")
        return 0
    return asyncio.run(run_forever(args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
