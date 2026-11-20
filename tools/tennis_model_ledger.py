# -*- coding: utf-8 -*-
"""网球「全量信号账本」：把每个比赛状态连同**模型概率**与**市场价**一起记下来。

为什么要它（2026-09-29 用户："建立一本网球虚拟账本，比对概率模型跟市场定价，
看网球的概率模型能否跑赢市场定价，全量记录各种信号跟价格"）
--------------------------------------------------------------------------
现有账本回答不了这个问题，因为**它们只记"通过闸门"的那一小撮**：
  * 主账本：只有被启发式锁定 + 价格闸门放行的信号才有仓；
  * 概率账本：只有"手写概率公式 − 价格 ≥5pp 且价 ≤0.70"的才有仓，而且用的是
    Gamma 快照价（系统性比实时卖一低 10 美分，口径不可比）；
  * 网球 DP 模型（`src/tennis_model.win_probability`）**从来没参与过判定**，
    只在 19% 的信号上被顺带记录过（缺 `leader_serving` 时算不出来）。

所以这本账本换一个粒度：**不记"仓位"，记"状态快照"**。每场比赛每 `--interval`
（默认 15 秒；状态一变立即记）落一条：

  * 状态：盘分、当前盘局分、谁在发球（以及推出来的"这一盘谁先发"）
  * **模型概率**：DP 递推的结果；发球方未知时把两种假设都算出来取平均，
    并用 `serving_known` 标出，报告里可以只用"已知"那部分复核
  * **市场价**：实时 CLOB 的买一/卖一/中间价（能真正成交的价），
    另附 Gamma 快照价，用来持续量化"快照 vs 实时"的口径差
  * **各条规则此刻会不会触发**：启发式锁定 / 手写概率规则 / 模型边际是否超阈值

时间口径（2026-09-29 用户问"账本里的时间对不对、什么时区"后补）
  * `t` = **这个盘口真正被抓到的时刻**（epoch 秒，绝对时间，无时区歧义）；
    同一轮里多场比赛**各记各的时刻**（以前共用"本轮开始时刻"，会引入最多几百毫秒偏差）
  * `tick_t` = 这一轮采样的开始时刻（用来分组"同一次采样"）
  * `beijing_time` = 北京时间 ISO 串（与 `signal_events.jsonl` 同一格式），肉眼比对用
  * ⚠️ `t` 是"**我们看到的时刻**"，不是"**比分变化的时刻**"：ESPN 自己的发布时间
    没被记录，所以<b>不能用这本账本做"比分 vs 价格谁先动"的延迟分析</b> ——
    那件事归 `tools/tennis_latency_sampler.py`

有了它，三件事才能算：
  1. **校准**：模型说 P，实际赢了多少；
  2. **能否跑赢市场**：按「模型 P − 实时卖一」分档，看各档的实际 ROI；
  3. **规则对照**：同一批快照上，启发式 / 手写概率 / 模型边际阈值 各自的
     触发次数、胜率、入场价、ROI —— 同一批观测，没有选择偏差。

结算：采样器只写快照；报告按市场结算结果（Gamma，closed 且价已到 0/1）补齐胜负，
并存进 `trades/tennis_model_results.json` 缓存，查过的不再重复查。

用法：
    python tools/tennis_model_ledger.py                # 常驻采样（systemd: tennis-model-ledger）
    python tools/tennis_model_ledger.py --once         # 采一轮（冒烟）
    python tools/tennis_model_ledger.py --report       # 校准 + 边际分档 + 规则对照
    python tools/tennis_model_ledger.py --selftest     # 不联网，校验报告统计逻辑
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
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import settlement_lookup  # noqa: E402
from src import tennis_model  # noqa: E402
from src.atomic_io import rotate_by_size  # noqa: E402
from src.config import load_config  # noqa: E402
from src.multi_signal import (  # noqa: E402
    count_decided_sets,
    evaluate_tennis_lock,
    infer_break_state,
)
from src.nba_feed import NBAFeed  # noqa: E402
from src.polymarket_feed import PolymarketFeed  # noqa: E402
from src.position_paths import ClobBookSource, summarize_book  # noqa: E402
from src.probability_signal import evaluate_tennis_probability  # noqa: E402

SAMPLE_PATH = "trades/tennis_model_ledger.jsonl"
RESULT_CACHE = "trades/tennis_model_results.json"
MAX_BYTES = 64 * 1024 * 1024
# 2026-10-27：原来轮转成固定的 `.1`，每次轮转都会**覆盖上一代**（09-29~10-09 那段就
# 是这么丢的，导致回测只剩 19 天）。改成带时间戳的归档 + 保留 8 份（≈ 90 天）。
BACKUP_COUNT = 8
DEFAULT_INTERVAL_SECONDS = 15.0
# 状态没变也至少记一条的间隔。原为 120 秒，但那样"比分没变、价格在漂移"的画面
# 分辨率只有 2 分钟（而入场前的 watch_paths 是 3 秒）→ 2026-09-29 降到 30 秒。
# 代价：约 11 场 × 2 条/分 ≈ 3 万行/天 ≈ 17MB/天（文件按 64MB 轮转）。
DEFAULT_HEARTBEAT_SECONDS = 30.0
MARKET_TTL = 1800.0              # token 映射缓存 30 分钟（与预匹配 TTL 一致）
BOT_STATE_URL = "http://127.0.0.1:8082/api/tennis-state"


def matches_from_bot_payload(payload) -> list[dict]:
    """从机器人面板的 `/api/tennis-state` 里取出比赛行（只保留模型需要的形状）。

    为什么要走机器人而不是自己抓 ESPN（2026-09-30）：
      机器人每 3-5 秒就要抓一次 ESPN 网球 scoreboard，**每次 1.3MB**（atp 0.75MB + wta 0.60MB）。
      账本再独立抓一遍 = 同一份数据每 15 秒多拉 1.3MB，实测会被 ESPN 限流成
      `TimeoutError (atp); TimeoutError (wta)`，账本从 00:04 到 10:46 **一条都没写进去**
      （而同一时刻机器人自己抓是好的、curl 也是好的）。
      机器人那边的状态还是**已经叠加过 PM 实时流**的（比 ESPN 原始行更新），所以改成
      优先读它、ESPN 只做兜底 —— 既省带宽，也不会再被限流。
    """
    out: list[dict] = []
    for row in (payload or {}).get("matches") or []:
        if not isinstance(row, dict) or not row.get("match_id"):
            continue
        players = []
        ok = True
        for key in ("player1", "player2"):
            player = row.get(key)
            if not isinstance(player, dict):
                ok = False
                break
            players.append({
                "name": player.get("name"),
                "linescores": list(player.get("linescores") or []),
                "serving": player.get("serving"),
            })
        if not ok:
            continue
        out.append({
            "match_id": str(row["match_id"]),
            "player1": players[0],
            "player2": players[1],
            "status": str(row.get("status") or ""),
            "tournament": str(row.get("tournament") or ""),
        })
    return out


def fetch_from_bot(timeout: float = 5.0) -> list[dict]:
    """读机器人已抓好的网球状态（本机 HTTP，毫秒级，不碰 ESPN）。"""
    try:
        request = urllib.request.Request(BOT_STATE_URL, headers={"User-Agent": "tennis-model-ledger"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return matches_from_bot_payload(json.loads(response.read().decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 - 读不到就回退 ESPN
        print(f"[model-ledger] 读机器人网球状态失败（回退 ESPN）：{type(exc).__name__}: {exc}",
              flush=True)
        return []


def beijing_stamp(moment: float | None = None) -> str:
    """epoch → 北京时间 ISO 串（与 signal_events 的 beijing_time 同格式）。"""
    stamp = datetime.fromtimestamp(float(moment if moment is not None else time.time()),
                                   ZoneInfo("Asia/Shanghai"))
    return stamp.isoformat(timespec="seconds")


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


# ---------- 纯函数：状态解析 / 模型概率 / 规则判定（可离线测试） ----------

def parse_state(match: dict) -> dict | None:
    """把一行比赛折成「观察对象视角」的状态。

    口径（2026-09-30 放宽）：原来要求「至少打完 2 盘 + 盘分有领先」（沿用锁定规则的
    前置条件）→ **第一盘整盘、以及盘分持平的比赛（0-0 / 1-1）全都不记**。实测一场
    比赛跑到第二盘之前账本一条都没有，跟"全量记录"的目标不符（用户最初问的正是
    "开局领先、价格却走低"，那全发生在第一盘）。
    现在只要**有比分**就记：盘分有领先时观察对象=领先方；盘分持平时取 player1，
    并用 `sets_level=True` 标出来（报告里既能看"领先方"，也能单独看平盘）。
    """
    p1 = match.get("player1") or {}
    p2 = match.get("player2") or {}
    for player in (p1, p2):
        name = str(player.get("name") or "").strip()
        if name in ("", "?"):
            return None
    ls1 = [int(x) for x in (p1.get("linescores") or [])]
    ls2 = [int(x) for x in (p2.get("linescores") or [])]
    if not ls1 or not ls2:
        return None
    # 盘分只算**已经分出胜负的盘**（6-3 算赢一盘，3-1 只是这一盘暂时领先）。
    # 用 multi_signal.count_decided_sets —— 和锁定规则、模型同一口径；
    # `NBAFeed.fetch_tennis_matches` 里那个 `a > b` 的写法是历史遗留，别跟。
    sets1, sets2 = count_decided_sets(ls1, ls2)
    sets_level = sets1 == sets2
    if sets_level:
        # 平盘：以 player1 为观察对象（模型算他照样对，只是"领先方"这个词不适用）
        leader, underdog = p1, p2
        leader_is_p1 = True
        leader_sets = underdog_sets = sets1
    else:
        leader_is_p1 = sets1 > sets2
        leader, underdog = (p1, p2) if leader_is_p1 else (p2, p1)
        leader_sets, underdog_sets = (sets1, sets2) if leader_is_p1 else (sets2, sets1)
    index = min(len(ls1), len(ls2)) - 1
    serving = leader.get("serving")
    return {
        "leader": str(leader.get("name") or ""),
        "underdog": str(underdog.get("name") or ""),
        "leader_is_p1": leader_is_p1,
        "leader_sets": leader_sets,
        "underdog_sets": underdog_sets,
        "leader_games": int(leader["linescores"][index]),
        "underdog_games": int(underdog["linescores"][index]),
        "leader_serving": serving if isinstance(serving, bool) else None,
        "set_index": index + 1,
        # True = 盘分持平，此时 `leader` 只是"观察对象"（player1），不是领先方
        "sets_level": sets_level,
    }


def model_probabilities(state: dict) -> dict:
    """算 DP 模型概率。发球方未知时**两个假设都算**并给平均（不猜一侧）。

    `serving_known=False` 的那部分单独标出来：实测 73% 的网球信号缺
    `leader_serving`（ESPN 的 possession 字段），旧口径下这些信号根本没有模型值。
    """
    games_played = state["leader_games"] + state["underdog_games"]
    known_first = tennis_model.first_server_is_leader(state["leader_serving"], games_played)

    def prob(first_server: bool):
        try:
            return round(tennis_model.win_probability(
                leader_sets=state["leader_sets"],
                underdog_sets=state["underdog_sets"],
                current_games_leader=state["leader_games"],
                current_games_underdog=state["underdog_games"],
                leader_serves_first_in_set=first_server,
            ), 4)
        except Exception:  # noqa: BLE001 - 模型只是记录，不能影响采集
            return None

    if known_first is not None:
        value = prob(bool(known_first))
        return {"model_p": value, "serving_known": True,
                "model_p_leader_serves_first": value, "model_p_underdog_serves_first": None}
    first, second = prob(True), prob(False)
    average = round((first + second) / 2, 4) if (first is not None and second is not None) else None
    return {"model_p": average, "serving_known": False,
            "model_p_leader_serves_first": first, "model_p_underdog_serves_first": second}


def rule_flags(config, match: dict, ask) -> dict:
    """同一时刻各条规则会不会触发（纯记录，不改变任何交易行为）。"""
    flags = {"heuristic_lock": False, "prob_rule": False, "prob_rule_p": None}
    try:
        flags["heuristic_lock"] = evaluate_tennis_lock(config, match) is not None
    except Exception:  # noqa: BLE001
        pass
    try:
        signal = evaluate_tennis_probability(config, match)
        if signal is not None:
            flags["prob_rule_p"] = float(signal.estimated_probability)
            if ask is not None:
                flags["prob_rule"] = bool(
                    signal.estimated_probability - float(ask) >= config.probability_min_edge
                    and float(ask) <= config.probability_max_entry_price
                )
    except Exception:  # noqa: BLE001
        pass
    return flags


# ---------- 采样 ----------

class MarketCache:
    """(match_id, leader) → {token_id, question, gamma_ask}，带 TTL（省 Gamma 调用）。

    2026-10-09 修（PROJECT_DESIGN #131）：原来键是 **match_id**，但一场比赛里
    `leader` 会随盘分翻转 —— 盘分持平时 leader 被定义为 player1，某一方赢下一盘后
    leader 就变成那一方。match_id 命中缓存后，`token_id` 还是**旧领先方**的 token，
    采样器却按**新领先方**写 `leader` 字段，于是抓回来的盘口是**对手方**的价。
    证据：`Valentin Vacherot` 那一行采样记 ask 0.130、同一场模拟盘实际成交 0.890；
    低档（ask<0.60）86 条里 77 条的 gamma_ask 比 CLOB ask 高出 0.05 以上，
    而正常档 153 条里 0 条（见 #128）。

    现在按 (match_id, leader) 缓存：领先方一变就重新解析，token 和价格一定同源。
    """

    def __init__(self, pm: PolymarketFeed, ttl: float = MARKET_TTL):
        self.pm = pm
        self.ttl = ttl
        self.items: dict[str, dict] = {}

    async def get(self, match_id: str, leader: str, underdog: str) -> dict:
        # 领先方是缓存键的一部分：换边必须重新解析，否则会拿错 token
        key = (str(match_id), str(leader))
        hit = self.items.get(key)
        if hit and time.time() - hit["at"] <= self.ttl:
            return hit
        info = {"token_id": "", "question": "", "gamma_ask": None, "at": time.time()}
        try:
            market = await self.pm.search_sports_markets("tennis", leader, underdog)
            if market is not None:
                index = self.pm.match_outcome_index(market.outcomes, leader)
                if 0 <= index < len(market.token_ids):
                    info["token_id"] = str(market.token_ids[index])
                info["question"] = str(market.question or "")
                if market.best_ask:
                    info["gamma_ask"] = float(market.best_ask)
        except Exception:  # noqa: BLE001 - 取不到就留空，下一轮重试
            pass
        self.items[key] = info
        return info


async def load_matches(feed: NBAFeed, source: str, retries: int = 3):
    """拿这一轮的比赛行：优先机器人（本机），失败再回退 ESPN（带重试）。

    返回 (rows, src)。`src` 写进每一行，事后能分清哪些快照来自哪条源。
    只在**真的报错**时才重试 ESPN —— "没有比赛"是合法结果，不该白拉三次 1.3MB。
    """
    if source in ("auto", "bot"):
        rows = await asyncio.to_thread(fetch_from_bot)
        if rows:
            return rows, "bot"
        if source == "bot":
            return [], "bot"
    for attempt in range(max(1, retries)):
        try:
            rows = await feed.fetch_tennis_matches()
        except Exception as exc:  # noqa: BLE001
            rows = []
            print(f"[model-ledger] ESPN 抓取异常：{type(exc).__name__}: {exc}", flush=True)
        if rows:
            return rows, "espn"
        health = (feed.health() or {}).get("TENNIS") or {}
        if not health.get("last_error"):
            return [], "espn"          # 正常但没有进行中的比赛
        print(f"[model-ledger] ESPN 本轮报错（{health.get('last_error')}），第 {attempt + 1} 次重试",
              flush=True)
        await asyncio.sleep(1.5 * (attempt + 1))
    return [], "espn"


async def sample_once(feed: NBAFeed, clob: ClobBookSource, cache: MarketCache,
                      last_state: dict, heartbeat: float = DEFAULT_HEARTBEAT_SECONDS,
                      source: str = "auto") -> int:
    config = feed.config
    matches, src = await load_matches(feed, source)
    if not matches:
        print(f"[model-ledger] 本轮没有可用的比赛行（源={src}）", flush=True)
        return 0
    tick_t = time.time()          # 本轮采样开始时刻（分组用）
    rows: list[dict] = []
    live_ids = set()
    for match in matches:
        state = parse_state(match)
        if state is None:
            continue
        match_id = str(match.get("match_id") or "")
        if not match_id:
            continue
        live_ids.add(match_id)
        signature = (state["leader_sets"], state["underdog_sets"], state["leader_games"],
                     state["underdog_games"], state["leader_serving"])
        previous = last_state.get(match_id)
        if (previous is not None and signature == previous[0]
                and tick_t - previous[1] < heartbeat):
            continue
        info = await cache.get(match_id, state["leader"], state["underdog"])
        snap = None
        book_source = ""
        if info.get("token_id"):
            try:
                book, book_source = await clob.fetch(info["token_id"])
                snap = summarize_book(book) if book else None
            except Exception:  # noqa: BLE001
                snap = None
        # 每行记**自己**的取价时刻（盘口是一个个抓的，同轮各场会差几百毫秒）
        row_t = time.time()
        ask = float(snap["best_ask"]) if snap and snap.get("best_ask") is not None else None
        mid = float(snap["mid"]) if snap and snap.get("mid") is not None else None
        model = model_probabilities(state)
        row = {
            "t": row_t,
            "tick_t": tick_t,
            "beijing_time": beijing_stamp(row_t),
            "src": src,
            "match_id": match_id,
            "tournament": str(match.get("tournament") or match.get("league") or ""),
            **state,
            **model,
            "best_bid": (snap or {}).get("best_bid"),
            "best_ask": ask,
            "mid": mid,
            "spread": (snap or {}).get("spread"),
            "ask_size": (snap or {}).get("ask_size"),
            "tick_size": (snap or {}).get("tick_size"),
            "book_source": book_source,
            "gamma_ask": info.get("gamma_ask"),
            "token_id": info.get("token_id") or "",
            "question": info.get("question") or "",
            **rule_flags(config, match, ask),
        }
        if model["model_p"] is not None and mid is not None:
            row["edge_vs_mid_pp"] = round((model["model_p"] - mid) * 100, 2)
        if model["model_p"] is not None and ask is not None:
            row["edge_vs_ask_pp"] = round((model["model_p"] - ask) * 100, 2)
        if mid is not None and ask is not None and info.get("gamma_ask") is not None:
            row["gamma_gap_cents"] = round((ask - float(info["gamma_ask"])) * 100, 2)
        rows.append(row)
        last_state[match_id] = (signature, row_t)
    append_rows(rows)
    for gone in [k for k in last_state if k not in live_ids]:
        last_state.pop(gone, None)
    return len(rows)


async def run_forever(interval: float, heartbeat: float = DEFAULT_HEARTBEAT_SECONDS,
                      source: str = "auto") -> int:
    config = load_config()
    feed = NBAFeed(config)
    pm = PolymarketFeed(config)
    clob = ClobBookSource(config)
    await feed.start()
    await pm.start()
    await clob.start()
    cache = MarketCache(pm)
    last_state: dict = {}
    print(f"[model-ledger] 每 {interval:.0f}s 采一轮；状态没变每 {heartbeat:.0f}s 记一条；"
          f"数据源={source} → {SAMPLE_PATH}", flush=True)
    try:
        while True:
            try:
                written = await sample_once(feed, clob, cache, last_state, heartbeat, source)
                if written:
                    print(f"[model-ledger] {time.strftime('%H:%M:%S')} 记录 {written} 条", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[model-ledger] 本轮失败：{type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(interval)
    finally:
        await clob.stop()
        await pm.stop()
        await feed.stop()


# ---------- 报告 ----------

def load_rows(path: str = SAMPLE_PATH) -> list[dict]:
    rows: list[dict] = []
    # 读**全部**归档（带时间戳的历史 + 旧的 `.1`），否则轮转一次就丢掉前面的样本。
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


def _load_result_cache() -> dict:
    if not os.path.exists(RESULT_CACHE):
        return {}
    try:
        return json.loads(open(RESULT_CACHE, encoding="utf-8").read()) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_result_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(RESULT_CACHE), exist_ok=True)
        with open(RESULT_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError as exc:
        print(f"[model-ledger] 结果缓存写入失败：{exc}", flush=True)


def _result_key(row: dict) -> str:
    return "|".join((row.get("token_id") or "", row.get("leader") or ""))


def resolve_results(rows: list[dict], *, allow_network: bool = True) -> dict:
    """给每个 (token, 领先方) 组合查终局价；返回 {key: 0/1} 缓存。"""
    cache = _load_result_cache()
    keys = {_result_key(r) for r in rows if r.get("token_id") and r.get("leader")}
    todo = [k for k in keys if k not in cache]
    if todo and allow_network:
        for key in todo:
            token, _, team = key.partition("|")
            question = next((r.get("question") for r in rows
                             if _result_key(r) == key and r.get("question")), "")
            try:
                price, note = settlement_lookup.resolve_outcome_price(question, token, team)
            except Exception as exc:  # noqa: BLE001
                price, note = None, f"查询异常 {exc}"
            if price is not None:
                cache[key] = price
            elif note:
                cache.setdefault("_notes", {})[key] = note
        _save_result_cache(cache)
    return cache


def _won(cache: dict, row: dict):
    value = cache.get(_result_key(row))
    if value in (0, 1, 0.0, 1.0):
        return int(value) == 1
    return None


def build_samples(rows: list[dict], cache: dict) -> list[dict]:
    """把账本行（字段是 `best_ask`）折成分析用的样本（统一成 `ask`）。

    2026-09-29 踩过：报告里直接读 `row["ask"]`，而采样器写的是 `best_ask` →
    **所有真实样本被静默过滤掉**（报告显示"还没有可用的结算结果"），
    而合成测试用的是 `ask` 所以没抓到。改成显式归一化 + 用真实字段形状测。
    """
    samples: list[dict] = []
    for row in rows:
        won = _won(cache, row)
        ask = row.get("best_ask")
        if won is None or ask is None:
            continue
        samples.append({**row, "won": won, "ask": float(ask)})
    return samples


def calibrate(samples: list[dict], buckets=(0.5, 0.7, 0.8, 0.85, 0.9, 0.95, 1.01)) -> list[dict]:
    out = []
    for lo, hi in zip(buckets, buckets[1:]):
        sub = [s for s in samples if s["model_p"] is not None and lo <= s["model_p"] < hi]
        if not sub:
            continue
        out.append({
            "lo": lo, "hi": hi, "n": len(sub),
            "model_mean": statistics.mean(s["model_p"] for s in sub),
            "win_rate": sum(1 for s in sub if s["won"]) / len(sub),
        })
    return out


def edge_buckets(samples: list[dict], edges=(-99, -5, 0, 3, 5, 10, 20, 99)) -> list[dict]:
    """按「模型 P − 实时卖一」分档，看买入持有到结算的 ROI（每笔等额 1 单位）。"""
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sub = [s for s in samples if s.get("edge_vs_ask_pp") is not None and lo <= s["edge_vs_ask_pp"] < hi]
        if not sub:
            continue
        cost = sum(s["ask"] for s in sub)
        pnl = sum((1.0 - s["ask"]) if s["won"] else (-s["ask"]) for s in sub)
        out.append({
            "lo": lo, "hi": hi, "n": len(sub),
            "win_rate": sum(1 for s in sub if s["won"]) / len(sub),
            "avg_ask": statistics.mean(s["ask"] for s in sub),
            "roi": (pnl / cost) if cost else None,
        })
    return out


def rule_compare(samples: list[dict]) -> list[dict]:
    rules = [
        ("启发式锁定（现在实盘用的）", lambda s: s.get("heuristic_lock")),
        ("模型边际 ≥3pp", lambda s: (s.get("edge_vs_ask_pp") or -99) >= 3),
        ("模型边际 ≥5pp", lambda s: (s.get("edge_vs_ask_pp") or -99) >= 5),
        ("模型边际 ≥10pp", lambda s: (s.get("edge_vs_ask_pp") or -99) >= 10),
        ("手写概率规则（价≤0.70 且边际≥5pp）", lambda s: s.get("prob_rule")),
    ]
    out = []
    for name, predicate in rules:
        sub = [s for s in samples if predicate(s) and s.get("ask")]
        if not sub:
            out.append({"rule": name, "n": 0})
            continue
        cost = sum(s["ask"] for s in sub)
        pnl = sum((1.0 - s["ask"]) if s["won"] else (-s["ask"]) for s in sub)
        out.append({
            "rule": name, "n": len(sub),
            "win_rate": sum(1 for s in sub if s["won"]) / len(sub),
            "avg_ask": statistics.mean(s["ask"] for s in sub),
            "roi": (pnl / cost) if cost else None,
        })
    return out


def _one_per_match(rows: list[dict]) -> list[dict]:
    """同一场比赛只留**第一条**快照。

    一场比赛会落几十条快照，直接当独立样本会把样本量虚增几倍（而且同场快照高度相关）。
    """
    seen: dict[str, dict] = {}
    for row in rows:
        seen.setdefault(str(row.get("match_id")), row)
    return list(seen.values())


def _first_per_match(rows: list[dict], predicate=None) -> list[dict]:
    """先按条件过滤，再**每场只留第一条**。

    D 表（按状态分档）用这个口径：同一场比赛在同一个状态维度上只会命中最靠前的那条，
    既避免了"同场几十条快照被当独立样本"，又比"整场只取第一条"覆盖更多状态。
    """
    seen: dict[str, dict] = {}
    for row in rows:
        if predicate is not None and not predicate(row):
            continue
        seen.setdefault(str(row.get("match_id")), row)
    return list(seen.values())


def _bucket_stats(rows: list[dict]) -> dict:
    """一个状态分档的汇总：样本量 / 模型均值 / 市场均价 / 平均边际 / 实际胜率 / ROI。"""
    if not rows:
        return {"n": 0}
    cost = sum(r["ask"] for r in rows)
    pnl = sum((1.0 - r["ask"]) if r["won"] else (-r["ask"]) for r in rows)
    models = [r["model_p"] for r in rows if r.get("model_p") is not None]
    edges = [r["edge_vs_ask_pp"] for r in rows if r.get("edge_vs_ask_pp") is not None]
    return {
        "n": len(rows),
        "win_rate": sum(1 for r in rows if r["won"]) / len(rows),
        "avg_ask": statistics.mean(r["ask"] for r in rows),
        "model_mean": statistics.mean(models) if models else None,
        "edge_mean": statistics.mean(edges) if edges else None,
        "roi": (pnl / cost) if cost else None,
    }


def _break_count(row: dict):
    """「领先方至少破发几次」——用账本里的原始局分 + 发球方现算（不依赖采集时算过没有）。"""
    try:
        _, count = infer_break_state(
            int(row.get("leader_games") or 0),
            int(row.get("underdog_games") or 0),
            row.get("leader_serving"),
        )
    except Exception:  # noqa: BLE001
        return None
    return count


def state_dimensions() -> list[tuple]:
    """D 表的维度定义：(名字, 判定)。真破发/局数差都从已记录字段派生。"""
    def lead_games(row: dict) -> int:
        return int(row.get("leader_games") or 0) - int(row.get("underdog_games") or 0)

    def sets_ahead(row: dict) -> bool:
        """盘分真的有领先（排除"平盘时以 player1 当观察对象"的那些行）。"""
        return (not row.get("sets_level")) and (int(row.get("leader_sets") or 0)
                                                > int(row.get("underdog_sets") or 0))

    return [
        ("盘分持平（观察对象=player1）", lambda r: bool(r.get("sets_level"))),
        ("盘分领先", sets_ahead),
        ("第 2 盘", lambda r: r.get("set_index") == 2),
        ("第 3 盘（决胜盘）", lambda r: r.get("set_index") == 3),
        ("当前盘领先 ≥2 局", lambda r: lead_games(r) >= 2),
        ("当前盘领先 1 局", lambda r: lead_games(r) == 1),
        ("当前盘持平", lambda r: lead_games(r) == 0),
        ("当前盘落后（领先方被反超）", lambda r: lead_games(r) < 0),
        ("领先方在发球", lambda r: r.get("leader_serving") is True),
        ("领先方不在发球", lambda r: r.get("leader_serving") is False),
        ("发球方未知", lambda r: r.get("serving_known") is False),
        ("真破发 ≥1", lambda r: (_break_count(r) or 0) >= 1),
        ("无破发（各自保发）", lambda r: _break_count(r) == 0),
        ("★ 盘分领先＋当前盘领先≥1＋真破发",
         lambda r: sets_ahead(r) and lead_games(r) >= 1 and (_break_count(r) or 0) >= 1),
        ("★ 盘分领先＋当前盘领先≥1＋无破发",
         lambda r: sets_ahead(r) and lead_games(r) >= 1 and _break_count(r) == 0),
    ]


def report(path: str = SAMPLE_PATH, *, network: bool = True, per_match: bool = True) -> int:
    rows = load_rows(path)
    if not rows:
        print(f"还没有样本（{path}）")
        return 1
    cache = resolve_results(rows, allow_network=network)
    samples = build_samples(rows, cache)
    if not samples:
        print(f"快照 {len(rows)} 条，但还没有可用的结算结果（市场还没结算完或 token 没配上）")
        return 0
    print(f"快照 {len(rows)} 条 → 有实时价且有结算的 {len(samples)} 条，覆盖 "
          f"{len({s['match_id'] for s in samples})} 场比赛"
          f"（其中 {sum(1 for s in samples if s.get('serving_known'))} 条知道谁在发球）")
    span = samples[-1]["t"] - samples[0]["t"]
    print(f"时间范围 {time.strftime('%m-%d %H:%M', time.localtime(samples[0]['t']))} ~ "
          f"{time.strftime('%m-%d %H:%M', time.localtime(samples[-1]['t']))}（{span / 3600:.1f} 小时）")
    gaps = [s["gamma_gap_cents"] for s in samples if s.get("gamma_gap_cents") is not None]
    if gaps:
        print(f"Gamma 快照价 vs 实时卖一：中位 {statistics.median(gaps):+.2f} 美分（n={len(gaps)}）")

    pool = _one_per_match(samples) if per_match else samples
    label = "每场一条" if per_match else "全部快照"
    print(f"\n=== A) 模型校准（{label}，n={len(pool)}）===")
    print("   模型P区间       n   模型均值   实际胜率    偏差")
    for bucket in calibrate(pool):
        print(f"   [{bucket['lo']:.2f},{bucket['hi']:.2f})  {bucket['n']:4d}    "
              f"{bucket['model_mean'] * 100:6.1f}%   {bucket['win_rate'] * 100:6.1f}%   "
              f"{(bucket['win_rate'] - bucket['model_mean']) * 100:+5.1f}pp")

    print("\n=== B) 模型边际 vs 市场（按实时卖一买入，持有到结算）===")
    print("   边际区间(pp)    n   胜率    平均入场价    ROI")
    for bucket in edge_buckets(pool):
        roi = f"{bucket['roi'] * 100:+6.2f}%" if bucket["roi"] is not None else "    —"
        print(f"   [{bucket['lo']:>4},{bucket['hi']:>4})  {bucket['n']:4d}  {bucket['win_rate'] * 100:5.1f}%   "
              f"{bucket['avg_ask']:.3f}      {roi}")

    print("\n=== C) 规则对照（同一批观测）===")
    print("   规则                                    n   胜率    平均入场价    ROI")
    for item in rule_compare(pool):
        if not item["n"]:
            print(f"   {item['rule']:38s}   0      —         —          —")
            continue
        print(f"   {item['rule']:38s}{item['n']:4d}  {item['win_rate'] * 100:5.1f}%   "
              f"{item['avg_ask']:.3f}     {item['roi'] * 100:+6.2f}%")

    print("\n=== D) 按状态分档（每场每个状态只取第一条快照）===")
    print("   状态                                n   模型均值  市场均价  平均边际   实际胜率    ROI")
    for name, predicate in state_dimensions():
        rows_in_bucket = _first_per_match(samples, predicate)
        stat = _bucket_stats(rows_in_bucket)
        if not stat["n"]:
            print(f"   {name:34s}   0        —        —        —         —        —")
            continue
        model_mean = f"{stat['model_mean'] * 100:6.1f}%" if stat["model_mean"] is not None else "     —"
        edge_mean = f"{stat['edge_mean']:+6.1f}pp" if stat["edge_mean"] is not None else "      —"
        roi = f"{stat['roi'] * 100:+6.2f}%" if stat["roi"] is not None else "     —"
        print(f"   {name:34s}{stat['n']:4d}   {model_mean}   {stat['avg_ask']:.3f}   {edge_mean}   "
              f"{stat['win_rate'] * 100:6.1f}%   {roi}")
    print("\n读法：A 看模型准不准；B 看「模型认为市场低估的幅度」能不能变成钱；")
    print("     C 是同一批快照上三条规则的表现 —— 没有选择偏差，这是唯一能横比规则的证据。")
    print("     D 回答的是更实用的问题：模型在**哪种状态**下比市场准（★ 两行是关键对照）。")
    return 0


def selftest() -> int:
    """用合成数据校验统计逻辑（校准 / 边际分档 / 规则对照 / 按场去重）。"""
    rows = []
    for i in range(20):
        high = i < 10
        model_p = 0.90 if high else 0.70
        won = (i % 10) < (9 if high else 7)
        rows.append({"match_id": f"M{i}", "t": 1000.0 + i, "model_p": model_p,
                     "edge_vs_ask_pp": (model_p - 0.85) * 100, "ask": 0.85, "won": won,
                     "heuristic_lock": high})
    cal = calibrate(rows)
    ok1 = (len(cal) == 2 and abs(cal[0]["win_rate"] - 0.7) < 1e-9
           and abs(cal[1]["win_rate"] - 0.9) < 1e-9)
    expected_roi = (7 * 0.15 - 3 * 0.85) / (10 * 0.85)
    ok2 = any(b["n"] == 10 and abs(b["roi"] - expected_roi) < 1e-6 for b in edge_buckets(rows))
    rules = {r["rule"]: r for r in rule_compare(rows)}
    ok3 = rules["启发式锁定（现在实盘用的）"]["n"] == 10
    ok4 = len(_one_per_match([{"match_id": "A"}, {"match_id": "A"}, {"match_id": "B"}])) == 2
    many = [{"match_id": "X", "t": 1 + k, "model_p": 0.9, "ask": 0.8, "won": True,
             "edge_vs_ask_pp": 10.0} for k in range(5)]
    ok5 = len(_one_per_match(many)) == 1
    checks = (("校准分档", ok1), ("边际分档 ROI", ok2), ("规则对照", ok3),
              ("按场去重", ok4), ("同场多条只算一次", ok5))
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(ok for _, ok in checks) else 1


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    ap.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT_SECONDS,
                    help="状态没变时也记一条的间隔（秒），默认 30")
    ap.add_argument("--source", choices=("auto", "bot", "espn"), default="auto",
                    help="比赛状态来源：auto=优先机器人（本机）/bot=只读机器人/espn=自己抓")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-network", action="store_true", help="报告不联网查结算（只用缓存）")
    ap.add_argument("--all-snapshots", action="store_true", help="报告用全部快照而不是每场一条")
    ap.add_argument("--path", default=SAMPLE_PATH)
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.selftest:
        return selftest()
    if args.report:
        return report(args.path, network=not args.no_network, per_match=not args.all_snapshots)
    if args.once:
        async def smoke():
            config = load_config()
            feed = NBAFeed(config)
            pm = PolymarketFeed(config)
            clob = ClobBookSource(config)
            await feed.start()
            await pm.start()
            await clob.start()
            try:
                return await sample_once(feed, clob, MarketCache(pm), {},
                                         source=args.source)
            finally:
                await clob.stop()
                await pm.stop()
                await feed.stop()
        written = asyncio.run(smoke())
        print(f"采了一轮，写了 {written} 条 → {SAMPLE_PATH}")
        return 0
    return asyncio.run(run_forever(args.interval, args.heartbeat, args.source))


if __name__ == "__main__":
    raise SystemExit(main())
