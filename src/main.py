# -*- coding: utf-8 -*-
"""Main bot loop - polls NBA/MLB/tennis games, detects locks, records paper trades."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

from src.config import load_config
from src.atomic_io import atomic_write_json
from src.cs2_signal import evaluate_cs2_lock, parse_cs2_score
from src.dashboard import Dashboard
from src.multi_signal import (
    evaluate_college_basketball_lock,
    evaluate_football_lock,
    evaluate_hockey_lock,
    evaluate_mlb_lock,
    evaluate_tennis_lock,
    tennis_watch_state,
)
from src.nba_feed import NBAFeed
from src.live_trader import LiveTrader
from src.polymarket_feed import MarketPrice, PolymarketFeed
from src.paper_trader import PaperTrader
from src.position_paths import ClobBookSource, PositionPathRecorder, summarize_book
from src.series_tie_shadow import (
    SeriesTieShadowTracker,
    cs2_deciding_map_tie_state,
)
from src.tennis_tie_shadow import TennisTieShadowTracker, deciding_tie_state
from src.tennis_tie_events import TennisTieEventRecorder
from src.watch_paths import WatchPathRecorder
from src.probability_signal import (
    evaluate_mlb_probability,
    evaluate_nba_probability,
    evaluate_tennis_probability,
)
from src.probability_trader import ProbabilityPaperTrader
from src.settlement_reconcile import SettlementReconciler
from src.signal import evaluate_lock, LockSignal
from src.soccer_model import SoccerModel
from src.soccer_signal import evaluate_soccer_lock, soccer_price_gate
from src.sports_ws_feed import PolymarketSportsFeed
from src.sports_overlay import SportsStreamOverlay
from src import tennis_model

logger = logging.getLogger(__name__)


class LockBot:
    def __init__(self):
        self.config = load_config()
        self.nba = NBAFeed(self.config)
        self.sports_feed = PolymarketSportsFeed(self.config)
        self.pm = PolymarketFeed(self.config)
        self.trader = PaperTrader(self.config)
        self.probability_trader = ProbabilityPaperTrader(self.config)
        self.tennis_tie_events = TennisTieEventRecorder(self.config)
        self.live_trader = LiveTrader(self.config)
        self.live_trader.set_tie_event_recorder(self.tennis_tie_events)
        # 持仓期间盘口采集：只读，供日后用真实买一价复核止损策略
        self.path_recorder = PositionPathRecorder(
            self.config,
            source=ClobBookSource(self.config, client_getter=self._live_clob_client),
        )
        self._path_task: asyncio.Task | None = None
        self.tennis_tie_shadow = TennisTieShadowTracker(self.config)
        self._tie_shadow_task: asyncio.Task | None = None
        self.cs2_tie_shadow = SeriesTieShadowTracker(self.config)
        self._cs2_tie_shadow_task: asyncio.Task | None = None
        self._cs2_tie_shadow_tasks: set[asyncio.Task] = set()
        self._cs2_tie_shadow_inflight: set[str] = set()
        # 入场**前**盘口采集（2026-09-28）：position_paths 只采入场之后，
        # 「信号触发前价格已经走到哪」从来没被记录过，导致"市场未收敛"这个
        # 前提验证不出结论。这里只读盘口、只追加写文件，见 src/watch_paths.py。
        self.watch_recorder = WatchPathRecorder(
            self.config,
            source=ClobBookSource(self.config, client_getter=self._live_clob_client),
        )
        self._watch_task: asyncio.Task | None = None
        # 模拟盘结算兜底：比分流只在"看到终局那一 tick"结算，WS 在比赛结束前后
        # 断线就会把仓位永久挂成 OPEN（2026-09-19 CS2 1681114 实测丢了一笔亏损）。
        # 这条路径与传输层无关，直接问市场结算结果，见 src/settlement_reconcile.py。
        self.settlement_reconciler = SettlementReconciler(self.config)
        self._settle_task: asyncio.Task | None = None
        # 2026-09-20：被价格闸门/边际闸门**拒掉**的锁定信号，也把盘口路径采下来。
        # 为什么需要：验证"市场未收敛"这个前提，靠的是"边际大小 vs 入场后漂移"的
        # 横截面差分，而那需要**低边际甚至负边际的对照组** —— 只采我们持仓的样本时
        # 没有对照组（足球闸门要求边际≥2pp，所有成交单都是高边际）。
        # 结构化：token_id → (登记时刻, 目标对象)，跑满 TTL 自动清掉。
        self._path_candidates: dict[str, tuple[float, object]] = {}
        self.dashboard = Dashboard(
            self.config,
            self.trader,
            self.live_trader,
            self.probability_trader,
            path_recorder=self.path_recorder,
            watch_recorder=self.watch_recorder,
            settlement_reconciler=self.settlement_reconciler,
            tie_shadow=self.tennis_tie_shadow,
            cs2_tie_shadow=self.cs2_tie_shadow,
        )
        self.fired_nba: set[str] = set()
        self.fired_wnba: set[str] = set()
        self.fired_mlb: set[str] = set()
        self.fired_tennis: set[str] = set()
        self.fired_cs2: set[str] = set()
        self.fired_nfl: set[str] = set()
        self.fired_cfb: set[str] = set()
        self.fired_nhl: set[str] = set()
        self.fired_ncaab: set[str] = set()
        self.fired_ncaaw: set[str] = set()
        self.fired_soccer: set[str] = set()
        # 足球专用：价格不合适时不"一场只算一次"（比分随时变，下一 tick 可能就有价值了），
        # 但不能每 3 秒写一条拒绝日志 —— 只在原因变化时记一次。
        self._soccer_gate_logged: dict[str, str] = {}
        # 盘口映射缓存：(联赛 tag, 主队名, 客队名) -> (market, 缓存时间)
        # 每 tick 重跑一次 Gamma 搜索（甚至两次）没必要：token 映射是稳定的，
        # 真正要实时的是盘口价（那一步走 CLOB，另行读取）。
        self._soccer_market_cache: dict[tuple, tuple] = {}
        # 盘口未匹配时的日志去重（(运动, game_id, 原因) -> 已记过），
        # 配合否定缓存：暂时找不到盘口的比赛会重试，但日志不重复刷
        self._reject_log_throttle: dict[tuple, str] = {}
        # 足球的预匹配要用专用匹配器，需要知道比赛属于哪个联赛 tag
        self._soccer_tag_by_game: dict[str, str] = {}
        self._drill_fired_at: dict[str, float] = {}
        # 「这个市场已经判过了」——**按 token/market id 记，不是按 game_id**。
        # 同一场比赛会以两个 id 出现：PM 实时流给 6 位 id（824143），ESPN 给 9 位
        # （401816952），预匹配又会把两个 id 都映射到**同一个市场**。原来只按 game_id
        # 去重，于是同一市场被评估两次、甚至买两次（2026-09-16 HOU 10:11 @0.94 与
        # 10:22 @0.95 是同一个 token；09-12 SD 亦然）。
        self._decided_markets: dict[str, float] = {}
        self._decided_markets_prob: dict[str, float] = {}
        # 「这个市场正在被评估」——tick 轮询和实时流回调可能在同一秒处理同一场，
        # 两边都通过了"已判过"检查、又都在等盘口（await 期间会让出控制权），
        # 于是各下一单。这里给一个短租约把它们错开（判完就释放，见 _skip_no_price）。
        self._inflight_markets: dict[str, float] = {}
        self.completed_games: set[str] = set()
        for position in self.trader.positions.values():
            if position.sport == "NBA":
                self.fired_nba.add(position.game_id)
            elif position.sport == "MLB":
                self.fired_mlb.add(position.game_id)
            elif position.sport == "TENNIS":
                self.fired_tennis.add(position.game_id)
            # 按市场补记：重启后即使 game_id 对不上（PM 流 id ↔ ESPN id），
            # 只要市场已经持有过就不会再买一次
            self._mark_market_decided({
                "token_id": getattr(position, "token_id", ""),
                "market_id": getattr(position, "market_id", ""),
            })
        self._probability_processed_path = Path(self.config.probability_processed_path)
        self.probability_processed: set[str] = self._load_probability_processed()
        self.cached_tennis: list = []
        self.cached_cs2: list = []
        self._cs2_inflight: set[str] = set()
        self._tennis_task: asyncio.Task | None = None
        # 预匹配缓存：(运动, gameId) -> 盘口映射；用于跳过下单前的 Gamma 搜索
        self._prematch: dict[tuple[str, str], dict] = {}
        self._prematch_tasks: set[asyncio.Task] = set()
        self._prematch_inflight: set[tuple[str, str]] = set()
        # Polymarket 实时流覆盖层：校验通过后把 PM 比分叠加到 ESPN 快照上
        self.sports_overlay = SportsStreamOverlay(self.config)
        self._last_stream_rows: dict[str, list] = {}
        self._stream_inflight: set[str] = set()
        self._score_change_state_path = Path(self.config.score_change_state_path)
        self.score_change_state: dict[str, dict] = self._load_score_change_state()

    def _should_skip_fired(self, bucket: set[str], game_id: str) -> bool:
        """演练模式下允许同一场比赛按冷却时间重复触发，否则沿用一次性去重。"""
        if not self.config.signal_drill_mode:
            return game_id in bucket
        last = self._drill_fired_at.get(game_id, 0.0)
        cooldown = max(1, self.config.signal_drill_cooldown_seconds)
        return (time.time() - last) < cooldown

    # 「同一场比赛只做一次」的第二个维度：market/token。
    # 一场比赛在系统里可能有两个 game_id（PM 实时流 6 位 vs ESPN 9 位），
    # 只按 game_id 去重会漏。这里用 token_id 与 market_id 双键，覆盖两种情形：
    #   * 同一市场换 id 再来 → token 相同 → 拦住；
    #   * 同一场比赛的另一边（对手 token 不同）→ market_id 相同 → 也拦住
    #     （与原语义一致：一场比赛只做一次，不做双边）。
    DECIDED_MARKET_TTL_SECONDS = 6 * 3600   # 一场比赛不会挂 6 小时，够覆盖整天赛程

    @staticmethod
    def _decided_keys(details: dict | None) -> list[str]:
        details = details or {}
        token_id = str(details.get("token_id") or "").strip()
        market_id = str(details.get("market_id") or "").strip()
        keys = []
        if token_id:
            keys.append(f"tok:{token_id}")
        if market_id:
            keys.append(f"mkt:{market_id}")
        return keys

    @staticmethod
    def _prune_decided(registry: dict[str, float], ttl: float) -> None:
        if len(registry) <= 4000:
            return
        cutoff = time.time() - ttl
        for key in [k for k, stamp in registry.items() if stamp < cutoff]:
            registry.pop(key, None)

    def _decided_registry(self, *, probability: bool = False) -> dict[str, float]:
        """取市场登记表（按需补建：测试夹具常用 `LockBot.__new__` 造对象、不跑 __init__）。"""
        name = "_decided_markets_prob" if probability else "_decided_markets"
        registry = getattr(self, name, None)
        if registry is None:
            registry = {}
            setattr(self, name, registry)
        return registry

    def _market_already_decided(self, details: dict | None, *, probability: bool = False) -> str:
        """这个市场是不是已经判过（开过仓或已被闸门拒过）。返回命中的键，没有则空串。"""
        # 演练模式本来就是"同一场反复跑通链路"，去重会把它废掉；那边实盘单只走 dry-run
        if getattr(self.config, "signal_drill_mode", False):
            return ""
        registry = self._decided_registry(probability=probability)
        now = time.time()
        for key in self._decided_keys(details):
            stamp = registry.get(key)
            if stamp is None:
                continue
            if now - stamp > self.DECIDED_MARKET_TTL_SECONDS:
                registry.pop(key, None)
                continue
            return key
        return ""

    def _mark_market_decided(self, details: dict | None, *, probability: bool = False) -> None:
        registry = self._decided_registry(probability=probability)
        now = time.time()
        for key in self._decided_keys(details):
            registry[key] = now
        self._prune_decided(registry, self.DECIDED_MARKET_TTL_SECONDS)

    # ---------- 「这个市场正在评估」的短租约（防 tick 与实时流并发重复下单）----------

    INFLIGHT_MARKET_TTL_SECONDS = 120

    def _inflight_registry(self) -> dict[str, float]:
        registry = getattr(self, "_inflight_markets", None)
        if registry is None:
            registry = {}
            self._inflight_markets = registry
        return registry

    def _market_in_flight(self, details: dict | None) -> str:
        """是否已有另一条链路正在评估这个市场；返回命中的键或空串。"""
        if getattr(self.config, "signal_drill_mode", False):
            return ""
        registry = self._inflight_registry()
        now = time.time()
        for key in self._decided_keys(details):
            stamp = registry.get(key)
            if stamp is None:
                continue
            if now - stamp > self.INFLIGHT_MARKET_TTL_SECONDS:
                registry.pop(key, None)     # 兜底：异常路径没释放也不会永久占用
                continue
            return key
        return ""

    def _reserve_market(self, details: dict | None) -> None:
        registry = self._inflight_registry()
        now = time.time()
        for key in self._decided_keys(details):
            registry[key] = now
        self._prune_decided(registry, self.INFLIGHT_MARKET_TTL_SECONDS)

    def _release_market(self, details: dict | None) -> None:
        """本轮没做决定（盘口读不到、要重试）→ 立刻释放租约，让下一 tick 能接着重试。"""
        registry = self._inflight_registry()
        for key in self._decided_keys(details):
            registry.pop(key, None)

    def _mark_fired(self, bucket: set[str], game_id: str, details: dict | None = None) -> None:
        """记住「这场已经判过了」（开仓成功、或被闸门判定为不可交易）。

        `details` 里带着 token_id/market_id 时，**同时**按市场记一笔 —— 这是
        「同一场比赛两个 game_id」的唯一可靠去重方式（见 `_decided_markets` 注释）。
        拿不到盘口时的跳过（NO_ASK/NO_BOOK）走的是 `_skip_no_price`，不调用这里，
        所以「没人卖」仍然会在下一 tick 重试。
        """
        bucket.add(game_id)
        self._mark_market_decided(details)
        if self.config.signal_drill_mode:
            self._drill_fired_at[game_id] = time.time()

    # ---------- 市场预匹配 ----------

    def _prematch_key(self, sport: str, game_id: str) -> tuple[str, str]:
        return (sport.upper(), str(game_id))

    def _prematch_entry(self, sport: str, game_id: str):
        """取原始缓存条目（**含否定结果**）；过期自动清除。

        缓存条目有两种：
          * 命中：{"market_id": ..., "token_ids": [...], "found": True}
          * 未命中：{"found": False, "reason": "NO_MARKET"}  ← 短 TTL

        为什么要有否定缓存：`_prematch_game` 找不到盘口时如果不写任何东西，
        这场比赛下一 tick 又会被排进队列重试，而它在候选表里要待很久
        （网球 2-3 小时）。结果就是队列前排被"永远匹配不上"的比赛长期占住，
        后面的比赛永远轮不到 —— 2026-09-14 实测网球整个下午 0 次预匹配成功。
        """
        if not game_id:
            return None
        key = self._prematch_key(sport, game_id)
        entry = self._prematch.get(key)
        if not entry:
            return None
        if entry.get("found", True):
            ttl = max(60, int(getattr(self.config, "prematch_ttl_seconds", 1800)))
        else:
            ttl = max(30, int(getattr(self.config, "prematch_miss_ttl_seconds", 300)))
        if time.time() - float(entry.get("matched_at") or 0) > ttl:
            self._prematch.pop(key, None)
            return None
        return entry

    def get_prematched_market(self, sport: str, game_id: str):
        """取预匹配**命中**结果；否定结果不返回（但保留在缓存里占位）。"""
        entry = self._prematch_entry(sport, game_id)
        if not entry or not entry.get("found", True):
            return None
        return entry

    def known_no_market(self, sport: str, game_id: str) -> bool:
        """这场比赛是否已被判定"当前找不到盘口"（用于避免重复搜索）。"""
        entry = self._prematch_entry(sport, game_id)
        return bool(entry and not entry.get("found", True))

    # 「暂时性」的盘口失败：市场可能稍后才挂出来（网球尤其常见），
    # 不该像价格闸门那样"一场只判一次"直接放弃。
    # MARKET_IN_FLIGHT：同一市场正被另一条链路评估，本轮让开、下一 tick 再来。
    TRANSIENT_MARKET_REASONS = {"NO_MARKET", "NO_TOKEN", "MARKET_IN_FLIGHT"}

    def _skip_no_price(self, signal, sport: str, game_id, fired, details) -> None:
        """盘口读不到 / 没有卖单时的处理：跳过本轮、**不放弃这场**。

        这是可重试状态（别人随时可能挂出卖单），所以不 _mark_fired；
        日志按 (运动, 比赛, 原因) 只记首次，避免每 3 秒刷一条。
        """
        sport_upper = str(sport).upper()
        reason = "NO_ASK" if details.get("book_available") else "NO_BOOK"
        # 这一轮没做决定 → 把"正在评估"的租约还回去，下一 tick 立刻可以重试
        self._release_market(details)
        token = (sport_upper, str(game_id), reason)
        if token in self._reject_log_throttle:
            return
        if len(self._reject_log_throttle) > 2000:
            self._reject_log_throttle.clear()
        self._reject_log_throttle[token] = reason
        self.trader.log_signal_rejected(signal, reason, sport_upper, details)
        logger.info(
            "%s 跳过（%s）：%s | 盘口读到了但无卖单" if reason == "NO_ASK"
            else "%s 跳过（%s）：%s | 盘口读取失败",
            sport_upper, reason,
            getattr(signal, "leader_name", getattr(signal, "leader_team", "?")),
        )

    def _reject_market_signal(self, signal, reason: str, sport: str, game_id, fired, details) -> None:
        """盘口没匹配上时的统一处理。

        * 暂时性原因（NO_MARKET / NO_TOKEN）：**不永久放弃这场** ——
          交给否定缓存限流（默认 5 分钟后再试一次），日志只记首次；
        * 其它原因（运动被禁用等）：维持原行为，一场只判一次。
        """
        sport_upper = str(sport).upper()
        if reason in self.TRANSIENT_MARKET_REASONS:
            token = (sport_upper, str(game_id), reason)
            if token in self._reject_log_throttle:
                return  # 已经在重试窗口里记过，静默等待下一轮
            if len(self._reject_log_throttle) > 2000:
                self._reject_log_throttle.clear()
            self._reject_log_throttle[token] = reason
            self.trader.log_signal_rejected(signal, reason, sport_upper, details)
            return
        self._observe_rejected_lock(sport_upper, details, reason)
        self._mark_fired(fired, game_id, details)
        self.trader.log_signal_rejected(signal, reason, sport_upper, details)

    # 只有这些原因是"价格/边际不够"——它们对应的市场**依然是锁定盘**，
    # 路径有意义；NO_MARKET / DUPLICATE_* / MARKET_IN_FLIGHT 不采（没 token 或会刷屏）。
    OBSERVABLE_REJECTIONS = {
        "PRICE_ABOVE_LIMIT",
        "PRICE_BELOW_LOCK_THRESHOLD",
        "EXPECTED_PROFIT_TOO_LOW",
        "SOCCER_EDGE_TOO_LOW",
    }
    # 观察对象保留多久：够覆盖"入场后 120 秒"的检验窗口，又不至于无限堆积
    OBSERVE_TTL_SECONDS = 900.0

    def _observe_rejected_lock(self, sport: str, details: dict | None, reason: str) -> None:
        """把被闸门拒掉的锁定信号登记成"采集目标"（只读盘口，不下单）。

        目的见 `_path_candidates` 的注释：给"边际 vs 入场后漂移"的检验补上对照组。
        任何异常都不影响主流程 —— 这是纯观测。
        """
        try:
            if reason not in self.OBSERVABLE_REJECTIONS or not isinstance(details, dict):
                return
            token = str(details.get("token_id") or "")
            price = details.get("live_entry_price") or details.get("entry_price")
            if not token or not price:
                return
            now = time.time()
            if len(self._path_candidates) > 200:
                for k, (ts, _obj) in list(self._path_candidates.items()):
                    if now - ts > self.OBSERVE_TTL_SECONDS:
                        self._path_candidates.pop(k, None)
            eid = str(details.get("event_id") or "")
            self._path_candidates[token] = (
                now,
                SimpleNamespace(
                    # 用 OBS_ 前缀把它和我们真正持仓的 LP_/POS_ 区分开
                    position_id=f"OBS_{eid or token[-10:]}",
                    game_id=str(details.get("game_id") or details.get("market_id") or ""),
                    event_id=eid,
                    sport=sport,
                    strategy="observe_rejected_lock",
                    team=str(details.get("search_term") or details.get("team") or ""),
                    token_id=token,
                    entry_price=float(price),
                    size=0.0,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 观测失败绝不能影响交易
            logger.debug("observe rejected lock failed: %s", exc)

    def _schedule_prematch(self, candidates: list) -> None:
        """为候选比赛排队预匹配；每轮最多启动 N 个，避免打爆 Gamma。

        按运动**轮转**取候选（而不是按候选表顺序从头取）：原实现里网球排在最后，
        CS2 等运动只要有新比赛进来就会把每轮的名额吃光，网球长期饿死。
        轮转让每个运动每轮都有机会，谁也堵不住谁。
        """
        if not getattr(self.config, "prematch_enabled", True):
            return
        per_tick = max(0, int(getattr(self.config, "prematch_max_per_tick", 3)))
        max_concurrent = max(1, int(getattr(self.config, "prematch_max_concurrent", 3)))
        # 按运动分组（组内保持原有先后），再轮流取
        buckets: dict[str, list] = {}
        for item in candidates:
            sport = str(item[0]).upper()
            buckets.setdefault(sport, []).append(item)
        ordered: list = []
        index = 0
        while True:
            added = False
            for sport in list(buckets.keys()):
                bucket = buckets[sport]
                if index < len(bucket):
                    ordered.append(bucket[index])
                    added = True
            if not added:
                break
            index += 1

        started = 0
        for sport, game_id, team_a, team_b in ordered:
            if started >= per_tick:
                break
            if not game_id or not team_a or not team_b:
                continue
            key = self._prematch_key(sport, game_id)
            # _prematch_entry 会顺手清掉过期条目（含否定缓存），所以这里不能只看 key 在不在
            if self._prematch_entry(sport, game_id) or key in self._prematch_inflight:
                continue
            if len(self._prematch_tasks) >= max_concurrent:
                break
            self._prematch_inflight.add(key)
            task = asyncio.create_task(self._prematch_game(sport, game_id, team_a, team_b))
            self._prematch_tasks.add(task)
            task.add_done_callback(self._prematch_tasks.discard)
            started += 1

    async def _prematch_game(self, sport: str, game_id: str, team_a: str, team_b: str) -> None:
        key = self._prematch_key(sport, game_id)
        try:
            if str(sport).upper() == "SOCCER":
                # 足球是三个独立二元盘（主胜/平局/客胜），必须走专用匹配器，
                # 否则永远匹配不上、还会把否定缓存写成"没有盘口"
                market = await self.pm.search_soccer_winner_market(
                    self._soccer_tag_by_game.get(game_id, "soccer"),
                    team_a,
                    team_b,
                    getattr(self.config, "sport_market_tag_ids", {}).get("SOCCER"),
                )
            else:
                market = await self.pm.search_sports_markets(
                    sport.lower(), team_a, team_b, None,
                    getattr(self.config, "sport_market_tag_ids", {}).get(sport.upper()),
                )
            if market is None:
                # 否定缓存：占位，避免这场比赛每 tick 重排把队列堵死（见 _prematch_entry）
                self._prematch[key] = {"found": False, "reason": "NO_MARKET", "matched_at": time.time()}
                return
            self._prematch[key] = {
                "found": True,
                "market_id": market.event_id,
                "question": market.question,
                "outcomes": list(market.outcomes),
                "token_ids": list(market.token_ids),
                # 记下这份快照的价格是「哪个 outcome 的」：
                # 预匹配用的是候选里的第一个球员，而信号可能来自第二个球员，
                # 使用时必须按需要翻面，否则会拿对手的价格去判断（2026-09-15 修）。
                # 认不出来就存 -1（不要默认 0 = 猜一侧），使用处见到 <0 不翻面。
                "leader_index": self.pm.match_outcome_index(market.outcomes, team_a),
                "best_bid": market.best_bid,
                "best_ask": market.best_ask,
                # 真实 tick：模拟盘记账要按「卖一 + 1 tick」算成交价，
                # 硬编码 0.01 会在 0.001 tick 的市场上把成本高估 0.9 分（2026-09-28）
                "tick_size": float(getattr(market, "tick_size", 0.01) or 0.01),
                "matched_at": time.time(),
            }
            logger.info(
                "PREMATCH %s %s -> %s", sport, game_id, market.question[:44]
            )
            await self._warm_order_metadata(market.token_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("预匹配失败 %s %s: %s", sport, game_id, exc)
        finally:
            self._prematch_inflight.discard(key)

    async def _warm_order_metadata(self, token_ids: list) -> None:
        """预热 tick size / neg risk 元数据，信号触发时签名就只剩本地计算。"""
        trader = self.live_trader
        client = getattr(trader, "client", None) if trader else None
        if client is None or not getattr(trader, "client_ready", False):
            return
        for token_id in list(token_ids)[:2]:
            try:
                await asyncio.to_thread(
                    client.create_limit_order,
                    token_id=token_id, price=0.5, size=1.0, side="BUY",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("元数据预热失败 %s: %s", str(token_id)[:12], exc)

    async def _on_sports_update(self, game: dict):
        """实时流事件驱动入口：状态一变就评估信号，不等主循环轮询。

        CS2 直接使用实时流自身的比分；MLB / NBA / WNBA / NFL / CFB / NHL / TENNIS
        走覆盖层（PM 比分先与 ESPN 对齐口径，再叠加到最近一次轮询得到的比赛行；
        网球比的是逐盘局分，见 SportsStreamOverlay._apply_to_tennis_row）。
        """
        if not getattr(self.config, "signal_event_driven", True):
            return
        league = str(game.get("leagueAbbreviation") or "").lower()
        if league != "cs2":
            sport = self.sports_overlay.ingest(game)
            if sport:
                await self._on_stream_trigger(sport, game)
            return
        self.cs2_tie_shadow.settle_completed_game(game)
        if not game.get("live") or game.get("ended"):
            return
        self._schedule_cs2_tie_shadow(game)
        game_id = str(game.get("gameId") or "")
        if not game_id or self._should_skip_fired(self.fired_cs2, game_id):
            return
        # 先用本地判定过滤，只有真的锁定才走联网的市场匹配
        if evaluate_cs2_lock(self.config, game) is None:
            return
        if game_id in self._cs2_inflight:
            return
        self._cs2_inflight.add(game_id)
        try:
            logger.info(
                "EVENT-DRIVEN CS2: gameId=%s score=%s", game_id, game.get("score")
            )
            await self._process_cs2(game)
        finally:
            self._cs2_inflight.discard(game_id)

    async def _on_stream_trigger(self, sport: str, payload: dict):
        """PM 推送到达时立刻用最新比分评估一次锁定信号。"""
        entry = self.sports_overlay.entry_for_payload(sport, payload)
        if entry is None or entry.first_score is None:
            return
        rows = self._last_stream_rows.get(sport) or []
        if not rows:
            return
        target = None
        for index, row in enumerate(rows):
            matched = self.sports_overlay.match_row(sport, row)
            if matched is not entry:
                continue
            merged, applied = self.sports_overlay.apply_to_row(sport, row)
            # 未校验（本 tick 只完成口径对齐）或比分未变时不触发
            if applied:
                # 立即刷新缓存，避免同一比分的重复推送再次触发评估
                rows[index] = merged
                target = merged
            break
        if target is None:
            return
        key = f"{sport}:{entry.away_key}@{entry.home_key}"
        if key in self._stream_inflight:
            return
        self._stream_inflight.add(key)
        try:
            logger.info(
                "EVENT-DRIVEN %s: %s vs %s %s",
                sport, entry.away_name, entry.home_name, entry.label(),
            )
            if sport == "MLB":
                await self._process_mlb(target)
            elif sport == "NBA":
                await self._process_nba(target)
            elif sport == "WNBA":
                await self._process_wnba(target)
            elif sport == "TENNIS":
                await self._process_tennis(target)
            elif sport in ("NFL", "CFB", "NHL"):
                await self._process_clock_sport(target, sport)
        finally:
            self._stream_inflight.discard(key)

    async def run(self):
        await self.nba.start()
        await self.sports_feed.start()
        await self.pm.start()
        # 事件驱动：实时流一有状态变化就评估，不再等 10 秒主循环
        self.sports_feed.on_game_update = self._on_sports_update
        await self.live_trader.start()
        await self.dashboard.start()
        self._start_path_recorder()
        self._start_watch_recorder()
        self._start_settlement_reconciler()
        if self.tennis_tie_shadow.enabled:
            self._tie_shadow_task = asyncio.create_task(
                self.tennis_tie_shadow.run_forever()
            )
        if self.cs2_tie_shadow.enabled:
            self._cs2_tie_shadow_task = asyncio.create_task(
                self.cs2_tie_shadow.run_forever()
            )
        logger.info("Dashboard at http://127.0.0.1:8082")
        logger.info("Bot started. Budget=%.2f, poll_interval=%ds",
                    self.config.starting_budget, self.config.nba_poll_interval)
        try:
            while True:
                await self._tick()
                await asyncio.sleep(self.config.nba_poll_interval)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            await self._stop_path_recorder()
            await self._stop_watch_recorder()
            await self._stop_settlement_reconciler()
            task = self._tie_shadow_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._tie_shadow_task = None
            for task in list(self._cs2_tie_shadow_tasks):
                task.cancel()
            if self._cs2_tie_shadow_tasks:
                await asyncio.gather(
                    *self._cs2_tie_shadow_tasks, return_exceptions=True
                )
                self._cs2_tie_shadow_tasks.clear()
            task = self._cs2_tie_shadow_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._cs2_tie_shadow_task = None
            self.trader.save_state()
            self.probability_trader.save_state()
            await self.nba.stop()
            await self.sports_feed.stop()
            await self.pm.stop()
            await self.live_trader.stop()
            await self.dashboard.stop()
            self._print_summary()

    # ---------- 持仓盘口采集 ----------

    def _live_clob_client(self):
        """复用实盘执行器的 CLOB 客户端（已就绪时免签名、免额外连接）。"""
        trader = self.live_trader
        if trader is None or not getattr(trader, "client_ready", False):
            return None
        return getattr(trader, "client", None)

    def _path_targets(self) -> list[tuple[str, object]]:
        """所有未结算持仓，带上所属账本名。

        2026-09-21：**实盘持仓也纳进来了**（`ledger="live"`）。
        原因是复盘"网球买入后回撤"时发现：`position_paths.jsonl` 一直只采模拟盘，
        实盘仓位没有任何入场后价格路径 —— 于是"补单""是不是买晚了"这类问题全部
        只能靠模拟盘推断，而模拟盘胜率（网球 93.5%）明显比实盘（89.7%）乐观。
        这一步纯只读（照样只发公开 `/book`），只是把观察对象补齐。
        """
        targets = (
            [("paper", p) for p in self.trader.open_positions]
            + [("probability", p) for p in self.probability_trader.open_positions]
        )
        live_trader = self.live_trader
        if live_trader is not None:
            targets += [
                ("live", p)
                for p in (getattr(live_trader, "positions", {}) or {}).values()
                if str(getattr(p, "status", "") or "") == "OPEN"
                and str(getattr(p, "token_id", "") or "")
            ]
        # 2026-09-20：再加上"被闸门拒掉但仍是锁定盘"的观察目标（只读盘口，不下单），
        # 给"边际 vs 入场后漂移"的检验提供低/负边际的对照组。过期的顺手清掉。
        if self._path_candidates:
            now = time.time()
            alive = {k: v for k, v in self._path_candidates.items()
                     if now - v[0] <= self.OBSERVE_TTL_SECONDS}
            self._path_candidates = alive
            targets += [("observe", obj) for _ts, obj in alive.values()]
        return targets

    def _start_path_recorder(self) -> None:
        if not self.path_recorder.enabled:
            logger.info("持仓盘口采集已关闭（POSITION_PATH_ENABLED=0）")
            return
        self._path_task = asyncio.create_task(
            self.path_recorder.run_forever(self._path_targets)
        )
        self._path_task.add_done_callback(self._clear_path_task)

    # ---------- 入场前盘口采集 ----------

    def _watch_targets(self) -> list[tuple]:
        """入场前采样目标：预匹配成功、且已进入"潜在锁定"状态的盘口。

        只挑"信号可能就在接下来几分钟内触发"的比赛 —— 全部比赛都采会让请求量
        失控，而在盘分还没分出领先时，价格对判断"我们是不是来晚了"没有信息量。
        """
        recorder = self.watch_recorder
        if not recorder.enabled or "TENNIS" not in recorder.sports:
            return []
        targets: list[tuple] = []
        for match in self.cached_tennis or []:
            try:
                state = tennis_watch_state(match)
            except Exception as exc:  # noqa: BLE001 - 纯观测，绝不影响主流程
                logger.debug("watch state 计算失败：%s", exc)
                continue
            if not state:
                continue
            leader, note = state
            game_id = str(match.get("match_id") or "")
            if not game_id:
                continue
            prematched = self.get_prematched_market("TENNIS", game_id)
            if not prematched or not prematched.get("found"):
                continue
            outcomes = list(prematched.get("outcomes") or [])
            token_ids = list(prematched.get("token_ids") or [])
            if not outcomes or not token_ids:
                continue
            index = self.pm.match_outcome_index(outcomes, leader)
            if not (0 <= index < len(token_ids)):
                # 认不出是哪一侧时退回第一个 token：记录里带 outcome 字段，
                # 事后可以按名字还原到底采的是哪一边（宁可采错侧也不丢样本）。
                index = 0
            targets.append(
                (
                    "TENNIS",
                    game_id,
                    token_ids[index],
                    outcomes[index] if index < len(outcomes) else "",
                    note,
                )
            )
        return targets

    def _start_watch_recorder(self) -> None:
        if not self.watch_recorder.enabled:
            logger.info("入场前盘口采集已关闭（WATCH_PATH_ENABLED=0）")
            return
        self._watch_task = asyncio.create_task(
            self.watch_recorder.run_forever(self._watch_targets)
        )
        self._watch_task.add_done_callback(self._clear_watch_task)

    def _clear_watch_task(self, task) -> None:
        self._watch_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("入场前盘口采集循环已退出：%s", exc)

    async def _stop_watch_recorder(self) -> None:
        task = self._watch_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不阻断主流程
            pass
        self._watch_task = None

    def _clear_path_task(self, task) -> None:
        self._path_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("持仓盘口采集循环已退出：%s", exc)

    async def _stop_path_recorder(self) -> None:
        task = self._path_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不阻断主流程
            pass
        self._path_task = None

    # ---------- 模拟盘结算兜底 ----------

    def _settlement_targets(self) -> list[tuple[str, object, object]]:
        """两个模拟盘账本里所有未结算持仓，带上所属账本名与结算器。"""
        return (
            [("paper", self.trader, p) for p in self.trader.open_positions]
            + [("probability", self.probability_trader, p)
               for p in self.probability_trader.open_positions]
        )

    def _start_settlement_reconciler(self) -> None:
        if not self.settlement_reconciler.enabled:
            logger.info("模拟盘结算兜底未启动（已关闭）")
            return
        self._settle_task = asyncio.create_task(
            self.settlement_reconciler.run_forever(self._settlement_targets)
        )
        self._settle_task.add_done_callback(self._clear_settle_task)

    def _clear_settle_task(self, task) -> None:
        self._settle_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("模拟盘结算兜底循环已退出：%s", exc)

    async def _stop_settlement_reconciler(self) -> None:
        task = self._settle_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不阻断主流程
            pass
        self._settle_task = None

    async def _tick(self):
        nba_games = await self.nba.fetch_games()
        mlb_games = await self.nba.fetch_mlb_games()
        wnba_games = await self.nba.fetch_wnba_games()
        ncaab_games = await self.nba.fetch_ncaab_games()
        ncaaw_games = await self.nba.fetch_ncaaw_games()
        soccer_games = await self.nba.fetch_soccer_games()
        nfl_games = await self.nba.fetch_nfl_games()
        cfb_games = await self.nba.fetch_cfb_games()
        nhl_games = await self.nba.fetch_nhl_games()
        self._sync_stream_snapshot()
        nba_games, _ = self.sports_overlay.merge_rows("NBA", nba_games)
        mlb_games, _ = self.sports_overlay.merge_rows("MLB", mlb_games)
        wnba_games, _ = self.sports_overlay.merge_rows("WNBA", wnba_games)
        ncaab_games, _ = self.sports_overlay.merge_rows("NCAAB", ncaab_games)
        ncaaw_games, _ = self.sports_overlay.merge_rows("NCAAW", ncaaw_games)
        nfl_games, _ = self.sports_overlay.merge_rows("NFL", nfl_games)
        cfb_games, _ = self.sports_overlay.merge_rows("CFB", cfb_games)
        nhl_games, _ = self.sports_overlay.merge_rows("NHL", nhl_games)
        self._last_stream_rows = {
            "NBA": nba_games,
            "MLB": mlb_games,
            "WNBA": wnba_games,
            "NCAAB": ncaab_games,
            "NCAAW": ncaaw_games,
            "NFL": nfl_games,
            "CFB": cfb_games,
            "NHL": nhl_games,
            # 网球在后台任务里更新，这里保留上一条，别把它冲掉
            "TENNIS": self._last_stream_rows.get("TENNIS", []),
        }
        cs2_all = self.sports_feed.snapshot("cs2")
        cs2_live = self.sports_feed.live_snapshot("cs2")
        self.cached_cs2 = cs2_all
        for game in cs2_all:
            self.cs2_tie_shadow.settle_completed_game(game)
        for game in cs2_live:
            self._schedule_cs2_tie_shadow(game)

        # 预匹配：把可能触发的比赛的盘口映射与订单元数据提前准备好
        candidates = [
            ("CS2", str(g.get("gameId", "")), g.get("homeTeam"), g.get("awayTeam"))
            for g in cs2_live
        ]
        for sport, games in (
            ("NBA", nba_games),
            ("WNBA", wnba_games),
            ("NCAAB", ncaab_games),
            ("NCAAW", ncaaw_games),
            ("NFL", nfl_games),
            ("CFB", cfb_games),
            ("NHL", nhl_games),
        ):
            for game in games:
                if getattr(game, "status", "") == "in":
                    candidates.append(
                        (sport, game.game_id, game.home_team_name, game.away_team_name)
                    )
        # 足球：单场比赛有三个结果盘，预匹配按「客队 vs 主队」全名准备
        for game in soccer_games:
            if getattr(game, "status", "") == "in":
                self._soccer_tag_by_game[game.game_id] = game.league_tag
                candidates.append(
                    ("SOCCER", game.game_id, game.home_team_name, game.away_team_name)
                )
        for game in mlb_games:
            if game.get("status") == "in":
                candidates.append(
                    ("MLB", game["game_id"], game["home_team_name"], game["away_team_name"])
                )
        for match in self.cached_tennis:
            candidates.append((
                "TENNIS",
                match.get("match_id", ""),
                (match.get("player1") or {}).get("name", ""),
                (match.get("player2") or {}).get("name", ""),
            ))
        self._schedule_prematch(candidates)

        await self._check_settlements(
            nba_games, mlb_games, self.cached_tennis, wnba_games, cs2_all,
            nfl_games, cfb_games, nhl_games, ncaab_games, ncaaw_games, soccer_games,
        )
        await self._update_dashboard_games(
            nba_games, mlb_games, self.cached_tennis, wnba_games, cs2_all,
            nfl_games, cfb_games, nhl_games, ncaab_games, ncaaw_games, soccer_games,
        )

        for game in nba_games:
            await self._process_nba(game)
        for game in wnba_games:
            await self._process_wnba(game)
        for game in ncaab_games:
            await self._process_college_basketball(game, "NCAAB")
        for game in ncaaw_games:
            await self._process_college_basketball(game, "NCAAW")
        for game in soccer_games:
            await self._process_soccer(game)
        for game in nfl_games:
            await self._process_clock_sport(game, "NFL")
        for game in cfb_games:
            await self._process_clock_sport(game, "CFB")
        for game in nhl_games:
            await self._process_clock_sport(game, "NHL")
        for game in mlb_games:
            await self._process_mlb(game)
        for game in cs2_live:
            await self._process_cs2(game)

        if not self.config.signal_drill_mode:
            await self._process_probability_candidates(nba_games, mlb_games, self.cached_tennis)

        # Fetch tennis in background - it's slow due to large payload
        if not self.config.signal_drill_mode and self._tennis_task is None:
            # 网球抓取要 2~5 秒，轮询缩短后必须防止任务堆积
            self._tennis_task = asyncio.create_task(self._fetch_and_process_tennis())
            self._tennis_task.add_done_callback(self._clear_tennis_task)

    def _clear_tennis_task(self, _task) -> None:
        self._tennis_task = None

    def _sync_stream_snapshot(self) -> None:
        """把实时流缓存的每一场比赛都喂给覆盖层。

        实时流只在状态变化时推送，进程启动后如果比赛没有新变化就拿不到数据；
        用快照补齐，保证「校验 → 覆盖」链路在第一个 tick 就能开始工作。
        """
        if not self.sports_overlay.enabled:
            return
        for row in self.sports_feed.snapshot():
            self.sports_overlay.ingest(row)

    def _market_status(self, sport: str, game_id: str) -> str:
        """比赛在下单链路上的位置：已触发 / 已预匹配 / 未匹配。

        面板上直接显示这个状态，就不用再翻日志猜「这场为什么没下单」。
        """
        if not game_id:
            return "-"
        fired = {
            "NBA": self.fired_nba,
            "WNBA": self.fired_wnba,
            "MLB": self.fired_mlb,
            "TENNIS": self.fired_tennis,
            "CS2": self.fired_cs2,
            "NFL": self.fired_nfl,
            "CFB": self.fired_cfb,
            "NHL": self.fired_nhl,
        }.get(str(sport).upper())
        if fired and str(game_id) in fired:
            return "已触发"
        if self.get_prematched_market(sport, game_id):
            return "已预匹配"
        if self.known_no_market(sport, game_id):
            # 最近一次搜索确认没有可匹配盘口（否定缓存内），到点会自动重试
            return "无盘口"
        return "未匹配"

    async def _fetch_and_process_tennis(self):
        try:
            tennis_matches = await self.nba.fetch_tennis_matches()
            # PM 实时流覆盖：校验通过后，用推送的逐盘局分替换轮询值
            tennis_matches, _ = self.sports_overlay.merge_rows("TENNIS", tennis_matches)
            self.cached_tennis = tennis_matches
            self._last_stream_rows["TENNIS"] = tennis_matches
            await self._update_dashboard_tennis(tennis_matches)
        except Exception:
            logger.exception("Tennis processing failed")

    async def _update_dashboard_tennis(self, tennis_matches):
        # 只读：把网球原始行（含逐盘局分 + 谁在发球）交给面板，暴露在 /api/tennis-state，
        # 供模型账本读取 —— 免得它自己每 15 秒重抓 1.3MB 的 ESPN 网球 scoreboard
        # 被限流（2026-09-30 实测：账本因此 10 小时一条没采到）。
        try:
            self.dashboard.update_tennis_matches(tennis_matches)
        except Exception as exc:  # noqa: BLE001 - 纯只读，不能影响主流程
            logger.debug("tennis-state 同步失败：%s", exc)
        for match in tennis_matches:
            p1 = match["player1"]
            p2 = match["player2"]
            ls1 = p1.get("linescores", [])
            ls2 = p2.get("linescores", [])
            sets_str = " ".join(f"{a}-{b}" for a, b in zip(ls1, ls2))
            item = {
                "sport": "Tennis",
                "id": match["match_id"],
                "away": p1["name"],
                "home": p2["name"],
                "away_score": sum(1 for i in range(len(ls1)) if ls1[i] > ls2[i]),
                "home_score": sum(1 for i in range(len(ls2)) if ls2[i] > ls1[i]),
                "status": match.get("status_detail", ""),
                "detail": f"Sets: {sets_str}",
                "state": match.get("status", ""),
                # 网球行原来漏了这个字段，面板的「盘口状态」列对网球永远是空的 ——
                # 会让预匹配看起来"只对 CS2 生效"（2026-09-14 实测确认）。
                "market_status": self._market_status("TENNIS", match["match_id"]),
            }
            self._touch_score_change(item)
            self.dashboard.update_tennis_game(item)
        # 处理放在这里（原来外部又循环了一遍 → 每场比赛每轮被评估两次，
        # 未成交的比赛还会把 TENNIS LOCK / 盘口搜索重复打一遍）
        for match in tennis_matches:
            self.tennis_tie_shadow.settle_completed_match(match)
            try:
                await self._observe_tennis_tie_shadow(match)
            except Exception as exc:  # noqa: BLE001 - 影子仓绝不影响主交易流程
                logger.debug("网球决胜盘平分影子仓记录失败：%s", exc)
            await self._process_tennis(match)

    async def _process_nba(self, game):
        if self._should_skip_fired(self.fired_nba, game.game_id):
            return
        signal = evaluate_lock(self.config, game)
        if signal is None:
            return

        logger.info(
            "NBA LOCK: %s leads %d-%d | Q%d %.0fs | diff=%d",
            signal.leader_team, signal.leader_score, signal.underdog_score,
            signal.quarter, signal.clock_seconds, signal.score_diff,
        )

        leader_name = (
            game.home_team_name if signal.leader_score == game.home_score else game.away_team_name
        )
        underdog_name = (
            game.away_team_name if signal.leader_score == game.home_score else game.home_team_name
        )
        market, market_reason, market_details = await self._evaluate_market(signal, leader_name, underdog_name, "NBA")
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, "NBA", game.game_id, self.fired_nba, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, "NBA", game.game_id, self.fired_nba,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate("NBA", entry_price)
        if gate:
            self._mark_fired(self.fired_nba, game.game_id, market_details)
            self.trader.log_signal_rejected(signal, gate, "NBA", market_details)
            return
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(signal, entry_price, "NBA", details=market_details)
            if pos:
                logger.info("NBA paper trade opened: %s YES @ %.3f", signal.leader_team, entry_price)
        self._mark_fired(self.fired_nba, game.game_id, market_details)
        await self._submit_live_order(signal, market, token_id, "NBA", book=book)

    async def _process_wnba(self, game):
        if self._should_skip_fired(self.fired_wnba, game.game_id):
            return
        signal = evaluate_lock(self.config, game)
        if signal is None:
            return

        logger.info(
            "WNBA LOCK: %s leads %d-%d | Q%d %.0fs | diff=%d",
            signal.leader_team, signal.leader_score, signal.underdog_score,
            signal.quarter, signal.clock_seconds, signal.score_diff,
        )

        leader_name = (
            game.home_team_name if signal.leader_score == game.home_score else game.away_team_name
        )
        underdog_name = (
            game.away_team_name if signal.leader_score == game.home_score else game.home_team_name
        )
        market, market_reason, market_details = await self._evaluate_market(
            signal, leader_name, underdog_name, "WNBA"
        )
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, "WNBA", game.game_id, self.fired_wnba, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, "WNBA", game.game_id, self.fired_wnba,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate("WNBA", entry_price)
        if gate:
            self._mark_fired(self.fired_wnba, game.game_id, market_details)
            self.trader.log_signal_rejected(signal, gate, "WNBA", market_details)
            return
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(signal, entry_price, "WNBA", details=market_details)
            if pos:
                logger.info("WNBA paper trade opened: %s YES @ %.3f", signal.leader_team, entry_price)
        self._mark_fired(self.fired_wnba, game.game_id, market_details)
        await self._submit_live_order(signal, market, token_id, "WNBA", book=book)

    async def _process_college_basketball(self, game, sport: str):
        """大学篮球（NCAAB 男篮 2 半场 / NCAAW 女篮 4 节）。

        与 WNBA 的区别只有两处：节次阈值按男女篮分别取，以及日志里的节次标签。
        市场匹配走「学校名」分支（同 CFB）。
        """
        fired = self.fired_ncaaw if sport == "NCAAW" else self.fired_ncaab
        if self._should_skip_fired(fired, game.game_id):
            return
        signal = evaluate_college_basketball_lock(self.config, game, sport)
        if signal is None:
            return

        logger.info(
            "%s LOCK: %s leads %d-%d | %s %.0fs | diff=%d",
            sport, signal.leader_team, signal.leader_score, signal.underdog_score,
            self._period_label(sport, signal.period), signal.clock_seconds, signal.score_diff,
        )

        leader_name = (
            game.home_team_name if signal.leader_score == game.home_score else game.away_team_name
        )
        underdog_name = (
            game.away_team_name if signal.leader_score == game.home_score else game.home_team_name
        )
        market, market_reason, market_details = await self._evaluate_market(
            signal, leader_name, underdog_name, sport
        )
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, sport, game.game_id, fired, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, sport, game.game_id, fired,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate(sport, entry_price)
        if gate:
            self._mark_fired(fired, game.game_id, market_details)
            self.trader.log_signal_rejected(signal, gate, sport, market_details)
            return
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(signal, entry_price, sport, details=market_details)
            if pos:
                logger.info(
                    "%s paper trade opened: %s YES @ %.3f", sport, signal.leader_team, entry_price
                )
        self._mark_fired(fired, game.game_id, market_details)
        await self._submit_live_order(signal, market, token_id, sport, book=book)

    async def _process_soccer(self, game):
        """足球：由胜率模型判定锁定，价格闸门按「模型概率 - 最小期望」算。

        与其它运动最大的不同在价格闸门 —— 足球不看固定的 max_entry_price 是否够低，
        而是看 **模型给的胜率减去市场价还剩多少期望**：
            模型 95% / 市场 0.90 → 期望 +5pp，下单
            模型 95% / 市场 0.96 → 期望 -1pp，跳过
        """
        if self._should_skip_fired(self.fired_soccer, game.game_id):
            return
        signal = evaluate_soccer_lock(self.config, game)
        if signal is None:
            return

        logger.info(
            "SOCCER LOCK[%s]: %s leads %d-%d @ %d' | 模型胜率 %.1f%%",
            signal.league, signal.leader_team, signal.leader_score, signal.underdog_score,
            int(signal.minute), signal.win_probability * 100,
        )

        market, market_reason, market_details = await self._evaluate_soccer_market(signal)
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, "SOCCER", game.game_id, self.fired_soccer, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, "SOCCER", game.game_id, self.fired_soccer,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = soccer_price_gate(self.config, signal, entry_price)
        if gate:
            # 只在「拒绝原因变化」时落一条日志/事件：足球会在价格不合适时反复重试
            # （比分随时变，下一 tick 可能就有价值），每 3 秒记一次会把日志刷爆。
            if self._soccer_gate_logged.get(game.game_id) != gate:
                self._soccer_gate_logged[game.game_id] = gate
                self.trader.log_signal_rejected(
                    signal, gate, "SOCCER",
                    {**market_details, "entry_price": entry_price,
                     "win_probability": signal.win_probability},
                )
                logger.info(
                    "SOCCER 跳过[%s]: %s 价格 %.3f vs 模型 %.1f%%（%s）",
                    signal.league, signal.leader_team, entry_price,
                    signal.win_probability * 100, gate,
                )
            if gate in ("PRICE_BELOW_LOCK_THRESHOLD", "PRICE_ABOVE_LIMIT"):
                self._mark_fired(self.fired_soccer, game.game_id, market_details)
            return

        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(signal, entry_price, "SOCCER", details=market_details)
            if pos:
                logger.info(
                    "SOCCER paper trade opened: %s YES @ %.3f（模型 %.1f%%，期望 +%.1fpp）",
                    signal.leader_name, entry_price, signal.win_probability * 100,
                    (signal.win_probability - entry_price) * 100,
                )
        self._mark_fired(self.fired_soccer, game.game_id, market_details)
        self._soccer_gate_logged.pop(game.game_id, None)
        await self._submit_live_order(signal, market, token_id, "SOCCER", book=book)

    async def _evaluate_soccer_market(self, signal):
        """足球专用的盘口匹配：找「<领先方> 赢」那个 Yes/No 二元盘。"""
        rules = self.config.sport_rules.get("SOCCER")
        if not rules or not rules.enabled:
            return None, "SPORT_DISABLED", {"sport": "SOCCER"}
        details = {
            "sport": "SOCCER",
            "league": signal.league,
            "search_term": signal.leader_name,
            "opponent_term": signal.underdog_name,
            "score": f"{signal.leader_score}-{signal.underdog_score}",
            "minute": signal.minute,
            "win_probability": signal.win_probability,
            "market_found": False,
        }
        prematched = self.get_prematched_market("SOCCER", signal.game_id)
        market = None
        if prematched:
            details["prematched"] = True
        # 否定缓存命中：最近确认过这场没有可匹配的盘口，不再重搜
        if self.known_no_market("SOCCER", signal.game_id):
            details["prematch_miss"] = True
            return None, "NO_MARKET", details
        # 映射缓存：token 与盘口题面是稳定的，没必要每 tick 重跑两次 Gamma 搜索
        cache_key = (signal.league_tag, signal.leader_name, signal.underdog_name)
        cached = self._soccer_market_cache.get(cache_key)
        ttl = float(getattr(self.config, "soccer_market_cache_seconds", 600) or 600)
        if cached and (time.time() - cached[1]) < ttl:
            market = cached[0]
            details["cached"] = True
        else:
            tag_id = getattr(self.config, "sport_market_tag_ids", {}).get("SOCCER")
            market = await self.pm.search_soccer_winner_market(
                signal.league_tag or "soccer",
                signal.leader_name,
                signal.underdog_name,
                tag_id,
            )
            if market:
                self._soccer_market_cache[cache_key] = (market, time.time())
        if market is None:
            return None, "NO_MARKET", details
        details.update({
            "market_found": True,
            "market_id": market.event_id,
            "market_question": market.question,
            "outcomes": market.outcomes,
            "token_id": market.token_ids[0] if market.token_ids else "",
            "best_bid": market.best_bid,
            "best_ask": market.best_ask,
            "max_entry_price": rules.max_entry_price,
        })
        if not details["token_id"]:
            return market, "NO_TOKEN", details
        # 与 _evaluate_market 同样的市场级去重（足球走的是独立匹配器，要单独接一次）
        decided_key = self._market_already_decided(details)
        if decided_key:
            details["duplicate_market"] = decided_key
            return market, "DUPLICATE_MARKET", details
        inflight_key = self._market_in_flight(details)
        if inflight_key:
            details["inflight_market"] = inflight_key
            return market, "MARKET_IN_FLIGHT", details
        self._reserve_market(details)
        return market, "MARKET_OK", details

    def _soccer_hold_probability(self, game) -> float | None:
        """当前状态下「领先方最终获胜」的模型概率（没有领先方时返回 None）。

        面板用它显示"离触发还差多少"；计算很轻（泊松卷积 ~10×10），
        每个 tick 对每场算一次没有问题。
        """
        home = int(game.home_score)
        away = int(game.away_score)
        if home == away:
            return None
        model = SoccerModel(
            home_lambda=float(getattr(game, "lambda_home", 1.573) or 1.573),
            away_lambda=float(getattr(game, "lambda_away", 1.293) or 1.293),
            max_goals=int(getattr(self.config, "soccer_max_goals", 10) or 10),
        )
        return model.leader_hold_probability(
            max(home, away), min(home, away), float(game.minute),
            leader_is_home=home > away,
        )

    @staticmethod
    def _period_label(sport: str, period: int) -> str:
        """节次显示：大学男篮是半场（H1/H2），其余是节（Q1/Q2…）。"""
        if str(sport).upper() == "NCAAB":
            return f"H{period}"
        return f"Q{period}"

    async def _process_clock_sport(self, game, sport: str):
        """按「节次 + 剩余时间」记分的运动（NFL / CFB / NHL）共用一条链路。"""
        fired = {
            "NFL": self.fired_nfl,
            "CFB": self.fired_cfb,
            "NHL": self.fired_nhl,
        }[sport]
        if self._should_skip_fired(fired, game.game_id):
            return
        evaluate = evaluate_hockey_lock if sport == "NHL" else evaluate_football_lock
        signal = evaluate(self.config, game)
        if signal is None:
            return

        logger.info(
            "%s LOCK: %s leads %d-%d | 第%d节 %.0fs | diff=%d (需要 %d)",
            sport, signal.leader_team, signal.leader_score, signal.underdog_score,
            getattr(signal, "quarter", getattr(signal, "period", 0)),
            signal.clock_seconds, signal.score_diff, signal.required_diff,
        )

        leader_name = (
            game.home_team_name if signal.leader_score == game.home_score else game.away_team_name
        )
        underdog_name = (
            game.away_team_name if signal.leader_score == game.home_score else game.home_team_name
        )
        market, market_reason, market_details = await self._evaluate_market(
            signal, leader_name, underdog_name, sport
        )
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, sport, game.game_id, fired, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, sport, game.game_id, fired,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate(sport, entry_price)
        if gate:
            self._mark_fired(fired, game.game_id, market_details)
            self.trader.log_signal_rejected(signal, gate, sport, market_details)
            return
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            position = self.trader.open_position(signal, entry_price, sport, details=market_details)
            if position:
                logger.info("%s paper trade opened: %s YES @ %.3f", sport, signal.leader_team, entry_price)
        self._mark_fired(fired, game.game_id, market_details)
        await self._submit_live_order(signal, market, token_id, sport, book=book)

    async def _process_mlb(self, game):
        if self._should_skip_fired(self.fired_mlb, game["game_id"]):
            return
        signal = evaluate_mlb_lock(self.config, game)
        if signal is None:
            return

        logger.info(
            "MLB LOCK: %s leads %d-%d | %s | diff=%d",
            signal.leader_team, signal.leader_score, signal.underdog_score,
            signal.status_detail, signal.score_diff,
        )

        leader_name = (
            game["home_team_name"] if signal.leader_score == game["home_score"] else game["away_team_name"]
        )
        underdog_name = (
            game["away_team_name"] if signal.leader_score == game["home_score"] else game["home_team_name"]
        )
        market, market_reason, market_details = await self._evaluate_market(signal, leader_name, underdog_name, "MLB")
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, "MLB", game["game_id"], self.fired_mlb, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, "MLB", game["game_id"], self.fired_mlb,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate("MLB", entry_price)
        if gate:
            self._mark_fired(self.fired_mlb, game["game_id"], market_details)
            self.trader.log_signal_rejected(signal, gate, "MLB", market_details)
            return
        ls = self._make_generic_signal(
            game_id=signal.game_id,
            leader=signal.leader_team,
            underdog=signal.underdog_team,
            leader_score=signal.leader_score,
            underdog_score=signal.underdog_score,
            period=signal.inning,
        )
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(ls, entry_price, "MLB", details=market_details)
            if pos:
                logger.info("MLB paper trade opened: %s YES @ %.3f", signal.leader_team, entry_price)
        self._mark_fired(self.fired_mlb, game["game_id"], market_details)
        await self._submit_live_order(signal, market, token_id, "MLB", book=book)

    async def _process_cs2(self, game):
        game_id = str(game.get("gameId", ""))
        if not game_id or self._should_skip_fired(self.fired_cs2, game_id):
            return
        signal = evaluate_cs2_lock(self.config, game)
        if signal is None:
            return

        logger.info(
            "CS2 LOCK: %s leads %s | maps %d-%d | rounds %d-%d | %s",
            signal.leader_team, signal.underdog_team,
            signal.maps_leader, signal.maps_underdog,
            signal.round_leader, signal.round_underdog,
            signal.period,
        )

        market, market_reason, market_details = await self._evaluate_market(
            signal,
            signal.leader_team,
            signal.underdog_team,
            "CS2",
            exclude_keywords=["map ", "round ", "pistol", "first kill", "handicap", "total rounds"],
        )
        if market_reason != "MARKET_OK":
            self._reject_market_signal(
                signal, market_reason, "CS2", game_id, self.fired_cs2, market_details
            )
            return

        token_id = market_details["token_id"]
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, "CS2", game_id, self.fired_cs2,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate("CS2", entry_price)
        if gate:
            self._mark_fired(self.fired_cs2, game_id, market_details)
            self.trader.log_signal_rejected(signal, gate, "CS2", market_details)
            return
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(signal, entry_price, "CS2", details=market_details)
            if pos:
                logger.info("CS2 paper trade opened: %s YES @ %.3f", signal.leader_team, entry_price)
        self._mark_fired(self.fired_cs2, game_id, market_details)
        await self._submit_live_order(signal, market, token_id, "CS2", book=book)

    def _schedule_cs2_tie_shadow(self, game: dict) -> None:
        """Observe a deciding-map tie without delaying the CS2 live-signal path."""
        tracker = self.cs2_tie_shadow
        if not tracker.enabled:
            return
        try:
            state = cs2_deciding_map_tie_state(
                game, max_round_score=tracker.max_round_score
            )
        except Exception as exc:  # noqa: BLE001 - observation must not block trading
            tracker.note_miss(f"parse: {type(exc).__name__}: {exc}")
            return
        if state is None:
            return
        shadow_id = tracker.shadow_id(state)
        if shadow_id in self._cs2_tie_shadow_inflight:
            return
        self._cs2_tie_shadow_inflight.add(shadow_id)
        task = asyncio.create_task(self._observe_cs2_tie_shadow(game, state))
        self._cs2_tie_shadow_tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._cs2_tie_shadow_tasks.discard(finished)
            self._cs2_tie_shadow_inflight.discard(shadow_id)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                tracker.note_miss(f"observer: {type(exc).__name__}: {exc}")

        task.add_done_callback(_done)

    async def _observe_cs2_tie_shadow(self, game: dict, state: dict) -> None:
        """Record the cheaper side at a new tied score in the deciding CS2 map."""
        tracker = self.cs2_tie_shadow
        if not tracker.enabled or not tracker.should_attempt(state):
            return

        game_id = str(state.get("game_id") or "")
        home_team = str(state.get("home_team") or "")
        away_team = str(state.get("away_team") or "")
        if not game_id or not home_team or not away_team:
            tracker.note_miss("missing game/team")
            return

        prematched = self.get_prematched_market("CS2", game_id)
        if prematched:
            outcomes = list(prematched.get("outcomes") or [])
            token_ids = list(prematched.get("token_ids") or [])
            question = str(prematched.get("question") or "")
        else:
            market = await self.pm.search_sports_markets(
                "cs2",
                away_team,
                home_team,
                None,
                getattr(self.config, "sport_market_tag_ids", {}).get("CS2"),
            )
            if market is None:
                tracker.note_miss("market_not_found")
                return
            outcomes = list(market.outcomes)
            token_ids = list(market.token_ids)
            question = str(market.question or "")
            self._prematch[self._prematch_key("CS2", game_id)] = {
                "found": True,
                "market_id": str(market.event_id),
                "question": question,
                "outcomes": outcomes,
                "token_ids": token_ids,
                "leader_index": self.pm.match_outcome_index(outcomes, home_team),
                "best_bid": market.best_bid,
                "best_ask": market.best_ask,
                "tick_size": float(getattr(market, "tick_size", 0.01) or 0.01),
                "matched_at": time.time(),
            }

        if len(outcomes) != 2 or len(token_ids) != 2:
            tracker.note_miss(
                f"market_parse: outcomes={len(outcomes)} tokens={len(token_ids)}"
            )
            return
        home_index = self.pm.match_outcome_index(outcomes, home_team)
        away_index = self.pm.match_outcome_index(outcomes, away_team)
        if (
            not (0 <= home_index < len(token_ids))
            or not (0 <= away_index < len(token_ids))
            or home_index == away_index
        ):
            tracker.note_miss(
                f"market_match: home={home_index} away={away_index}"
            )
            return

        async def book_view(token_id: str):
            book = await self._live_book(token_id)
            if book is None:
                return None
            try:
                summary = summarize_book(book)
            except Exception:
                return None
            if not summary:
                return None
            return summary

        sampled = await asyncio.gather(
            book_view(str(token_ids[home_index])),
            book_view(str(token_ids[away_index])),
            return_exceptions=True,
        )
        if any(isinstance(row, Exception) or not row for row in sampled):
            tracker.note_miss("book_unavailable")
            return
        players = [
            {"name": home_team, "token_id": str(token_ids[home_index]), **sampled[0]},
            {"name": away_team, "token_id": str(token_ids[away_index]), **sampled[1]},
        ]
        row = tracker.record_entry(state=state, question=question, players=players)
        if row:
            logger.info(
                "CS2 TIE SHADOW: %s | map %s %s | %s @ %.3f",
                question,
                state.get("deciding_map_number"),
                state.get("round_score"),
                row.get("selected"),
                float(row.get("selected_ask") or 0.0),
            )

    async def _observe_tennis_tie_shadow(self, match):
        """Record a virtual buy of the cheaper side at every new deciding-set tie.

        This path is deliberately outside ``_process_tennis`` and its one-per-match
        lock deduplication: 1-1, 2-2, ... 6-6 are separate observations.  It only
        reads the CLOB books; no live order is submitted.
        """
        tracker = self.tennis_tie_shadow
        if not tracker.enabled:
            return
        state = deciding_tie_state(match)
        if state is None or not tracker.should_attempt(state):
            return
        trigger_at = time.time()
        match_id = state["match_id"]
        player1 = state["player1"]
        player2 = state["player2"]
        if not match_id or not player1 or not player2:
            return
        event_id = self.tennis_tie_events.event_id(
            match_id, state["game_score"], trigger_at
        )
        score_record = self.score_change_state.get(match_id) or {}
        try:
            score_change_at = float(score_record.get("at") or 0.0) or None
        except (TypeError, ValueError):
            score_change_at = None
        trace_state = {
            **state,
            "event_id": event_id,
            "trigger_at": trigger_at,
            "score_change_at": score_change_at,
        }
        self.tennis_tie_events.record_trigger(
            event_id=event_id,
            match=match,
            state=state,
            score_change_at=score_change_at,
            trigger_at=trigger_at,
        )

        lookup_started_at = time.time()
        prematched = self.get_prematched_market("TENNIS", match_id)
        if prematched:
            outcomes = list(prematched.get("outcomes") or [])
            token_ids = list(prematched.get("token_ids") or [])
            question = str(prematched.get("question") or "")
            market_id = str(prematched.get("market_id") or "")
            market_source = "prematch"
        else:
            market = await self.pm.search_sports_markets(
                "tennis",
                player1,
                player2,
                None,
                getattr(self.config, "sport_market_tag_ids", {}).get("TENNIS"),
            )
            if market is None:
                self.tennis_tie_events.record_error(
                    event_id=event_id,
                    phase="market_lookup",
                    error="market_not_found",
                )
                return
            outcomes = list(market.outcomes)
            token_ids = list(market.token_ids)
            question = str(market.question or "")
            market_id = str(market.event_id)
            market_source = "search"
            self._prematch[self._prematch_key("TENNIS", match_id)] = {
                "found": True,
                "market_id": market_id,
                "question": question,
                "outcomes": outcomes,
                "token_ids": token_ids,
                "leader_index": self.pm.match_outcome_index(outcomes, player1),
                "best_bid": market.best_bid,
                "best_ask": market.best_ask,
                "tick_size": float(getattr(market, "tick_size", 0.01) or 0.01),
                "matched_at": time.time(),
            }
        market_ready_at = time.time()
        if len(outcomes) != 2 or len(token_ids) != 2:
            self.tennis_tie_events.record_error(
                event_id=event_id,
                phase="market_parse",
                error=f"outcomes={len(outcomes)} tokens={len(token_ids)}",
            )
            return
        self.tennis_tie_events.record_market(
            event_id=event_id,
            market_id=market_id,
            question=question,
            outcomes=outcomes,
            token_ids=token_ids,
            lookup_started_at=lookup_started_at,
            market_ready_at=market_ready_at,
            source=market_source,
        )

        index1 = self.pm.match_outcome_index(outcomes, player1)
        index2 = self.pm.match_outcome_index(outcomes, player2)
        if (
            not (0 <= index1 < len(token_ids))
            or not (0 <= index2 < len(token_ids))
            or index1 == index2
        ):
            self.tennis_tie_events.record_error(
                event_id=event_id,
                phase="market_match",
                error=f"player1_index={index1} player2_index={index2}",
            )
            return

        async def book_view(token_id: str):
            started_at = time.time()
            book = await self._live_book(token_id)
            if book is None:
                return None
            try:
                summary = summarize_book(book)
                return book, summary, started_at, time.time()
            except Exception:  # noqa: BLE001 - malformed shadow quote is ignored
                return None

        quote_started_at = time.time()
        sampled = await asyncio.gather(
            book_view(str(token_ids[index1])),
            book_view(str(token_ids[index2])),
            return_exceptions=True,
        )
        quote_finished_at = time.time()
        if any(
            isinstance(item, Exception)
            or not item
            or not item[1]
            for item in sampled
        ):
            errors = [
                repr(item) for item in sampled
                if isinstance(item, Exception) or not item or not item[1]
            ]
            self.tennis_tie_events.record_error(
                event_id=event_id,
                phase="book_fetch",
                error="; ".join(errors)[:500] or "empty_book",
            )
            return
        raw_books = [item[0] for item in sampled]
        views = [item[1] for item in sampled]
        book_timing = [
            {
                "token_id": str(token_ids[index1]),
                "started_at": sampled[0][2],
                "finished_at": sampled[0][3],
                "fetch_ms": round((sampled[0][3] - sampled[0][2]) * 1000, 3),
            },
            {
                "token_id": str(token_ids[index2]),
                "started_at": sampled[1][2],
                "finished_at": sampled[1][3],
                "fetch_ms": round((sampled[1][3] - sampled[1][2]) * 1000, 3),
            },
        ]
        players = [
            {
                "name": player1,
                "token_id": str(token_ids[index1]),
                **views[0],
            },
            {
                "name": player2,
                "token_id": str(token_ids[index2]),
                **views[1],
            },
        ]
        shadow_id = tracker.shadow_id(state)
        selected = min(players, key=lambda item: float(item["best_ask"]))
        other = max(players, key=lambda item: float(item["best_ask"]))
        self.tennis_tie_events.record_quote(
            event_id=event_id,
            quote_started_at=quote_started_at,
            quote_finished_at=quote_finished_at,
            views=[
                {"player": player1, **book_timing[0], **views[0]},
                {"player": player2, **book_timing[1], **views[1]},
            ],
            selected=selected,
            other=other,
            live_enabled=bool(getattr(self.config, "tennis_tie_live_enabled", False)),
        )
        trace_state.update({
            "market_id": market_id,
            "question": question,
            "selected_token_id": str(selected.get("token_id") or ""),
            "selected_ask": float(selected.get("best_ask") or 0.0),
            "other_token_id": str(other.get("token_id") or ""),
            "other_ask": float(other.get("best_ask") or 0.0),
            "quote_started_at": quote_started_at,
            "quote_finished_at": quote_finished_at,
        })
        live_order = None
        if bool(getattr(self.config, "tennis_tie_live_enabled", False)):
            try:
                live_order = await self.live_trader.submit_tie_low_buy(
                    game_id=match_id,
                    selected=str(selected.get("name") or ""),
                    opponent=str(other.get("name") or ""),
                    token_id=str(selected.get("token_id") or ""),
                    book=(
                        raw_books[0]
                        if str(selected.get("token_id")) == str(token_ids[index1])
                        else raw_books[1]
                    ),
                    order_usdc=float(
                        getattr(self.config, "tennis_tie_live_order_usdc", 1.10) or 1.10
                    ),
                    tie_state=trace_state,
                    shadow_id=shadow_id,
                )
            except Exception as exc:  # noqa: BLE001
                self.tennis_tie_events.record_error(
                    event_id=event_id,
                    phase="live_submit",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            if live_order is None:
                self.tennis_tie_events.record_error(
                    event_id=event_id,
                    phase="live_order",
                    error=self.live_trader.last_error or "unknown",
                )
                logger.warning(
                    "TENNIS TIE LIVE rejected: %s %s selected=%s reason=%s",
                    question,
                    state["game_score"],
                    selected.get("name"),
                    self.live_trader.last_error,
                )
                return
        row = tracker.record_entry(
            state=trace_state,
            question=question,
            players=players,
            live_order_id=getattr(live_order, "order_id", ""),
            live_status=getattr(live_order, "status", ""),
        )
        if row:
            logger.info(
                "TENNIS TIE %s: %s %s → %s @ %.3f",
                "LIVE" if live_order else "SHADOW",
                question,
                state["game_score"],
                row["selected"],
                row["selected_ask"],
            )

    async def _process_tennis(self, match):
        match_id = match.get("match_id", "")
        if self._should_skip_fired(self.fired_tennis, match_id):
            return
        signal = evaluate_tennis_lock(self.config, match)
        if signal is None:
            return

        # 日志里连每盘局分一起打出来：盘分是按"已分出胜负的盘"算的，
        # 只写 Sets 2-1 无法复盘（曾把"1-1 + 第 3 盘 1-0"读成 2-1）
        player1 = match.get("player1") or {}
        player2 = match.get("player2") or {}
        if signal.leader_name == player1.get("name"):
            leader_ls, underdog_ls = player1.get("linescores") or [], player2.get("linescores") or []
        else:
            leader_ls, underdog_ls = player2.get("linescores") or [], player1.get("linescores") or []
        sets_detail = " ".join(f"{int(a)}-{int(b)}" for a, b in zip(leader_ls, underdog_ls))
        # 破发影子字段（2026-09-16）：只记录不改判定。"领先 1 局"不等于"多一个破发"，
        # 先用几天数据看清"真破发"这一条值不值钱，再决定是否写进闸门。
        serve_text = (
            "未知" if signal.leader_serving is None
            else ("领先方" if signal.leader_serving else "对手")
        )
        break_text = (
            "未知" if signal.break_up is None
            else (f"是(+{signal.leader_min_breaks})" if signal.break_up else "否")
        )
        logger.info(
            "TENNIS LOCK: %s leads %s | Sets %d-%d | Current %d-%d (%s) | 局分 %s"
            " | 发球=%s 真破发=%s",
            signal.leader_name, signal.underdog_name,
            signal.sets_leader, signal.sets_underdog,
            signal.current_set_leader, signal.current_set_underdog,
            signal.status_detail, sets_detail,
            serve_text, break_text,
        )

        market, market_reason, market_details = await self._evaluate_market(signal, signal.leader_name, signal.underdog_name, "TENNIS")
        # ---- 2026-09-19 第 2 步：网球胜率模型（只记录，不参与判定）----
        # 现在的启发式只知道"领先一盘 + 领先一局"，看不见"谁在发球"值多少钱。
        # 模型（分→局→盘→场的解析递推）能算出真实概率，先**只落盘**观察
        # "模型概率 vs 市场价 vs 实际结果"，跑一两周再决定要不要写进闸门。
        model_win_prob = None
        games_played = int(signal.current_set_leader) + int(signal.current_set_underdog)
        first_server = tennis_model.first_server_is_leader(
            signal.leader_serving, games_played
        )
        if first_server is not None:
            try:
                model_win_prob = round(tennis_model.win_probability(
                    leader_sets=signal.sets_leader,
                    underdog_sets=signal.sets_underdog,
                    current_games_leader=signal.current_set_leader,
                    current_games_underdog=signal.current_set_underdog,
                    leader_serves_first_in_set=first_server,
                ), 4)
            except Exception as exc:  # noqa: BLE001 - 模型只是记录，不能影响下单
                logger.debug("tennis model failed: %s", exc)
        # 让影子字段跟着信号事件一起落盘（signal_events.jsonl 的 details 里），
        # 这样被闸门拒掉的和真正开仓的都能按"真破发"分组复盘。
        market_details = {
            **market_details,
            "leader_serving": signal.leader_serving,
            "leader_min_breaks": signal.leader_min_breaks,
            "break_up": signal.break_up,
            "current_set_games": f"{signal.current_set_leader}-{signal.current_set_underdog}",
            # ---- 2026-09-19 补齐状态字段（网球校准分析发现的最大缺口）----
            # 之前只记了"当前盘局分"，**盘分和逐盘局分全都没落盘**，导致复盘时
            # 根本无法还原比赛状态（校准样本里 114 条只有 48 条带状态）。
            # 这几个字段 ESPN 早就给了，只是没写进来，补上不动任何判定。
            "leader_sets": signal.sets_leader,
            "underdog_sets": signal.sets_underdog,
            # 逐盘局分，例如 "6-3 1-2"（进行中的那盘也在里面）
            "set_lines": sets_detail,
            # 当前是第几盘（1 起）：进行中的盘也算，用于还原状态
            "current_set_index": len(leader_ls),
            "status_detail": signal.status_detail,
            # 数据源那一刻的时间戳，用于事后判断状态是否过期
            "feed_updated_at": match.get("updated_at"),
            # 网球胜率模型的输出（只记录）：概率 + 用的参数，便于事后复现
            "model_win_prob": model_win_prob,
            "model_serve_point_win": tennis_model.default_serve_point_win("ATP"),
            "model_best_of": 3,
        }
        if market_reason != "MARKET_OK":
            # 运动名统一用大写，否则信号日志里会同时出现 Tennis / TENNIS 两组
            self._reject_market_signal(
                signal, market_reason, "TENNIS", match_id, self.fired_tennis, market_details
            )
            return

        token_id = market_details.get("token_id", "")
        entry_price, book = await self._shared_entry_price(token_id, market.best_ask)
        if entry_price is None:
            # 盘口读不到 / 没有卖单：跳过本轮并等下一 tick 重试，
            # 绝不能拿预匹配快照价当成交价去过闸门（见 _shared_entry_price 注释）
            self._skip_no_price(
                signal, "TENNIS", match_id, self.fired_tennis,
                {**market_details, "book_available": book is not None},
            )
            return
        gate = self._entry_price_gate("TENNIS", entry_price)
        if gate:
            self._mark_fired(self.fired_tennis, match_id, market_details)
            self.trader.log_signal_rejected(signal, gate, "TENNIS", market_details)
            return
        ls = self._make_generic_signal(
            game_id=match_id,
            leader=signal.leader_name,
            underdog=signal.underdog_name,
            leader_score=signal.sets_leader,
            underdog_score=signal.sets_underdog,
            period=match.get("period", 0),
        )
        if self.config.signal_drill_mode:
            logger.info("DRILL: 跳过虚拟盘开仓，避免污染统计")
        else:
            pos = self.trader.open_position(ls, entry_price, "TENNIS", details=market_details)
            if pos:
                logger.info("Tennis paper trade opened: %s YES @ %.3f", signal.leader_name, entry_price)
        self._mark_fired(self.fired_tennis, match_id, market_details)
        await self._submit_live_order(signal, market, token_id, "TENNIS", book=book)

    async def _live_book(self, token_id: str):
        """取一次 CLOB 实时盘口，供虚拟盘与实盘共用，避免两边价格源不一致。"""
        trader = self.live_trader
        client = getattr(trader, "client", None) if trader else None
        if client is None or not getattr(trader, "client_ready", False):
            return None
        try:
            return await asyncio.to_thread(client.get_order_book, token_id=token_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CLOB 客户端读盘口失败，回退公共 REST：%s（token=%s…）",
                exc, str(token_id)[:12],
            )
            return await self._live_book_rest(token_id)

    async def _live_book_rest(self, token_id: str):
        """公共 REST 兜底读盘口。

        2026-09-15 实测：py-clob 客户端对部分 token（尤其网球）会抛
        `Request failed`，同一个 token 用公共 REST 却能正常返回 200。
        原来直接回退到 Gamma 快照价，而那份快照可能是几十分钟前的、
        甚至是另一个球员的价格，导致价格闸门按垃圾数据拒绝信号。

        返回值是**原始 JSON dict**（`{"bids":[{"price":"0.01","size":"..."}], ...}`），
        与 py-clob 的 Book 对象形状不同 —— 下游 `_book_best_ask()` 必须两种都认，
        否则这条兜底路径等于白走（2026-09-16 修的正是这个）。
        """
        import aiohttp

        url = f"{self.config.polymarket_clob_url}/book"
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                async with session.get(
                    url,
                    params={"token_id": token_id},
                    proxy=getattr(self.config, "proxy_url", None),
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "CLOB REST 读盘口 HTTP %d（token=%s…）", resp.status, str(token_id)[:12]
                        )
                        return None
                    book = await resp.json()
                    logger.debug(
                        "CLOB 公共 REST 兜底成功：token=%s… bids=%s asks=%s",
                        str(token_id)[:12],
                        len(book.get("bids") or []) if isinstance(book, dict) else "?",
                        len(book.get("asks") or []) if isinstance(book, dict) else "?",
                    )
                    return book
        except Exception as exc:  # noqa: BLE001
            logger.warning("CLOB REST 读盘口失败：%s（token=%s…）", exc, str(token_id)[:12])
            return None

    @staticmethod
    def _book_best_ask(book) -> float | None:
        """盘口最优卖价；**同时兼容** py-clob 的 Book 对象与公共 REST 的原始 JSON。

        2026-09-16 的教训：`_live_book_rest()` 兜底成功时返回的是 `resp.json()`（dict），
        而这里原来只读对象属性（`book.asks[i].price`），字典一律解析成"空卖盘" →
        调用方看到 `book_ask is None` 但 `book is not None` → 误判成 `NO_ASK`
        「盘口读到了但无卖单」。实测 34 条 NO_ASK 里 30 条是这么来的
        （前 6 秒必有「客户端读盘口失败，回退公共 REST」），等于把兜底路径整个废掉。

        解析统一走 `position_paths.summarize_book()` —— 和盘口采集器同一份实现，
        两条路径对"什么叫卖一"不会再出现分歧。
        """
        if book is None:
            return None
        try:
            snapshot = summarize_book(book)
            return snapshot["best_ask"] if snapshot else None
        except Exception as exc:  # noqa: BLE001 - 解析失败按"买不到"处理，绝不能拿垃圾价过闸门
            # 但要留痕：2026-09-16 那个 bug 就是"解析失败 = 静默返回 None"造成的，
            # 同样的沉默不能再有一次（采集器那边已改成 parse_failures 计数）。
            logger.warning("盘口解析异常（%s）：%s", type(book).__name__, exc)
            return None

    async def _shared_entry_price(self, token_id: str, fallback: float) -> tuple[float, object]:
        """返回 (入场价, 盘口)。

        入场价 `None` = **现在买不到**，调用方应当跳过并等下一 tick 重试，绝不能拿它当价格。

        2026-09-15 的教训：原来读不到盘口就回退 `market.best_ask`（预匹配快照价），
        而那份快照可能是几十分钟前的、甚至是**另一个球员**的价格 ——
        实测连续 5 个网球信号都因此拿着 0.04/0.12 这种垃圾价格去过闸门，
        把本来该成交的机会全部拒掉（用户看到的现象："锁定的球员怎么是高赔率"）。

        只有一种情况保留快照回退：**没有实盘客户端**（纯模拟盘跑法），
        那时本来就拿不到实时盘口，用快照价是唯一选择。
        """
        has_client = self._live_clob_client() is not None
        book = await self._live_book(token_id)
        if book is not None:
            book_ask = self._book_best_ask(book)
            if book_ask is not None:
                return book_ask, book
            # 盘口读到了但没有卖单 —— 真的没人卖，挂单也成交不了
            return None, book
        if not has_client:
            return fallback, None
        return None, None

    async def _submit_live_order(self, signal, market, token_id: str, sport: str, book=None):
        if not token_id:
            reason = "MISSING_TOKEN_ID"
            self.live_trader.last_error = reason
            self.live_trader.log_rejection(signal, token_id, sport, reason)
            logger.warning(
                "LIVE ORDER REJECTED: sport=%s game=%s reason=%s",
                sport, signal.game_id, reason,
            )
            return
        try:
            logger.info(
                "LIVE SUBMIT START: sport=%s game=%s token_id=%s price=%s",
                sport, signal.game_id, token_id, getattr(market, "best_ask", None),
            )
            order = await self.live_trader.submit_limit_buy(
                signal, market, token_id, sport, book=book
            )
            if order:
                logger.info("LIVE ORDER SUBMITTED: %s %s order_id=%s", sport, token_id, order.order_id)
            elif self.live_trader:
                reason = self.live_trader.last_error or self.live_trader.last_reject_reason or "UNKNOWN"
                self.live_trader.log_rejection(signal, token_id, sport, reason)
                logger.warning("LIVE ORDER REJECTED: sport=%s game=%s reason=%s", sport, signal.game_id, reason)
        except Exception as exc:
            logger.exception("LIVE ORDER FAILED: sport=%s game=%s", sport, signal.game_id)
            self.live_trader.last_error = str(exc)
            self.live_trader.log_rejection(signal, token_id, sport, f"EXCEPTION: {exc}")

    async def _evaluate_market(
        self,
        signal,
        search_term: str,
        opponent_term: str,
        sport: str,
        exclude_keywords: list | None = None,
    ):
        rules = self.config.sport_rules.get(sport)
        if not rules or not rules.enabled:
            return None, "SPORT_DISABLED", {"sport": sport}
        details = {
            "search_term": search_term,
            "opponent_term": opponent_term,
            "sport_tag": sport.lower(),
            "sport": sport,
            "market_found": False,
            "score": f"{getattr(signal, 'leader_score', '-')}-{getattr(signal, 'underdog_score', '-')}",
            "period": getattr(signal, "quarter", getattr(signal, "inning", None)),
            "clock_seconds": getattr(signal, "clock_seconds", None),
            "required_diff": getattr(signal, "required_diff", None),
            "confidence": getattr(signal, "confidence", None),
        }

        game_id = getattr(signal, "game_id", "")
        # 否定缓存命中：最近一次已确认"这场没有可匹配的盘口"。
        # 直接返回，不再打 Gamma —— 否则每 tick 都会为同一场重搜一次。
        if self.known_no_market(sport, game_id):
            details["prematch_miss"] = True
            return None, "NO_MARKET", details
        # 优先用预匹配缓存，命中就不必再走 1.3 秒以上的 Gamma 搜索
        prematched = self.get_prematched_market(sport, game_id)
        if prematched:
            market = MarketPrice(
                event_id=prematched["market_id"],
                question=prematched["question"],
                token_ids=list(prematched["token_ids"]),
                outcomes=list(prematched["outcomes"]),
                best_bid=float(prematched.get("best_bid") or 0.0),
                best_ask=float(prematched.get("best_ask") or 0.0),
                tick_size=float(prematched.get("tick_size") or 0.01),
            )
            details["prematched"] = True
        else:
            tag_id = getattr(self.config, "sport_market_tag_ids", {}).get(sport.upper())
            market = await self.pm.search_sports_markets(
                sport.lower(), search_term, opponent_term, exclude_keywords, tag_id
            )
            details["prematched"] = False

        if market is None:
            return None, "NO_MARKET", details
        details["market_found"] = True

        # 统一走同一个匹配器：它认得「亚洲姓名顺序相反」（Ma YeXin ↔ Yexin Ma），
        # 并且在有歧义时返回 -1（见 PolymarketFeed.match_outcome_index）。
        leader_index = self.pm.match_outcome_index(market.outcomes, search_term)
        token_id = market.token_ids[leader_index] if 0 <= leader_index < len(market.token_ids) else ""
        # 缓存里的快照价是给「预匹配时那个球员」的；如果这次信号来自另一个球员，
        # 必须翻面，否则就是把对手的价格当成自己的（两人盘满足 ask' = 1 - bid）。
        # cached_index < 0 表示预匹配时也没认出来是谁，那就**不翻面**（宁可保持原样，
        # 也不要拿一个猜出来的方向去定价）。
        if prematched and leader_index >= 0 and len(market.outcomes) == 2:
            cached_index = int(prematched.get("leader_index", -1))
            if cached_index >= 0 and leader_index != cached_index:
                market = MarketPrice(
                    event_id=market.event_id,
                    question=market.question,
                    token_ids=list(market.token_ids),
                    outcomes=list(market.outcomes),
                    best_bid=round(1 - market.best_ask, 3),
                    best_ask=round(1 - market.best_bid, 3),
                    tick_size=market.tick_size,
                )
                details["price_side_flipped"] = True
        details.update({
            "market_id": market.event_id,
            "market_question": market.question,
            "outcomes": market.outcomes,
            "leader_index": leader_index,
            "token_id": token_id,
            "best_bid": market.best_bid,
            "best_ask": market.best_ask,
            # 该盘口的最小报价单位（Gamma）。模拟盘按「卖一 + 1 tick」记账要用它，
            # 闸门与拒单日志也一并带上，便于事后核对是不是 0.001 tick 的盘。
            "tick_size": float(getattr(market, "tick_size", 0.01) or 0.01),
            "max_entry_price": rules.max_entry_price,
            "min_expected_profit": rules.min_expected_profit,
            "theoretical_profit": 1.0 - market.best_ask,
        })
        if not token_id:
            return market, "NO_TOKEN", details
        # 同一场比赛的第二个 game_id：市场已经判过了（开过仓 / 已被闸门拒过）→ 直接放弃。
        # 放在这里而不是各 _process_* 里，是为了让 11 个运动一次性都拿到这个保护。
        decided_key = self._market_already_decided(details)
        if decided_key:
            details["duplicate_market"] = decided_key
            return market, "DUPLICATE_MARKET", details
        inflight_key = self._market_in_flight(details)
        if inflight_key:
            details["inflight_market"] = inflight_key
            return market, "MARKET_IN_FLIGHT", details
        self._reserve_market(details)
        # 价格闸门不在这里做：market.best_ask 来自 Gamma 快照，可能已经过期，
        # 用它会误杀本来可以成交的信号。真正的闸门在拿到 CLOB 实时盘口之后
        # （_entry_price_gate），实盘执行器还会再按同一份盘口复核一次。
        return market, "MARKET_OK", details

    def _entry_price_gate(self, sport: str, price: float) -> str | None:
        """按实时盘口价做入场价格风控，返回拒绝原因或 None。"""
        rules = self.config.sport_rules.get(sport)
        if not rules or price <= 0:
            return None
        # 市场一致性闸门：与实盘同一口径（演练模式跳过，方便跑通链路）
        min_entry = max(0.0, float(getattr(rules, "live_min_entry_price", 0.0) or 0.0))
        if min_entry > 0 and not getattr(self.config, "signal_drill_mode", False) and price < min_entry:
            return "PRICE_BELOW_LOCK_THRESHOLD"
        tolerance = max(0.0, float(getattr(rules, "live_entry_price_tolerance", 0.0) or 0.0))
        max_price = float(rules.max_entry_price) + tolerance
        min_profit = max(0.0, float(rules.min_expected_profit) - tolerance)
        if price > max_price:
            return "PRICE_ABOVE_LIMIT"
        if (1.0 - price) < min_profit:
            return "EXPECTED_PROFIT_TOO_LOW"
        # 入场价区间闸门（2026-09-23）：与实盘同一口径 —— 只做两个账本都为正的区间。
        if bool(getattr(self.config, "entry_band_enabled", False)) and not getattr(
            self.config, "signal_drill_mode", False
        ):
            band_min = float(getattr(self.config, "entry_band_min", 0.0) or 0.0)
            band_max = float(getattr(self.config, "entry_band_max", 1.0) or 1.0)
            if band_max > band_min and (price < band_min or price >= band_max):
                return "ENTRY_BAND"
        return None

    async def _evaluate_probability_market(self, signal, search_term: str, opponent_term: str, sport: str):
        details = {
            "search_term": search_term,
            "opponent_term": opponent_term,
            "sport": sport,
            "market_found": False,
            "score": f"{signal.leader_score}-{signal.underdog_score}",
            "period": signal.period,
            "rationale": signal.rationale,
            "estimated_probability": signal.estimated_probability,
        }
        market = await self.pm.search_sports_markets(sport.lower(), search_term, opponent_term)
        if market is None:
            return None, "NO_MARKET", details

        leader_index = self.pm.match_outcome_index(market.outcomes, search_term)
        token_id = market.token_ids[leader_index] if 0 <= leader_index < len(market.token_ids) else ""
        details.update({
            "market_found": True,
            "market_id": market.event_id,
            "market_question": market.question,
            "outcomes": market.outcomes,
            "leader_index": leader_index,
            "token_id": token_id,
            "best_bid": market.best_bid,
            "best_ask": market.best_ask,
            "max_entry_price": self.config.probability_max_entry_price,
            "min_edge": self.config.probability_min_edge,
            "probability_edge": signal.estimated_probability - market.best_ask,
        })
        if not token_id:
            return market, "NO_TOKEN", details
        # 概率账本用**独立**的去重表：它和锁定策略各自记账、各自限额，
        # 一边开过不该把另一边也堵死。但同一策略内部同样要防「两个 game_id 同一市场」。
        decided_key = self._market_already_decided(details, probability=True)
        if decided_key:
            details["duplicate_market"] = decided_key
            return market, "DUPLICATE_MARKET", details
        if market.best_ask > self.config.probability_max_entry_price:
            return market, "PRICE_ABOVE_LIMIT", details
        if signal.estimated_probability - market.best_ask < self.config.probability_min_edge:
            return market, "PROBABILITY_EDGE_TOO_LOW", details
        return market, "MARKET_OK", details

    async def _process_probability_candidates(self, nba_games, mlb_games, tennis_matches):
        if not self.config.probability_enabled:
            return

        candidates: list[tuple[object, str]] = []
        for game in nba_games:
            signal = evaluate_nba_probability(self.config, game)
            if signal:
                candidates.append((signal, "NBA"))
        for game in mlb_games:
            signal = evaluate_mlb_probability(self.config, game)
            if signal:
                candidates.append((signal, "MLB"))
        for match in tennis_matches:
            signal = evaluate_tennis_probability(self.config, match)
            if signal:
                candidates.append((signal, "TENNIS"))

        for probability_signal, sport in candidates:
            signature = (
                f"{probability_signal.leader_score}-{probability_signal.underdog_score}|"
                f"{probability_signal.rationale}"
            )
            processed_key = f"{sport}:{probability_signal.game_id}:{signature}"
            if processed_key in self.probability_processed:
                continue
            self.probability_processed.add(processed_key)
            self._save_probability_processed()

            market, reason, details = await self._evaluate_probability_market(
                probability_signal,
                probability_signal.leader_name,
                probability_signal.underdog_name,
                sport,
            )
            self.probability_trader.log_event(probability_signal, reason, sport, details)
            if reason != "MARKET_OK":
                continue

            if any(p.game_id == probability_signal.game_id for p in self.probability_trader.open_positions):
                continue

            position = self.probability_trader.open_position(
                probability_signal,
                market.best_ask,
                sport,
                details=details,
            )
            if position:
                # 概率账本也按市场记账：同一个市场换个 game_id 回来时不再重复建仓
                # （实测概率账本里有 1 个 token 被建过两次）
                self._mark_market_decided(details, probability=True)
                logger.info(
                    "PROBABILITY PAPER OPEN: %s %s @ %.3f | p=%.3f",
                    sport,
                    probability_signal.leader_name,
                    market.best_ask,
                    probability_signal.estimated_probability,
                )

    def _load_probability_processed(self) -> set[str]:
        if not self._probability_processed_path.exists():
            return set()
        try:
            data = json.loads(self._probability_processed_path.read_text(encoding="utf-8"))
            return set(data.get("keys", [])) if isinstance(data, dict) else set()
        except Exception as exc:
            logger.warning("Failed to load probability processed state: %s", exc)
            return set()

    def _save_probability_processed(self):
        try:
            self._probability_processed_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "keys": sorted(self.probability_processed)[-5000:],
                "saved_at": time.time(),
            }
            self._probability_processed_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save probability processed state: %s", exc)

    async def _get_polymarket_market(self, search_term: str, sport: str):
        rules = self.config.sport_rules.get(sport)
        if not rules or not rules.enabled:
            return None
        market = await self.pm.search_sports_markets(sport.lower(), search_term)
        if not market:
            return None

        entry_price = market.best_ask
        if entry_price > rules.max_entry_price:
            logger.info("Price too high (%.3f > %.3f) for %s", entry_price, rules.max_entry_price, search_term)
            return None

        if (1.0 - entry_price) < rules.min_expected_profit:
            logger.info("Expected profit too low (%.3f) for %s", 1.0 - entry_price, search_term)
            return None

        return market

    def _make_generic_signal(self, game_id, leader, underdog, leader_score, underdog_score, period):
        return LockSignal(
            game_id=game_id,
            leader_team=leader,
            underdog_team=underdog,
            leader_score=leader_score,
            underdog_score=underdog_score,
            quarter=period,
            clock_seconds=0,
            score_diff=abs(leader_score - underdog_score),
            required_diff=1,
            confidence=1.0,
        )

    async def _update_dashboard_games(
        self, nba_games, mlb_games, tennis_matches, wnba_games=None, cs2_games=None,
        nfl_games=None, cfb_games=None, nhl_games=None, ncaab_games=None, ncaaw_games=None,
        soccer_games=None,
    ):
        live = []
        for sport, games in (
            ("NBA", nba_games),
            ("WNBA", wnba_games or []),
            ("NCAAB", ncaab_games or []),
            ("NCAAW", ncaaw_games or []),
            ("NFL", nfl_games or []),
            ("CFB", cfb_games or []),
            ("NHL", nhl_games or []),
        ):
            for game in games:
                live.append({
                    "sport": sport,
                    "id": game.game_id,
                    "away": game.away_team,
                    "home": game.home_team,
                    "away_score": game.away_score,
                    "home_score": game.home_score,
                    "status": (
                        f"{self._period_label(sport, game.quarter)} "
                        f"{int(game.clock_seconds//60)}:{int(game.clock_seconds%60):02d}"
                    ),
                    "state": game.status,
                    "updated_at": game.updated_at,
                    "market_status": self._market_status(sport, game.game_id),
                })
                self._touch_score_change(live[-1])
        # 足球单独一段：时钟是「已进行分钟」，显示成 "67'" 而不是倒计时
        for game in soccer_games or []:
            minute = int(float(game.minute))
            half = ""
            if game.status == "in":
                # period=1 上半场 / =2 下半场；拿不到就用分钟兜底
                if int(getattr(game, "period", 0) or 0) == 1 or minute <= 45:
                    half = "上半场"
                else:
                    half = "下半场"
            probability = self._soccer_hold_probability(game)
            threshold = float(getattr(self.config, "soccer_min_win_probability", 0.90) or 0.90)
            live.append({
                "sport": "SOCCER",
                "id": game.game_id,
                "away": game.away_team,
                "home": game.home_team,
                "away_score": game.away_score,
                "home_score": game.home_score,
                "status": f"{game.league_name} {minute}'" + (f" {half}" if half else ""),
                # 详情列放模型胜率：一眼看出离触发阈值还差多少，
                # 不用去翻代码猜"这场为什么没信号"。
                "detail": (
                    f"模型 {probability * 100:.1f}%（需 ≥{threshold * 100:.0f}%）"
                    if probability is not None else "暂无领先方"
                ),
                "state": game.status,
                "updated_at": game.updated_at,
                "market_status": self._market_status("SOCCER", game.game_id),
            })
            self._touch_score_change(live[-1])
        for game in cs2_games or []:
            live.append({
                "sport": "CS2",
                "id": str(game.get("gameId", "")),
                "away": game.get("awayTeam") or "?",
                "home": game.get("homeTeam") or "?",
                "away_score": 0,
                "home_score": 0,
                "status": f"{game.get('period', '')} {game.get('score', '')}",
                "detail": f"{game.get('leagueAbbreviation', '')} {game.get('score', '')}".strip(),
                "state": "in" if game.get("live") and not game.get("ended") else ("post" if game.get("ended") else "pre"),
                "updated_at": game.get("received_at"),
                "market_status": self._market_status("CS2", str(game.get("gameId", ""))),
            })
            self._touch_score_change(live[-1])
        for game in mlb_games:
            live.append({
                "sport": "MLB",
                "id": game["game_id"],
                "away": game["away_team"],
                "home": game["home_team"],
                "away_score": game["away_score"],
                "home_score": game["home_score"],
                "status": game["status_detail"],
                "state": game["status"],
                "updated_at": game.get("updated_at"),
                "market_status": self._market_status("MLB", game["game_id"]),
            })
            self._touch_score_change(live[-1])
        for match in tennis_matches:
            p1 = match["player1"]
            p2 = match["player2"]
            ls1 = p1.get("linescores", [])
            ls2 = p2.get("linescores", [])
            sets_str = " ".join(f"{a}-{b}" for a, b in zip(ls1, ls2))
            live.append({
                "sport": "Tennis",
                "id": match["match_id"],
                "away": p1["name"],
                "home": p2["name"],
                "away_score": sum(1 for i in range(len(ls1)) if ls1[i] > ls2[i]),
                "home_score": sum(1 for i in range(len(ls2)) if ls2[i] > ls1[i]),
                "status": match.get("status_detail", ""),
                "detail": f"Sets: {sets_str}",
                "state": match.get("status", ""),
                "updated_at": match.get("updated_at"),
                "market_status": self._market_status("TENNIS", match["match_id"]),
            })
            self._touch_score_change(live[-1])
        self.dashboard.update_live_games(live)
        feed_health = dict(self.nba.health())
        feed_health.update(self.sports_feed.health())
        overlay_stats = self.sports_overlay.stats()
        for sport, entry in feed_health.items():
            suffix = self.sports_overlay.health_suffix(sport)
            if suffix:
                entry["source"] = f"{entry.get('source') or '-'}{suffix}"
            stat = overlay_stats.get(sport.upper())
            if stat is not None:
                entry["overlay"] = {
                    "tracked": stat.tracked,
                    "validated": stat.validated,
                    "applied": stat.applied_last,
                    "last_message_at": stat.last_message_at,
                    "last_apply_at": stat.last_apply_at,
                    "sample": stat.sample,
                    "note": stat.note,
                }
        self.dashboard.update_feed_health(feed_health)

    async def _check_settlements(
        self, nba_games, mlb_games, tennis_matches, wnba_games=None, cs2_games=None,
        nfl_games=None, cfb_games=None, nhl_games=None, ncaab_games=None, ncaaw_games=None,
        soccer_games=None,
    ):
        open_game_ids = {p.game_id for p in self.trader.open_positions}
        open_game_ids.update(
            p.game_id for p in self.live_trader.positions.values() if p.status == "OPEN"
        )
        open_game_ids.update(
            p.game_id for p in self.probability_trader.open_positions
        )
        if not open_game_ids:
            return
        tennis_open_ids = {
            p.game_id
            for p in [*self.trader.open_positions, *self.probability_trader.open_positions]
            if p.sport == "TENNIS"
        }
        tennis_open_ids.update(
            p.game_id
            for p in self.live_trader.positions.values()
            if p.status == "OPEN" and getattr(p, "sport", "") == "TENNIS"
        )
        if tennis_open_ids:
            tennis_matches = tennis_matches + await self.nba.fetch_tennis_matches(
                include_completed=True,
                game_ids=tennis_open_ids,
            )
        # 遗留 id 兜底：StatsAPI 与 ESPN 的 game_id 命名空间不同，
        # 用 ESPN 回退源开的仓（id 形如 401816900）在切回 StatsAPI 后永远匹配不上，
        # 会一直卡在未结算。这里只在真的存在"取不到对应比赛"的未结算仓位时才多拉一次。
        mlb_open_ids = {
            p.game_id
            for p in [*self.trader.open_positions, *self.probability_trader.open_positions]
            if p.sport == "MLB"
        }
        mlb_open_ids.update(
            p.game_id
            for p in self.live_trader.positions.values()
            if p.status == "OPEN" and getattr(p, "sport", "") == "MLB"
        )
        fetched_mlb_ids = {
            (game.get("game_id") if isinstance(game, dict) else getattr(game, "game_id", ""))
            for game in mlb_games
        }
        if mlb_open_ids - fetched_mlb_ids:
            extra_games = await self.nba.fetch_mlb_espn_games(self.nba._mlb_dates())
            merged = {g.get("game_id"): g for g in mlb_games if isinstance(g, dict)}
            for game in extra_games:
                merged.setdefault(game.get("game_id"), game)
            if len(merged) > len(mlb_games):
                logger.info("为遗留 game_id 补拉 ESPN 记分板：%d → %d 场", len(mlb_games), len(merged))
            mlb_games = list(merged.values())
        for game in [
            *nba_games, *(wnba_games or []), *(nfl_games or []), *(cfb_games or []),
            *(nhl_games or []), *(ncaab_games or []), *(ncaaw_games or []),
            *mlb_games, *tennis_matches, *(soccer_games or []),
        ]:
            game_id = game.game_id if hasattr(game, "game_id") else game.get("game_id", "")
            if game_id not in open_game_ids:
                continue
            status = game.status if hasattr(game, "status") else game.get("status", "")
            if status == "in":
                continue
            if game_id in self.completed_games:
                continue
            self.completed_games.add(game_id)
            if hasattr(game, "game_id"):
                home_team, away_team = game.home_team, game.away_team
                home_score, away_score = game.home_score, game.away_score
            else:
                home_team = game.get("home_team", game.get("home", {}).get("name", ""))
                away_team = game.get("away_team", game.get("away", {}).get("name", ""))
                home_score = game.get("home_score", 0)
                away_score = game.get("away_score", 0)
            self.trader.settle_game(
                game_id=game_id,
                home_team=home_team,
                away_team=away_team,
                home_score=home_score,
                away_score=away_score,
                allow_draw=hasattr(game, "league_tag"),
            )
            self.live_trader.settle_game(
                game_id=game_id,
                home_team=home_team,
                away_team=away_team,
                home_score=home_score,
                away_score=away_score,
                allow_draw=hasattr(game, "league_tag"),
            )
            self.probability_trader.settle_game(
                game_id=game_id,
                home_team=home_team,
                away_team=away_team,
                home_score=home_score,
                away_score=away_score,
                allow_draw=hasattr(game, "league_tag"),
            )
            logger.info("Settled game %s: %s %d - %d %s",
                        game_id, home_team, home_score,
                        away_score, away_team)

        for game in cs2_games or []:
            game_id = str(game.get("gameId", ""))
            if game_id not in open_game_ids or game_id in self.completed_games:
                continue
            if not game.get("ended"):
                continue
            parsed = parse_cs2_score(game.get("score", ""))
            if not parsed:
                continue
            _, _, home_maps, away_maps, _ = parsed
            if home_maps == away_maps:
                continue
            self.completed_games.add(game_id)
            home_team = game.get("homeTeam") or ""
            away_team = game.get("awayTeam") or ""
            for trader in (self.trader, self.live_trader, self.probability_trader):
                trader.settle_game(
                    game_id=game_id,
                    home_team=home_team,
                    away_team=away_team,
                    home_score=home_maps,
                    away_score=away_maps,
                )
            logger.info(
                "Settled CS2 match %s: %s %d - %d %s",
                game_id, home_team, home_maps, away_maps, away_team,
            )

    def _print_summary(self):
        s = self.trader.summary()
        logger.info("=== Bot Summary ===")
        for k, v in s.items():
            logger.info("  %s: %s", k, v)
        probability = self.probability_trader.summary()
        logger.info("=== Probability Paper Summary ===")
        for k, v in probability.items():
            logger.info("  %s: %s", k, v)

    def _load_score_change_state(self) -> dict[str, dict]:
        if not self._score_change_state_path.exists():
            return {}
        try:
            data = json.loads(self._score_change_state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed to load score change state: %s", exc)
            return {}

    def _touch_score_change(self, item: dict):
        signature = f"{item.get('home_score')}-{item.get('away_score')}"
        if item.get("detail"):
            signature = f"{signature}|{item['detail']}"
        game_id = str(item.get("id", ""))
        record = self.score_change_state.get(game_id)
        if not record or record.get("signature") != signature:
            record = {"signature": signature, "at": time.time()}
            self.score_change_state[game_id] = record
            self._save_score_change_state()
        item["score_change_at"] = record.get("at")

    def _save_score_change_state(self):
        try:
            atomic_write_json(self._score_change_state_path, self.score_change_state)
        except Exception as exc:
            logger.warning("Failed to save score change state: %s", exc)


def setup_logging(config):
    os.makedirs(os.path.dirname(config.log_path) or ".", exist_ok=True)
    # 日志轮转：bot.log 原来只增不减（已到 16 MB / 9 万行）
    handler: logging.Handler
    max_bytes = int(getattr(config, "log_max_bytes", 0) or 0)
    if max_bytes > 0:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(
            config.log_path,
            maxBytes=max_bytes,
            backupCount=int(getattr(config, "log_backup_count", 5) or 0),
            encoding="utf-8",
        )
    else:
        handler = logging.FileHandler(config.log_path, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            handler,
        ],
    )


def main():
    config = load_config()
    setup_logging(config)
    bot = LockBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
