# -*- coding: utf-8 -*-
"""MLB 局面全量采集（只读）：ESPN 的 situation × Polymarket 实时盘口。

背景（2026-10-28 用户："MLB 触发时价格都 95+，翻车 1-2 场收益就清空，想放松规则"）
--------------------------------------------------------------------------------
1980 场 2026 赛季回放显示：放松阈值 = 胜率下降 = 打平价同步下降，**期望不变**；
而且实盘 <0.90 那一档是亏的（13 笔 76.9%）。原因是 "8 局 + 2 分" 这个规则
**看不到垒上有没有人、几出局**——市场给 0.88 时，往往在说"2 分领先但满垒 0 出局"。

所以真正值得验证的是：**用更细的局面识别"市场还没反应过来的真锁"**。
本工具只做采集，不下单、不影响交易。

输出 `trades/mlb_situation_ledger.jsonl`，每行一个"进行中的局面"：
  inning / half / 主客比分 / 分差 / outs / 壘上 / balls / strikes
  leader / best_bid / best_ask / ask_size / token_id / question
  raw_situation（ESPN 原始 situation 字典，字段名将来变了也能回看）

用法：
    python tools/mlb_situation_ledger.py                 # 常驻
    python tools/mlb_situation_ledger.py --once          # 采一轮（冒烟）
    python tools/mlb_situation_ledger.py --interval 15
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

from src.atomic_io import rotate_by_size  # noqa: E402
from src.config import load_config  # noqa: E402
from src.polymarket_feed import PolymarketFeed  # noqa: E402
from src.position_paths import ClobBookSource, summarize_book  # noqa: E402

SAMPLE_PATH = "trades/mlb_situation_ledger.jsonl"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
MAX_BYTES = 32 * 1024 * 1024
BACKUP_COUNT = 12
MARKET_TTL = 1800.0
MIN_INNING = 6      # 只记第 6 局及以后（覆盖 6/7/8 局各档，方便以后按规则回放）
MIN_DIFF = 1


def beijing_stamp(ts: float) -> str:
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def append_rows(rows: list[dict]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(SAMPLE_PATH), exist_ok=True)
    try:
        rotate_by_size(SAMPLE_PATH, max_bytes=MAX_BYTES, backup_count=BACKUP_COUNT)
    except OSError:
        pass
    with open(SAMPLE_PATH, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def fetch_scoreboard(session: aiohttp.ClientSession) -> list[dict]:
    try:
        async with session.get(SCOREBOARD, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 - 采集失败就跳过这一轮
        return []
    return data.get("events") or []


def parse_game(event: dict) -> dict | None:
    """把 ESPN 的一个 event 折成局面；不是进行中 / 不在窗口内就返回 None。"""
    status = (event.get("status") or {}).get("type") or {}
    if str(status.get("state") or "") != "in":
        return None
    comp = (event.get("competitions") or [{}])[0]
    period = int(status.get("period") or (comp.get("status") or {}).get("period") or 0)
    if period < MIN_INNING:
        return None
    detail = str(status.get("detail") or "").lower()
    half = "top" if detail.startswith("top") else ("bottom" if detail.startswith("bot") else "")
    teams = {}
    for side in comp.get("competitors") or []:
        key = str(side.get("homeAway") or "")
        if key in ("home", "away"):
            teams[key] = {
                "name": str((side.get("team") or {}).get("displayName") or ""),
                "short": str((side.get("team") or {}).get("name") or ""),
                "score": int(side.get("score") or 0),
            }
    if "home" not in teams or "away" not in teams:
        return None
    diff = teams["home"]["score"] - teams["away"]["score"]
    if abs(diff) < MIN_DIFF:
        return None
    leader_side = "home" if diff > 0 else "away"
    underdog_side = "away" if leader_side == "home" else "home"
    raw = comp.get("situation") or {}
    return {
        "game_id": str(event.get("id") or ""),
        "title": str(event.get("name") or ""),
        "inning": period,
        "half": half,
        "detail": str(status.get("detail") or ""),
        "home": teams["home"]["name"],
        "away": teams["away"]["name"],
        "home_score": teams["home"]["score"],
        "away_score": teams["away"]["score"],
        "diff": abs(diff),
        "leader": teams[leader_side]["name"],
        "leader_short": teams[leader_side]["short"],
        "underdog": teams[underdog_side]["name"],
        "underdog_short": teams[underdog_side]["short"],
        "leader_is_home": leader_side == "home",
        "outs": raw.get("outs"),
        "balls": raw.get("balls"),
        "strikes": raw.get("strikes"),
        "on_first": raw.get("onFirst"),
        "on_second": raw.get("onSecond"),
        "on_third": raw.get("onThird"),
        "raw_situation": raw,
    }


class MarketCache:
    """(game_id, leader) → token/question，带 TTL（领先方一变就重新解析）。"""

    def __init__(self, pm: PolymarketFeed, ttl: float = MARKET_TTL):
        self.pm = pm
        self.ttl = ttl
        self.items: dict[tuple, dict] = {}

    async def get(self, game_id: str, leader: str, underdog: str) -> dict:
        key = (str(game_id), str(leader))
        hit = self.items.get(key)
        if hit and time.time() - hit["at"] <= self.ttl:
            return hit
        info = {"token_id": "", "question": "", "at": time.time()}
        try:
            market = await self.pm.search_sports_markets("mlb", leader, underdog)
            if market is not None:
                index = self.pm.match_outcome_index(market.outcomes, leader)
                if 0 <= index < len(market.token_ids):
                    info["token_id"] = str(market.token_ids[index])
                info["question"] = str(market.question or "")
        except Exception:  # noqa: BLE001
            pass
        self.items[key] = info
        return info


async def sample_once(session, pm: PolymarketFeed, clob: ClobBookSource,
                      cache: MarketCache) -> int:
    events = await fetch_scoreboard(session)
    rows = []
    for event in events:
        state = parse_game(event)
        if state is None:
            continue
        info = await cache.get(state["game_id"], state["leader"], state["underdog"])
        snap = None
        source = ""
        if info.get("token_id"):
            try:
                book, source = await clob.fetch(info["token_id"])
                snap = summarize_book(book) if book else None
            except Exception:  # noqa: BLE001
                snap = None
        row_t = time.time()
        row = {
            "t": row_t,
            "beijing_time": beijing_stamp(row_t),
            **state,
            "best_bid": (snap or {}).get("best_bid"),
            "best_ask": (snap or {}).get("best_ask"),
            "mid": (snap or {}).get("mid"),
            "ask_size": (snap or {}).get("ask_size"),
            "tick_size": (snap or {}).get("tick_size"),
            "book_source": source,
            "token_id": info.get("token_id") or "",
            "question": info.get("question") or "",
        }
        rows.append(row)
    append_rows(rows)
    return len(rows)


async def run(interval: float) -> None:
    config = load_config()
    pm = PolymarketFeed(config)
    clob = ClobBookSource(config)
    await pm.start()
    await clob.start()
    cache = MarketCache(pm)
    print(f"[mlb-situation] 每 {interval:.0f}s 采一轮 → {SAMPLE_PATH}", flush=True)
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        try:
            while True:
                try:
                    written = await sample_once(session, pm, clob, cache)
                    if written:
                        print(f"[mlb-situation] {time.strftime('%H:%M:%S')} 记录 {written} 条", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[mlb-situation] 本轮失败：{type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(interval)
        finally:
            await clob.stop()
            await pm.stop()


async def once(interval: float) -> int:
    config = load_config()
    pm = PolymarketFeed(config)
    clob = ClobBookSource(config)
    await pm.start()
    await clob.start()
    cache = MarketCache(pm)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            n = await sample_once(session, pm, clob, cache)
        print(f"记录 {n} 条 → {SAMPLE_PATH}")
        return 0
    finally:
        await clob.stop()
        await pm.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=15.0)
    args = ap.parse_args()
    if args.once:
        return asyncio.run(once(args.interval))
    try:
        asyncio.run(run(args.interval))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
