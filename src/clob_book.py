# -*- coding: utf-8 -*-
"""仪表盘用的只读盘口面板数据源：取数 + 规范化 + 短 TTL 缓存 + 快照落盘。

为什么要有这个模块（2026-09-21，用户问"polymarket 上哪里可以看到订单簿"）
--------------------------------------------------------------------------
实测结论：Polymarket **网页 UI 没有完整订单簿**——体育比赛页和普通预测盘页
都没有 "Order Book / 订单簿" 入口，交易面板只给最优买卖价，能看到的历史只有
「活动」里的逐笔成交。完整阶梯只能走公开 REST。所以面板自带一个只读盘口区，
盯盘不用切网页。

三条设计约束（都是踩过的坑）：

1. **不碰交易链路**：dashboard 进程里没有、也不该初始化带密钥的客户端。取数只走
   公开 REST，直接复用 `position_paths.ClobBookSource` 的「客户端优先 + REST
   回退 + 重试 + 代理」逻辑——它本来就是为"实盘客户端未就绪也要能采盘口"写的。
2. **请求量与看盘人数解耦**：面板每 2 秒问一次，多开几个标签页请求就翻倍。
   这里按 token 做 1 秒内存缓存，命中直接返回。
3. **陈旧必须显式**：取数失败时**不抛异常、不返回空**，而是回上一次缓存并打上
   `stale=True` 与 `age_s`。盯盘最危险的是把旧盘口当实时——那会让人照着僵尸
   盘口判断"止损卖不卖得掉"。

还有一个排序坑：CLOB `/book` 原始返回是 **bids 升序、asks 降序，最优价在数组
末尾**。本模块统一规范成 bids 降序 / asks 升序（与 `tmp/show_book.py` 同口径），
前端不要再自己排序。

只读：不下单、不改任何账本，只往 `trades/book_snapshots.jsonl` 追加。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.atomic_io import rotate_by_size
from src.config import Config
from src.position_paths import ClobBookSource

logger = logging.getLogger(__name__)

BOOK_CACHE_TTL_SECONDS = 1.0
BOOK_CACHE_MAX_TOKENS = 16
DEFAULT_LEVELS = 8
MAX_LEVELS = 20
# 缓存里存的档数：比面板显示的多，这样"抓快照"能存下更完整的阶梯
MAX_STORED_LEVELS = 50

SNAPSHOT_PATH = "trades/book_snapshots.jsonl"
SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
SNAPSHOT_BACKUP_COUNT = 2

# 交易所时间戳超过这个秒数就算"陈旧盘口"，前端显示黄色警示
STALE_AFTER_SECONDS = 30.0


def _level_pairs(raw: Any) -> list[tuple[float, float]]:
    """把 {"price","size"} 或对象形式的档位解析成 (price, size)，丢掉非正数。"""
    pairs: list[tuple[float, float]] = []
    for row in list(raw or []):
        if isinstance(row, dict):
            price, size = row.get("price"), row.get("size")
        else:
            price, size = getattr(row, "price", None), getattr(row, "size", None)
        try:
            price = float(price)
            size = float(size)
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            pairs.append((price, size))
    return pairs


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _book_age_seconds(book: Any) -> Optional[float]:
    """交易所盘口时间戳距今多少秒（拿不到返回 None，不要瞎猜 0）。"""
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


def normalize_book(token_id: str, book: Any, levels: int = DEFAULT_LEVELS) -> dict:
    """把原始 `/book` 压成面板要的扁平结构。两侧都没有挂单时返回 ok=False。"""
    # 这里存的是"最大档数"，显示层再按需裁剪（见 _with_levels）
    levels = max(1, min(int(levels or DEFAULT_LEVELS), MAX_STORED_LEVELS))
    raw = book if isinstance(book, dict) else {}
    raw_bids = raw.get("bids") if isinstance(book, dict) else getattr(book, "bids", None)
    raw_asks = raw.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
    bids_raw = _level_pairs(raw_bids)
    asks_raw = _level_pairs(raw_asks)

    payload: dict = {
        "ok": False,
        "token_id": str(token_id),
        "error": None,
        "stale": False,
        "fetched_at": time.time(),
        "source": "",
        "tick_size": _as_float(raw.get("tick_size") if isinstance(raw, dict) else None),
        "min_order_size": _as_float(raw.get("min_order_size") if isinstance(raw, dict) else None),
        "last_trade_price": _as_float(raw.get("last_trade_price") if isinstance(raw, dict) else None),
        "book_age_s": _book_age_seconds(book),
        "best_bid": None,
        "best_ask": None,
        "bid_size": None,
        "ask_size": None,
        "mid": None,
        "spread": None,
        "bid_depth": 0.0,
        "ask_depth": 0.0,
        "bid_levels": len(bids_raw),
        "ask_levels": len(asks_raw),
        "one_sided": not (bids_raw and asks_raw),
        "bids": [],
        "asks": [],
    }
    if not bids_raw and not asks_raw:
        payload["error"] = "empty_book"
        return payload

    # 原始数组顺序不可信（bids 升序 / asks 降序），按价格重新定序
    bids = sorted(bids_raw, key=lambda pair: -pair[0])
    asks = sorted(asks_raw, key=lambda pair: pair[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    payload.update(
        {
            "ok": True,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_size": sum(size for price, size in bids if price == best_bid) if best_bid else None,
            "ask_size": sum(size for price, size in asks if price == best_ask) if best_ask else None,
            "mid": round((best_bid + best_ask) / 2, 6) if best_bid and best_ask else None,
            "spread": round(best_ask - best_bid, 6) if best_bid and best_ask else None,
            "bid_depth": round(sum(size for _, size in bids[:levels]), 4),
            "ask_depth": round(sum(size for _, size in asks[:levels]), 4),
            "bids": [[price, size] for price, size in bids[:levels]],
            "asks": [[price, size] for price, size in asks[:levels]],
        }
    )
    return payload


class BookFeed:
    """带短 TTL 缓存的只读盘口源；`get()` 保证不抛异常。"""

    def __init__(
        self,
        config: Config,
        *,
        ttl_seconds: float = BOOK_CACHE_TTL_SECONDS,
        max_tokens: int = BOOK_CACHE_MAX_TOKENS,
    ):
        self.config = config
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_tokens = max(1, int(max_tokens))
        self.source = ClobBookSource(config)
        self._cache: dict[str, dict] = {}
        self.requests = 0
        self.cache_hits = 0
        self.failures = 0
        self.last_error = ""

    async def start(self) -> None:
        await self.source.start()

    async def close(self) -> None:
        await self.source.stop()

    async def get(self, token_id: str, levels: int = DEFAULT_LEVELS) -> dict:
        token_id = str(token_id or "").strip()
        if not token_id:
            return {"ok": False, "token_id": "", "error": "missing_token_id",
                    "stale": False, "fetched_at": time.time()}

        cached = self._cache.get(token_id)
        now = time.time()
        if cached and now - float(cached.get("fetched_at") or 0) < self.ttl_seconds:
            self.cache_hits += 1
            return _with_levels(cached, levels)

        self.requests += 1
        book, source = await self.source.fetch(token_id)
        if book is not None:
            payload = normalize_book(token_id, book, levels=MAX_STORED_LEVELS)
            payload["source"] = source or self.source.last_source or "clob-rest"
            self._remember(token_id, payload)
            return _with_levels(payload, levels)

        # 取数失败：回上一次缓存并显式标注陈旧；没有缓存才算失败
        self.failures += 1
        # 空错误信息要补一句人话：面板上直接显示给用户看的
        self.last_error = self.source.last_error or "取盘口失败（无详情，多半是超时或代理不通）"
        if cached:
            fallback = dict(cached)
            fallback["stale"] = True
            fallback["error"] = self.last_error
            fallback["source"] = (fallback.get("source") or "cache") + "+cache"
            return _with_levels(fallback, levels)
        return {
            "ok": False,
            "token_id": token_id,
            "error": self.last_error,
            "stale": False,
            "fetched_at": now,
            "source": "clob-rest",
            "bids": [],
            "asks": [],
        }

    def _remember(self, token_id: str, payload: dict) -> None:
        self._cache[token_id] = payload
        if len(self._cache) <= self.max_tokens:
            return
        oldest = sorted(self._cache.items(), key=lambda kv: float(kv[1].get("fetched_at") or 0))
        for key, _ in oldest[: len(self._cache) - self.max_tokens]:
            self._cache.pop(key, None)

    def status(self) -> dict:
        return {
            "cached_tokens": len(self._cache),
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "last_error": self.last_error,
            "ttl_seconds": self.ttl_seconds,
        }

    def clear_cache(self) -> int:
        """丢掉缓存（排查"盘口看起来不动"时先清一次，强制回源）。"""
        count = len(self._cache)
        self._cache.clear()
        return count


def _with_levels(payload: dict, levels: int) -> dict:
    """按请求档数裁剪阶梯（缓存里存的是最大档数，避免每次重新取数）。"""
    # 上限用 MAX_STORED_LEVELS：面板显示由调用方（HTTP 层）自己再收到 20 档，
    # 但"抓快照"要能拿到完整阶梯
    levels = max(1, min(int(levels or DEFAULT_LEVELS), MAX_STORED_LEVELS))
    out = dict(payload)
    bids = list(payload.get("bids") or [])
    asks = list(payload.get("asks") or [])
    out["bids"] = bids[:levels]
    out["asks"] = asks[:levels]
    out["bid_depth"] = round(sum(size for _, size in out["bids"]), 4)
    out["ask_depth"] = round(sum(size for _, size in out["asks"]), 4)
    age = out.get("book_age_s")
    out["levels"] = levels
    if not out.get("stale") and age is not None and age > STALE_AFTER_SECONDS:
        # 交易所时间戳本身很旧 —— 盘口可能是挂着的僵尸价，标出来
        out["stale"] = True
        out["stale_reason"] = f"book_age_s={age:.0f}"
    return out


def snapshot_record(token_id: str, payload: dict, *, note: str = "", extra: Optional[dict] = None) -> dict:
    """拼一条落盘记录（含完整阶梯），字段口径与 position_paths 的采样对齐。"""
    stamp = time.time()
    record = {
        "t": round(stamp, 3),
        "beijing_time": datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds"),
        "kind": "book_snapshot",
        "note": note,
        "token_id": str(token_id),
        "source": payload.get("source"),
        "stale": bool(payload.get("stale")),
        "book_age_s": payload.get("book_age_s"),
        "best_bid": payload.get("best_bid"),
        "best_ask": payload.get("best_ask"),
        "bid_size": payload.get("bid_size"),
        "ask_size": payload.get("ask_size"),
        "mid": payload.get("mid"),
        "spread": payload.get("spread"),
        "tick_size": payload.get("tick_size"),
        "min_order_size": payload.get("min_order_size"),
        "bid_levels": payload.get("bid_levels"),
        "ask_levels": payload.get("ask_levels"),
        "bids": payload.get("bids") or [],
        "asks": payload.get("asks") or [],
    }
    if extra:
        record.update({k: v for k, v in extra.items() if k not in record})
    return record


def append_snapshot(path: str | Path = SNAPSHOT_PATH, record: Optional[dict] = None) -> tuple[bool, str]:
    """追加一条快照到 jsonl（超过阈值先轮转）。返回 (是否成功, 说明)。"""
    if not record:
        return False, "empty_record"
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        rotate_by_size(target, max_bytes=SNAPSHOT_MAX_BYTES, backup_count=SNAPSHOT_BACKUP_COUNT)
        with target.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True, str(target)
    except Exception as exc:  # noqa: BLE001 - 落盘失败不能影响看盘
        logger.warning("盘口快照落盘失败：%s", exc)
        return False, str(exc)
