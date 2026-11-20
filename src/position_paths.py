# -*- coding: utf-8 -*-
"""持仓期间盘口采集（只读，不影响下单）。

为什么需要它
------------
2026-09-12 的止损反事实回测（`tmp/backtest_stop_loss.py`）只能用 CLOB
`prices-history` 的**分钟中间价**，有两个绕不过去的盲区：

1. 中间价 0.55 不等于「有人愿意用 0.55 买」。挂出去的单能不能成交，取决于
   `best_bid` 有没有被打到，而中途价跌到 0.55 完全可能是「买一 0.28、卖一 0.32」
   这种单子根本碰不到的状态；
2. 真崩盘时买盘会消失（越急着止损越卖不掉），而插针那一刻盘口反而是正常的
   —— 中间价看不出这两种情形的区别。

所以当时的结论是「暂不采用」，并留下待办：把持仓期间的 `best_bid/best_ask`
记下来，1-2 周后用**真实买一价**重跑回测。这个模块就是那条待办。

它只读盘口、只追加写 `trades/position_paths.jsonl`，不参与任何下单决策，
也不改动任何既有账本文件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from src.atomic_io import rotate_by_size
from src.config import Config

logger = logging.getLogger(__name__)

BOOK_SOURCE_CLIENT = "clob-client"
BOOK_SOURCE_REST = "clob-rest"


def _raw_levels(book: Any, side: str) -> list:
    raw = book.get(side) if isinstance(book, dict) else getattr(book, side, None)
    return list(raw or [])


def _level_price_size(row: Any) -> tuple[float, float] | None:
    if isinstance(row, dict):
        price, size = row.get("price"), row.get("size")
    else:
        price, size = getattr(row, "price", None), getattr(row, "size", None)
    try:
        price = float(price)
        size = float(size)
    except (TypeError, ValueError):
        return None
    if price <= 0 or size <= 0:
        return None
    return price, size


def _book_age_seconds(book: Any) -> Optional[float]:
    raw = book.get("timestamp") if isinstance(book, dict) else getattr(book, "timestamp", None)
    if not raw:
        return None
    text = str(raw)
    try:
        if text.isdigit():
            stamp = float(text)
            if stamp > 1e12:  # 毫秒
                stamp /= 1000.0
            return max(0.0, time.time() - stamp)
    except Exception:  # noqa: BLE001
        pass
    try:
        return max(0.0, time.time() - datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def _entry_time_of(position: Any) -> float:
    """仓位入场时间。

    模拟盘（PaperPosition）用 `entry_time`，实盘（LivePosition）用 `opened_at` ——
    2026-09-21 把实盘仓位也纳入采样时才发现两者字段名不同。
    取不到就返回 0（调用方会把 age_s 记成 None，而不是算出一个天文数字）。
    """
    for name in ("entry_time", "opened_at"):
        try:
            value = float(getattr(position, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def summarize_book(book: Any) -> Optional[dict]:
    """把一份盘口压成扁平快照；两侧都没有挂单时返回 None。

    **单边盘口也要记**（只有买价或只有卖价）：这正是「想止损却没人接」的证据，
    之前直接丢掉，采集侧显示成 empty_book —— 把最有价值的样本过滤没了。

    2026-09-16：单边盘口以前会在这里抛 `TypeError: round(None)`（空的那一侧
    `bid_size/ask_size` 是 None），而调用方 `asyncio.gather(..., return_exceptions=True)`
    会把它当成"这一轮没采到"静默丢掉 —— 于是"单边也要记"这个承诺从来没兑现过
    （实测 7065 条采样里 one_sided 一条都没有）。现在空的一侧返回 None。
    """
    if book is None:
        return None
    bids = [pair for pair in (_level_price_size(row) for row in _raw_levels(book, "bids")) if pair]
    asks = [pair for pair in (_level_price_size(row) for row in _raw_levels(book, "asks")) if pair]
    if not bids and not asks:
        return None

    best_bid = max(price for price, _ in bids) if bids else None
    best_ask = min(price for price, _ in asks) if asks else None
    bid_size = sum(size for price, size in bids if price == best_bid) if best_bid is not None else None
    ask_size = sum(size for price, size in asks if price == best_ask) if best_ask is not None else None
    # 盘口深度按「离最优价最近的 5 档」累加：用来判断止损单真跌下去时有没有承接
    top_bids = sorted(bids, key=lambda pair: -pair[0])[:5]
    top_asks = sorted(asks, key=lambda pair: pair[0])[:5]
    tick = book.get("tick_size") if isinstance(book, dict) else getattr(book, "tick_size", None)
    try:
        tick = float(tick)
    except (TypeError, ValueError):
        tick = None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": round(bid_size, 4) if bid_size is not None else None,
        "ask_size": round(ask_size, 4) if ask_size is not None else None,
        "mid": round((best_bid + best_ask) / 2, 6) if bids and asks else None,
        "spread": round(best_ask - best_bid, 6) if bids and asks else None,
        "one_sided": not (bids and asks),
        "bid_depth5": round(sum(size for _, size in top_bids), 4),
        "ask_depth5": round(sum(size for _, size in top_asks), 4),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "tick_size": tick,
        "book_age_s": _book_age_seconds(book),
    }


class ClobBookSource:
    """取 CLOB 盘口：优先复用已就绪的 py-clob 客户端，否则回退公共 REST。

    备份路径很重要：实盘客户端没就绪（未配密钥 / 熔断 / 只读模式）时，
    采集不该跟着停摆 —— `/book` 是公开接口，无需任何凭证。
    """

    def __init__(
        self,
        config: Config,
        *,
        client_getter: Optional[Callable[[], Any]] = None,
    ):
        self.config = config
        self._client_getter = client_getter
        self._session = None
        self.last_source = ""
        self.last_error = ""

    async def start(self) -> None:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
            await self._warmup()

    async def _warmup(self) -> None:
        """建连预热：实测「新建 session 后的第一次请求」经代理经常失败，
        之后同一 session 全部正常。这里先打一次无意义请求把隧道建起来，
        失败也不影响后续（每个采样周期都会重试）。
        """
        import aiohttp

        if self._session is None:
            return
        url = f"{self.config.polymarket_clob_url}/"
        try:
            async with self._session.get(
                url,
                proxy=getattr(self.config, "proxy_url", None),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                logger.debug("CLOB 连接预热完成：HTTP %s", resp.status)
        except Exception as exc:  # noqa: BLE001
            logger.debug("CLOB 连接预热失败（忽略）：%s", exc)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, token_id: str) -> tuple[Optional[dict], str]:
        client = self._client_getter() if self._client_getter else None
        if client is not None:
            try:
                book = await asyncio.to_thread(client.get_order_book, token_id=token_id)
                if book is not None:
                    self.last_source = BOOK_SOURCE_CLIENT
                    return book, BOOK_SOURCE_CLIENT
            except Exception as exc:  # noqa: BLE001 - 回退 REST，不让采集中断
                # 超时类异常的 str() 常常是空串 —— 空错误信息会把"到底为什么失败"
                # 从日志里抹掉（2026-09-21 排查仪表盘盘口面板时踩到）
                self.last_error = str(exc) or type(exc).__name__
                logger.debug("CLOB 客户端取盘口失败，回退 REST：%s", exc)
        book = await self._fetch_rest(token_id)
        if book is not None:
            self.last_source = BOOK_SOURCE_REST
            return book, BOOK_SOURCE_REST
        return None, ""

    async def _fetch_rest(self, token_id: str) -> Optional[dict]:
        import aiohttp

        if self._session is None:
            await self.start()
        if self._session is None:  # pragma: no cover - start 失败时的兜底
            return None
        url = f"{self.config.polymarket_clob_url}/book"
        timeout = aiohttp.ClientTimeout(total=float(getattr(self.config, "position_path_timeout_seconds", 6.0)))
        proxy = getattr(self.config, "proxy_url", None)
        # 代理/连接偶发抖动（实测首连失败率不低）→ 同一次采样内重试一次
        for attempt in range(2):
            try:
                async with self._session.get(
                    url, params={"token_id": token_id}, proxy=proxy, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        self.last_error = f"HTTP {resp.status}"
                        return None
                    return await resp.json()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc) or type(exc).__name__
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                return None
        return None


class ShadowStopTracker:
    """影子止损：只记录「如果挂了止损单会怎样」，不下任何卖单、不改任何仓位。

    为什么需要它（2026-09-15）：用真实买一价做的止损反事实回测只有 15 笔样本、
    其中 2 笔亏损，阈值排序完全由那两笔决定（Basiletti 最低 0.450、Andreeva 最低 0.080）
    —— 只要有一笔多跌一分钱，0.45 和 0.40 的名次就会翻转。这是典型的过拟合，
    直接上线卖出等于拿真金白银赌一个噪声参数。

    影子模式让每个新仓位自动产生反事实数据，攒够样本再定线上阈值。
    同时评估多组参数，避免"只测了一条线、两周后发现选错了"：
      * 阈值：0.70 → 0.40（浅的保护多但误杀多）
      * 确认：连续 N 次采样跌破才触发（1=立刻、2≈16 秒、3≈24 秒）——
        确认规则是**有状态**的，不是从价格轨迹一眼能看出来的，必须在这里跟着记。
    """

    def __init__(self, path: str | Path, thresholds: Iterable[float], confirms: Iterable[int]):
        self.path = Path(path)
        self.thresholds = [float(t) for t in thresholds]
        self.confirms = [max(1, int(c)) for c in confirms]
        self._below: dict[tuple[str, float], int] = {}      # 连续跌破计数
        self._fired: set[tuple[str, float, int]] = set()    # 已触发，避免重复记录
        self._tracked: set[str] = set()
        self.events_written = 0
        # 重启不清空数据：文件是只追加的，进程内的计数器会归零，
        # 所以启动时先把"文件里已有多少条"读进来，面板才不会显示成 0 条（2026-09-18 用户问）。
        self.events_in_file_at_start = self._count_existing_events()

    def _count_existing_events(self) -> int:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                return sum(1 for line in file if line.strip())
        except FileNotFoundError:
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("影子止损历史条数读取失败：%s", exc)
            return 0

    def observe(self, position: Any, snapshot: dict, when: float, ledger: str = "paper") -> list[dict]:
        """喂一条盘口快照；返回本次首次触发的事件（不同阈值/确认各算一条）。"""
        position_id = str(getattr(position, "position_id", "") or "")
        bid = snapshot.get("best_bid")
        entry = float(getattr(position, "entry_price", 0) or 0)
        if not position_id or bid is None or entry <= 0:
            return []
        self._tracked.add(position_id)
        size = float(getattr(position, "size", 0) or 0)
        entry_time = _entry_time_of(position)
        events: list[dict] = []
        for threshold in self.thresholds:
            if threshold >= entry:
                # 入场价本来就低于止损线，这条止损对它没有意义
                continue
            key = (position_id, threshold)
            if bid <= threshold:
                self._below[key] = self._below.get(key, 0) + 1
            else:
                self._below[key] = 0
            count = self._below[key]
            for confirm in self.confirms:
                token = (position_id, threshold, confirm)
                if count < confirm or token in self._fired:
                    continue
                self._fired.add(token)
                events.append({
                    "kind": "shadow-stop",
                    "t": when,
                    "beijing_time": datetime.fromtimestamp(when).astimezone().isoformat(timespec="seconds"),
                    "ledger": ledger,
                    "position_id": position_id,
                    "game_id": getattr(position, "game_id", ""),
                    "sport": getattr(position, "sport", "Unknown"),
                    "strategy": getattr(position, "strategy", ""),
                    "team": getattr(position, "team", ""),
                    "token_id": getattr(position, "token_id", ""),
                    "entry_price": entry,
                    "size": size,
                    "threshold": threshold,
                    "confirm_samples": confirm,
                    "age_s": round(when - entry_time, 1) if entry_time else None,
                    "best_bid_at_trigger": bid,
                    "best_ask_at_trigger": snapshot.get("best_ask"),
                    "bid_size_at_trigger": snapshot.get("bid_size"),
                    # 两种成交口径：挂单（乐观，按阈值成交）/ 追价（悲观，按当时买一成交）
                    "exit_pnl_limit": round((threshold - entry) * size, 2),
                    "exit_pnl_chased": round((bid - entry) * size, 2),
                    # 触发时已经跌穿阈值多少（跳空/滑点大小）
                    "gap_below_threshold": round(threshold - bid, 4),
                })
        if events:
            self._append(events)
        return events

    def _append(self, events: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                for event in events:
                    file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.events_written += len(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入影子止损记录失败：%s", exc)

    def forget(self, position_id: str) -> None:
        """仓位结算后清掉它的状态，避免内存无限增长。"""
        self._tracked.discard(position_id)
        for key in [k for k in self._below if k[0] == position_id]:
            self._below.pop(key, None)
        for key in [k for k in self._fired if k[0] == position_id]:
            self._fired.discard(key)

    def status(self) -> dict:
        return {
            "enabled": True,
            "path": str(self.path),
            "thresholds": self.thresholds,
            "confirm_samples": self.confirms,
            "tracking": len(self._tracked),
            "events_written": self.events_written,
            # 面板展示用：本次运行写了多少 / 文件里累计多少（重启不会清空）
            "events_this_run": self.events_written,
            "events_total": self.events_in_file_at_start + self.events_written,
        }


class DipAddShadowTracker:
    """补单影子线：只记录「如果跌到 X 再补一单会怎样」，不下单、不改任何账本。

    为什么要有它（2026-09-21 用户提出）
    ----------------------------------
    用户观察："网球每次买入后价格总会波动、下降很多，但最后还是我赢 ——
    能不能等价格跌到低点再补一单？"

    复盘 `tmp/analyze_tennis_dip.py`（62 笔有入场后路径的模拟盘仓位）：
      * 现象是真的：69% 的仓位回撤 ≥3pp，中位 −6.2pp，90% 最终回到入场价之上；
      * 但回撤**带信息**：赢单回撤中位 −5.9pp、输单 −75.8pp（4 笔输单全部 ≥5pp），
        条件胜率随深度下降（全体 94% → ≥5pp 档 89% → ≥8pp 档 86%）；
      * 反事实（固定金额、按当时卖一成交）在 ≥3pp 档有 +8.1pp 边际，z≈1.7，
        而且**全部来自模拟盘** —— 实盘此前没有入场后路径。

    所以"补单"不是捡便宜，而是**在同一个事件上加杠杆**。这里只记录，
    胜负留给离线分析（`tmp/analyze_dip_add_shadow.py`）和真实结算对齐。

    与影子止损共用同一条采样流，**不产生额外 CLOB 请求**。
    """

    def __init__(
        self,
        path: str | Path,
        thresholds: Iterable[float],
        *,
        min_entry_price: float = 0.60,
        max_bytes: int = 32 * 1024 * 1024,
        backup_count: int = 2,
    ):
        self.path = Path(path)
        self.thresholds = sorted({float(t) for t in thresholds if float(t) > 0})
        # 我们自己实盘闸门 `live_min_entry_price`：补单发生在它下面的话，
        # 真上线会被闸门拦掉 —— 记在事件里，离线分析才能分开看"浅坑 / 深坑"
        self.min_entry_price = float(min_entry_price)
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._baseline: dict[str, float] = {}     # position_id → 入场后第一份中间价
        self._fired: set[tuple[str, float]] = set()
        self._tracked: set[str] = set()
        self.events_written = 0
        self.events_in_file_at_start = self._count_existing_events()

    def _count_existing_events(self) -> int:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                return sum(1 for line in file if line.strip())
        except FileNotFoundError:
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("补单影子线历史条数读取失败：%s", exc)
            return 0

    def observe(self, position: Any, snapshot: dict, when: float, ledger: str = "paper") -> list[dict]:
        """喂一条盘口快照；返回本次首次触发的事件（每个阈值各一条，最多一次）。"""
        position_id = str(getattr(position, "position_id", "") or "")
        bid = snapshot.get("best_bid")
        ask = snapshot.get("best_ask")
        if not position_id or bid is None or ask is None:
            return []
        try:
            bid = float(bid)
            ask = float(ask)
        except (TypeError, ValueError):
            return []
        if bid <= 0 or ask <= 0:
            return []

        # 基准口径与离线复盘一致：入场后**第一份双边中间价**（不是我们的成交价，
        # 免得把"买贵的那 1 个 tick"算成回撤）
        base_mid = self._baseline.get(position_id)
        if base_mid is None:
            base_mid = (bid + ask) / 2
            self._baseline[position_id] = base_mid
        self._tracked.add(position_id)

        entry = float(getattr(position, "entry_price", 0) or 0)
        size = float(getattr(position, "size", 0) or 0)
        cost = float(getattr(position, "cost", 0) or 0) or (size * entry if entry else 0.0)
        entry_time = _entry_time_of(position)
        events: list[dict] = []
        for threshold in self.thresholds:
            key = (position_id, threshold)
            if key in self._fired:
                continue
            if ask > base_mid - threshold:
                continue
            self._fired.add(key)
            # "和原仓位同金额再买一单"
            stake = cost if cost > 0 else size * ask
            shares = stake / ask if ask > 0 else 0.0
            events.append({
                "kind": "dip-add-shadow",
                "t": when,
                "beijing_time": datetime.fromtimestamp(when).astimezone().isoformat(timespec="seconds"),
                "ledger": ledger,
                "position_id": position_id,
                "game_id": getattr(position, "game_id", ""),
                "sport": getattr(position, "sport", "Unknown"),
                "strategy": getattr(position, "strategy", ""),
                "team": getattr(position, "team", ""),
                "token_id": getattr(position, "token_id", ""),
                "entry_price": entry,
                "size": size,
                "cost": round(cost, 4),
                "base_mid": round(base_mid, 6),
                "threshold": threshold,
                "dip_pp": round(threshold * 100, 2),
                "drop_from_base_pp": round((ask - base_mid) * 100, 2),
                "age_s": round(when - entry_time, 1) if entry_time else None,
                "best_ask_at_trigger": ask,
                "best_bid_at_trigger": bid,
                "ask_size_at_trigger": snapshot.get("ask_size"),
                "add_stake_usdc": round(stake, 4),
                "virtual_shares": round(shares, 4),
                # 两种终局下的账面；真实胜负由离线分析按结算结果填
                "pnl_if_win": round(shares * (1 - ask), 4),
                "pnl_if_loss": round(-stake, 4),
                "breakeven_win_rate": round(ask, 6),
                "within_min_entry_gate": bool(ask >= self.min_entry_price),
                "note": "影子记录，未下任何单",
            })
        if events:
            self._append(events)
        return events

    def _append(self, events: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rotate_by_size(self.path, max_bytes=self.max_bytes, backup_count=self.backup_count)
            with self.path.open("a", encoding="utf-8") as file:
                for event in events:
                    file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.events_written += len(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入补单影子线失败：%s", exc)

    def forget(self, position_id: str) -> None:
        """仓位结算后清状态，避免内存无限增长。"""
        self._tracked.discard(position_id)
        self._baseline.pop(position_id, None)
        for key in [k for k in self._fired if k[0] == position_id]:
            self._fired.discard(key)

    def status(self) -> dict:
        return {
            "enabled": True,
            "path": str(self.path),
            "thresholds": self.thresholds,
            "min_entry_price": self.min_entry_price,
            "tracking": len(self._tracked),
            "events_this_run": self.events_written,
            "events_total": self.events_in_file_at_start + self.events_written,
        }


def analyze_shadow_stop(
    events_path: "str | Path",
    positions: Iterable[Any] | Mapping[str, Any],
    *,
    min_settled: int = 8,
) -> dict:
    """把影子止损记录汇总成「哪条止损线最划算」的表。

    面板和 CLI（`tmp/analyze_shadow_stop.py`）**共用这一份实现** —— 原来只有 node 版
    (`tmp/analyze_shadow_stop.js`)，面板要显示就得再写一份，两份逻辑迟早走偏。

    对每条触发记录，用 `position_id` 找到真实仓位：
      * 仓位还没结算 → 无法判断是误杀还是救回，只计入 `open_triggers`；
      * 已结算 → `净变化 = 止损出场盈亏 − 实际持有到结算盈亏`（正数=止损更划算）；
        实际是盈利却被打掉 → 记一次**误杀**；实际是亏损 → 记一次**救回**。

    两种成交口径：`delta_limit`（假设买单打到阈值价就成交，**上限**）、
    `delta_chased`（按触发时真实买一价成交，**下限**）。
    """
    path = Path(events_path)
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        events = []
    except Exception as exc:  # noqa: BLE001
        logger.debug("影子止损记录读取失败：%s", exc)
        events = []

    if isinstance(positions, Mapping):
        by_id = {str(key): value for key, value in positions.items()}
    else:
        by_id = {str(getattr(p, "position_id", "")): p for p in positions}

    def field(obj: Any, name: str, default: Any = None) -> Any:
        """仓位既可能是 PaperPosition 对象，也可能是 state.json 里的 dict。"""
        if isinstance(obj, Mapping):
            return obj.get(name, default)
        return getattr(obj, name, default)

    groups: dict[tuple, dict] = {}
    settled_triggers = 0
    open_triggers = 0
    for event in events:
        position = by_id.get(str(event.get("position_id") or ""))
        if position is None:
            continue
        status = str(field(position, "status", "") or "")
        threshold = float(event.get("threshold") or 0.0)
        confirm = int(event.get("confirm_samples") or 1)
        group = groups.setdefault(
            (threshold, confirm),
            {
                "threshold": threshold, "confirm": confirm, "triggers": 0, "settled": 0,
                "killed": 0, "saved": 0, "delta_limit": 0.0, "delta_chased": 0.0,
                "open": 0, "gaps": [],
            },
        )
        group["triggers"] += 1
        group["gaps"].append(float(event.get("gap_below_threshold") or 0.0))
        if not status.startswith("CLOSED"):
            group["open"] += 1
            open_triggers += 1
            continue
        settled_triggers += 1
        group["settled"] += 1
        actual = float(field(position, "pnl", 0.0) or 0.0)
        group["delta_limit"] += float(event.get("exit_pnl_limit") or 0.0) - actual
        group["delta_chased"] += float(event.get("exit_pnl_chased") or 0.0) - actual
        if actual > 0:
            group["killed"] += 1
        else:
            group["saved"] += 1

    rows = []
    for group in groups.values():
        gaps = sorted(group.pop("gaps"))
        group["median_gap"] = round(gaps[len(gaps) // 2], 4) if gaps else 0.0
        group["delta_limit"] = round(group["delta_limit"], 2)
        group["delta_chased"] = round(group["delta_chased"], 2)
        rows.append(group)
    # 排序：净变化从好到坏（同分看阈值小的优先，尽量少砍仓位）
    rows.sort(key=lambda r: (-r["delta_limit"], r["threshold"], r["confirm"]))
    best = rows[0] if rows else None
    return {
        "events_total": len(events),
        "settled_triggers": settled_triggers,
        "open_triggers": open_triggers,
        "min_settled": min_settled,
        "conclusive": settled_triggers >= min_settled,
        "best": best,
        "all_negative": bool(rows) and all(r["delta_limit"] <= 0 for r in rows),
        "rows": rows,
    }


class PositionPathRecorder:
    """按固定间隔给每个未结算持仓记一条盘口快照。

    用法（见 `src/main.py`）：`tick()` 每秒钟调用一次，内部按
    `position_path_interval_seconds` 判断每个持仓是否「到点」，
    因此单次抓取变慢不会把采样周期整体拖长。
    """

    def __init__(
        self,
        config: Config,
        *,
        source: Optional[ClobBookSource] = None,
        path: Optional[str | Path] = None,
    ):
        self.config = config
        self.path = Path(path or getattr(config, "position_paths_path", "trades/position_paths.jsonl"))
        self.source = source or ClobBookSource(config)
        self.enabled = bool(getattr(config, "position_path_enabled", True))
        self.interval = max(2.0, float(getattr(config, "position_path_interval_seconds", 8) or 8))
        self.max_concurrent = max(1, int(getattr(config, "position_path_max_concurrent", 4) or 4))
        self.timeout = max(1.0, float(getattr(config, "position_path_timeout_seconds", 6.0) or 6.0))
        self.max_bytes = max(0, int(getattr(config, "position_path_max_bytes", 0) or 0))
        self.backup_count = max(0, int(getattr(config, "position_path_backup_count", 2) or 0))
        self._last_sampled: dict[str, float] = {}
        self._warned_missing_token: set[str] = set()
        self._warned_parse_failure: set[str] = set()
        self._records_written = 0
        # 影子止损：跟着同一条采样流评估"如果挂了止损会怎样"（只记录，不卖出）
        self.shadow = None
        if bool(getattr(config, "shadow_stop_enabled", True)):
            self.shadow = ShadowStopTracker(
                getattr(config, "shadow_stop_path", "trades/shadow_stop_events.jsonl"),
                getattr(config, "shadow_stop_thresholds", [0.55]),
                getattr(config, "shadow_stop_confirm_samples", [1]),
            )
        # 补单影子线：同样跟着这条采样流评估"如果跌到 X 再补一单会怎样"（只记录）
        self.dip_add = None
        if bool(getattr(config, "dip_add_shadow_enabled", True)):
            self.dip_add = DipAddShadowTracker(
                getattr(config, "dip_add_shadow_path", "trades/dip_add_shadow.jsonl"),
                getattr(config, "dip_add_shadow_thresholds", [0.02, 0.03, 0.05]),
                min_entry_price=float(getattr(config, "live_min_entry_price", 0.60) or 0.60),
                max_bytes=int(getattr(config, "dip_add_shadow_max_bytes", 32 * 1024 * 1024) or 0),
                backup_count=int(getattr(config, "dip_add_shadow_backup_count", 2) or 0),
            )
        self._fetch_failures = 0
        self._missing_token = 0
        self._empty_book = 0
        # 解析异常单独计数：以前它被 gather(return_exceptions=True) 静默吞掉，
        # 「单边盘口也记」的 bug 就是这么藏了两周的（见 summarize_book 注释）。
        self._parse_failures = 0
        self._last_write_at = 0.0
        self._last_error = ""

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self.enabled:
            await self.source.start()

    async def stop(self) -> None:
        await self.source.stop()

    async def run_forever(self, targets_provider: Callable[[], Iterable]) -> None:
        """独立后台循环：与主 tick 解耦，主循环忙时也不会漏采。"""
        if not self.enabled:
            logger.info("持仓盘口采集已关闭（POSITION_PATH_ENABLED=0）")
            return
        await self.start()
        logger.info(
            "持仓盘口采集已启动：每 %.0fs 一条 → %s", self.interval, self.path
        )
        slice_seconds = min(2.0, max(0.5, self.interval / 4))
        try:
            while True:
                try:
                    await self.tick(targets_provider())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 采集绝不影响主流程
                    self._last_error = str(exc)
                    logger.warning("持仓盘口采集异常（已忽略）：%s", exc)
                await asyncio.sleep(slice_seconds)
        finally:
            await self.stop()

    # ---------- 采样 ----------

    def _due(self, position: Any, now: float) -> bool:
        position_id = getattr(position, "position_id", "") or ""
        return now - self._last_sampled.get(position_id, 0.0) >= self.interval

    async def tick(self, targets: Iterable) -> int:
        """对到点的持仓抓一轮盘口并落盘，返回写入的记录条数。"""
        if not self.enabled:
            return 0
        now = time.time()
        due: list[tuple[str, Any, str]] = []
        for item in targets or []:
            ledger, position = (item if isinstance(item, (tuple, list)) else ("", item))
            token_id = str(getattr(position, "token_id", "") or "")
            if not token_id:
                position_id = getattr(position, "position_id", "") or ""
                self._missing_token += 1
                if position_id and position_id not in self._warned_missing_token:
                    self._warned_missing_token.add(position_id)
                    logger.info(
                        "持仓 %s 没有 token_id，跳过盘口采集（旧仓位无法回溯补采）", position_id
                    )
                continue
            if self._due(position, now):
                due.append((ledger, position, token_id))
        if not due:
            return 0

        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def sample_one(ledger: str, position: Any, token_id: str) -> Optional[dict]:
            async with semaphore:
                try:
                    book, source = await asyncio.wait_for(self.source.fetch(token_id), self.timeout)
                except asyncio.TimeoutError:
                    self._fetch_failures += 1
                    self._last_error = "timeout"
                    return None
                except Exception as exc:  # noqa: BLE001
                    self._fetch_failures += 1
                    self._last_error = str(exc)
                    return None
            # 即使这次没拿到盘口也要打时间戳，避免失败时每秒钟重试一次
            self._last_sampled[str(getattr(position, "position_id", ""))] = time.time()
            try:
                snapshot = summarize_book(book)
            except Exception as exc:  # noqa: BLE001 - 解析异常要留痕，不能再静默丢样本
                self._parse_failures += 1
                self._last_error = f"parse: {exc}"
                position_id = str(getattr(position, "position_id", "") or "")
                if position_id and position_id not in self._warned_parse_failure:
                    self._warned_parse_failure.add(position_id)
                    logger.warning("盘口解析失败（%s）：%s", position_id, exc)
                return None
            if snapshot is None:
                self._empty_book += 1
                return None
            stamp = time.time()
            entry_time = _entry_time_of(position)
            return {
                "t": round(stamp, 3),
                "beijing_time": datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds"),
                "ledger": ledger,
                "position_id": getattr(position, "position_id", ""),
                "game_id": getattr(position, "game_id", ""),
                "event_id": getattr(position, "event_id", None),
                "sport": getattr(position, "sport", "Unknown"),
                "strategy": getattr(position, "strategy", ""),
                "team": getattr(position, "team", ""),
                "token_id": token_id,
                "entry_price": getattr(position, "entry_price", None),
                "size": getattr(position, "size", None),
                "age_s": round(stamp - entry_time, 3) if entry_time else None,
                "book_source": source,
                **snapshot,
            }

        records = await asyncio.gather(
            *(sample_one(ledger, position, token_id) for ledger, position, token_id in due),
            return_exceptions=True,
        )
        valid = [row for row in records if isinstance(row, dict)]
        self._append(valid)
        # 影子止损 / 补单影子线：都用刚采到的盘口评估"如果……会怎样"。
        # 放在采样之后，只对真正拿到盘口的持仓评估（快照里 best_bid 一定有值）。
        if (self.shadow is not None or self.dip_add is not None) and valid:
            pairs = [
                (item if isinstance(item, (tuple, list)) else ("", item))
                for item in targets or []
            ]
            by_id = {str(getattr(p, "position_id", "")): (ledger, p) for ledger, p in pairs}
            for row in valid:
                item = by_id.get(str(row.get("position_id") or ""))
                if not item:
                    continue
                ledger, position = item
                book_view = {
                    "best_bid": row.get("best_bid"),
                    "best_ask": row.get("best_ask"),
                    "bid_size": row.get("bid_size"),
                    "ask_size": row.get("ask_size"),
                }
                when = float(row.get("t") or time.time())
                # 影子止损保持原有样本口径（模拟盘两本 + 被闸门拒掉的观察目标）；
                # 实盘仓位是 2026-09-21 才纳入采样的，混进来会改变那条线的口径
                if self.shadow is not None and ledger != "live":
                    self.shadow.observe(position, book_view, when, ledger)
                if self.dip_add is not None:
                    self.dip_add.observe(position, book_view, when, ledger)
            # 已结算的持仓清掉影子状态，避免内存无限增长
            live_ids = {str(getattr(p, "position_id", "")) for _l, p in pairs}
            for tracker in (self.shadow, self.dip_add):
                if tracker is None:
                    continue
                for stale in [pid for pid in list(tracker._tracked) if pid not in live_ids]:
                    tracker.forget(stale)
        return len(valid)

    def _append(self, records: list[dict]) -> None:
        if not records:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rotate_by_size(self.path, max_bytes=self.max_bytes, backup_count=self.backup_count)
            with self.path.open("a", encoding="utf-8") as file:
                for record in records:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._records_written += len(records)
            self._last_write_at = time.time()
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("写入持仓盘口记录失败：%s", exc)

    # ---------- 观测 ----------

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "interval_seconds": self.interval,
            "tracking": len(self._last_sampled),
            "records_written": self._records_written,
            "last_write_at": self._last_write_at or None,
            "fetch_failures": self._fetch_failures,
            "empty_book": self._empty_book,
            "missing_token": self._missing_token,
            "parse_failures": self._parse_failures,
            "last_source": getattr(self.source, "last_source", ""),
            "last_error": self._last_error or getattr(self.source, "last_error", ""),
            "shadow_stop": self.shadow.status() if self.shadow is not None else {"enabled": False},
            "dip_add_shadow": self.dip_add.status() if self.dip_add is not None else {"enabled": False},
        }
