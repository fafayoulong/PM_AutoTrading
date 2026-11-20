# -*- coding: utf-8 -*-
"""入场**前**盘口采集（只读，不影响下单）。

为什么必须有它（2026-09-28）
---------------------------
整个策略的盈利前提是「我们下单时市场还没收敛完」。这个前提验证了三次都
判不出结论，根因不在统计，而在**数据**：

  * `trades/position_paths.jsonl` 只采**入场之后**的快照 ——
    实测 `age_s` 最小值 0.3 秒、负值 0 条；
  * 我们的网球盘口（ITF/WTA）也不在 `soccer_latency_sampler` 的 PM 实时流
    覆盖里（138 个实盘网球 token 命中 0 个）。

于是"信号触发前价格已经走到哪"这件事**从来没有被记录过**，能拿到的只有
"成交价"和"成交之后的漂移"。而入场后价格不涨这一点，既可能是"市场已经收敛"，
也可能是"薄盘没人来收敛" —— 这两种解释对策略的含义完全相反，靠现有数据分不开。

采集对象（刻意收窄）
-------------------
不是所有比赛，而是**预匹配成功、且已经进入"潜在锁定"状态**的盘口：
网球 = 已分出盘数领先 + 当前盘尚未结束。价格真正开始向"锁定"收敛，就是从
这一刻开始的；再往前的价格对判断"我们是不是来晚了"没有信息量，只会浪费请求。

每个比赛只采**一个 token**（二元盘两侧价格互补，记录 bid/ask 就能还原对手侧），
每轮有目标数与并发上限 —— 扫描器只读公开 `/book`，但绝不能把下单链路的
网络挤掉。文件按体积轮转（复用 `atomic_io.rotate_by_size`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from src.atomic_io import rotate_by_size
from src.config import Config
from src.position_paths import ClobBookSource, summarize_book

logger = logging.getLogger(__name__)


class WatchPathRecorder:
    """按固定间隔给"潜在锁定"的盘口记一条快照。

    用法（见 `src/main.py`）：`run_forever(targets_provider)`，
    provider 每轮返回 `(sport, game_id, token_id, outcome, state_note)` 列表。
    内部按 `watch_path_interval_seconds` 判断每个 token 是否到点，
    单次抓取变慢不会把采样周期整体拖长。
    """

    def __init__(
        self,
        config: Config,
        *,
        source: Optional[ClobBookSource] = None,
        path: Optional[str | Path] = None,
    ):
        self.config = config
        self.path = Path(path or getattr(config, "watch_paths_path", "trades/watch_paths.jsonl"))
        self.source = source or ClobBookSource(config)
        self.enabled = bool(getattr(config, "watch_path_enabled", True))
        self.sports = {str(s).upper() for s in (getattr(config, "watch_path_sports", []) or [])}
        self.interval = max(1.0, float(getattr(config, "watch_path_interval_seconds", 3.0) or 3.0))
        self.max_targets = max(1, int(getattr(config, "watch_path_max_targets", 12) or 12))
        self.max_concurrent = max(1, int(getattr(config, "watch_path_max_concurrent", 2) or 2))
        self.timeout = max(1.0, float(getattr(config, "watch_path_timeout_seconds", 6.0) or 6.0))
        self.max_bytes = max(0, int(getattr(config, "watch_path_max_bytes", 0) or 0))
        self.backup_count = max(0, int(getattr(config, "watch_path_backup_count", 2) or 2))
        self._last_sampled: dict[str, float] = {}
        self._records_written = 0
        self._fetch_failures = 0
        self._empty_book = 0
        self._last_error = ""
        self._last_write_at = 0.0
        self._tokens_seen: set[str] = set()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self.enabled:
            await self.source.start()

    async def stop(self) -> None:
        await self.source.stop()

    def _due(self, token_id: str, now: float) -> bool:
        return now - self._last_sampled.get(token_id, 0.0) >= self.interval

    def _select(self, targets: Iterable) -> list[tuple]:
        """挑出本轮到点的目标（按"最久没采"排序，保证快照间隔尽量均匀）。"""
        now = time.time()
        candidates = []
        for item in targets or []:
            try:
                sport, game_id, token_id = item[0], item[1], item[2]
            except (TypeError, IndexError):
                continue
            token_id = str(token_id or "")
            if not token_id:
                continue
            if self.sports and str(sport or "").upper() not in self.sports:
                continue
            candidates.append((self._last_sampled.get(token_id, 0.0), item))
        candidates.sort(key=lambda pair: pair[0])
        due = [item for stamp, item in candidates if now - stamp >= self.interval]
        return due[: self.max_targets]

    async def tick(self, targets: Iterable) -> int:
        """对到点的目标抓一轮盘口并落盘，返回写入条数。"""
        if not self.enabled:
            return 0
        due = self._select(targets)
        if not due:
            return 0
        semaphore = asyncio.Semaphore(self.max_concurrent)
        records: list[dict] = []

        async def sample_one(item) -> None:
            sport, game_id, token_id, outcome, note = (
                item[0], item[1], str(item[2]), (item[3] if len(item) > 3 else ""),
                (item[4] if len(item) > 4 else ""),
            )
            async with semaphore:
                try:
                    book, source = await asyncio.wait_for(self.source.fetch(token_id), self.timeout)
                except asyncio.TimeoutError:
                    self._fetch_failures += 1
                    self._last_error = "timeout"
                    return
                except Exception as exc:  # noqa: BLE001 - 采集绝不影响主流程
                    self._fetch_failures += 1
                    self._last_error = str(exc) or type(exc).__name__
                    return
            if book is None:
                self._fetch_failures += 1
                return
            snapshot = summarize_book(book)
            if not snapshot:
                self._empty_book += 1
                return
            self._last_sampled[token_id] = time.time()
            self._tokens_seen.add(token_id)
            records.append(
                {
                    "t": time.time(),
                    "sport": sport,
                    "game_id": str(game_id),
                    "token_id": token_id,
                    "outcome": str(outcome or ""),
                    "state_note": str(note or ""),
                    "book_source": source,
                    **snapshot,
                }
            )

        await asyncio.gather(*(sample_one(item) for item in due), return_exceptions=True)
        if records:
            self._append(records)
        return len(records)

    def _append(self, records: list[dict]) -> None:
        try:
            rotate_by_size(self.path, max_bytes=self.max_bytes, backup_count=self.backup_count)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                for record in records:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._records_written += len(records)
            self._last_write_at = time.time()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"写入失败：{exc}"
            logger.warning("入场前盘口采集写入失败：%s", exc)

    async def run_forever(self, targets_provider: Callable[[], Iterable]) -> None:
        """独立后台循环：与主 tick 解耦，主循环忙时也不会漏采。"""
        if not self.enabled:
            logger.info("入场前盘口采集已关闭（WATCH_PATH_ENABLED=0）")
            return
        if not self.sports:
            logger.info("入场前盘口采集没有配置运动（WATCH_PATH_SPORTS），不启动")
            return
        await self.start()
        logger.info(
            "入场前盘口采集已启动：%s 每 %.0fs 一条（最多 %d 个/轮）→ %s",
            "/".join(sorted(self.sports)),
            self.interval,
            self.max_targets,
            self.path,
        )
        slice_seconds = min(2.0, max(0.5, self.interval / 3))
        try:
            while True:
                try:
                    await self.tick(targets_provider())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 采集绝不影响主流程
                    self._last_error = str(exc) or type(exc).__name__
                    logger.warning("入场前盘口采集异常（已忽略）：%s", exc)
                await asyncio.sleep(slice_seconds)
        finally:
            await self.stop()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "sports": sorted(self.sports),
            "interval_seconds": self.interval,
            "max_targets": self.max_targets,
            "records_written": self._records_written,
            "tokens_tracked": len(self._tokens_seen),
            "fetch_failures": self._fetch_failures,
            "empty_book": self._empty_book,
            "last_write_at": self._last_write_at or None,
            "last_error": self._last_error,
            "path": str(self.path),
        }
