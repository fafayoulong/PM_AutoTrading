# -*- coding: utf-8 -*-
"""模拟盘结算兜底：比分流漏掉终局时，用**市场结算结果**补结算。

为什么必须有这条路径（2026-09-20 实测）：
  模拟盘的仓位原来只有一条结算路径 —— `main._check_settlements` 里的比分流，
  要求"该场比赛这一 tick 还在快照里，且 ended=True"。而 PM 实时流的比赛行在
  终局 30 分钟后就被 `_evict_stale` 淘汰：只要 WS 抖一下（9-18~9-19 三天
  151 次重连，其中 09-19 03:42–03:48 连着重连 4 次），终局那条消息就永远丢了，
  仓位会**永久**停在 OPEN。实盘没有这个问题，因为它按交易所对账结算。

  代价不只是"少记一笔"：模拟盘 CS2 因此显示 22 胜 0 负，而实盘是 13 胜 1 负；
  用模拟盘胜率验证策略 edge 时，这种失真直接污染结论。

设计要点：
  * **比分流优先**：默认等 `paper_settle_reconcile_min_age_seconds`（30 分钟，
    正好是实时流淘汰终局的宽限）之后才插手，正常比赛仍然由比分流秒级结算；
  * **与传输层无关**：直接问 Gamma"这个 token 最后结算成什么"，
    并且只在市场真的 closed 且价已到 0/1 时才写账（见 src/settlement_lookup.py）；
  * **绝不猜**：查不到、还没结算、结算价不是 0/1 —— 一律保持 OPEN，只记日志；
  * 结算写入走 `trader.close_position(..., source="MARKET_RESOLVE")`，
    现金/权益曲线的口径与比分流结算完全一致，并在面板上标出结算来源。
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from src import settlement_lookup

logger = logging.getLogger(__name__)


class SettlementReconciler:
    """周期性给"超时仍未结算"的模拟盘仓位查市场结果并补结算。"""

    # 每轮最多查几个仓位：开仓未结算的通常个位数，留个上限避免网络抖动时卡住循环
    MAX_LOOKUPS_PER_TICK = 6
    # 每个仓位的"跳过原因"只在变化时打一次日志，避免每 10 分钟刷同一行
    _REASON_THROTTLE = True

    def __init__(self, config):
        self.config = config
        self.enabled = bool(getattr(config, "paper_settle_reconcile_enabled", True))
        self.interval = float(
            getattr(config, "paper_settle_reconcile_interval_seconds", 600.0) or 600.0
        )
        self.min_age = float(
            getattr(config, "paper_settle_reconcile_min_age_seconds", 1800.0) or 1800.0
        )
        self.timeout = float(
            getattr(config, "paper_settle_reconcile_timeout_seconds", 20.0) or 20.0
        )
        self.path = Path(
            getattr(config, "paper_settle_reconcile_path", "trades/settlement_reconcile_events.jsonl")
        )
        self._event_paths = [
            Path(config.signal_events_path),
            Path(config.probability_events_path),
        ]
        # event_id -> 市场问题文本（比分流结算靠 game_id，市场结算要靠问题文本定位市场）
        self._questions: dict[str, str] = {}
        self._offsets: dict[str, int] = {}
        self._last_note: dict[str, str] = {}
        self.stats: dict[str, object] = {
            "checked": 0,
            "settled": 0,
            "skipped": 0,
            "errors": 0,
            "last_error": "",
            "last_run_at": None,
            "last_note": "",
        }

    # ---------- 市场问题索引 ----------

    def _refresh_questions(self) -> int:
        """增量读 signal_events / probability_events，补出 event_id → 市场问题。

        只读新增字节（两个日志都是纯追加），文件被轮转/截断时从头再来。
        """
        added = 0
        for path in self._event_paths:
            key = str(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self._offsets.get(key, 0)
            if size < offset:
                offset = 0
            if size == offset:
                continue
            try:
                with path.open("r", encoding="utf-8") as file:
                    file.seek(offset)
                    for line in file:
                        if not line.endswith("\n"):
                            # 半行：不推进偏移，下一轮补上
                            break
                        offset += len(line.encode("utf-8"))
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event_id = str(event.get("event_id") or "")
                        question = str((event.get("details") or {}).get("market_question") or "")
                        if event_id and question and event_id not in self._questions:
                            self._questions[event_id] = question
                            added += 1
            except OSError as exc:
                self.stats["last_error"] = f"读取事件日志失败：{exc}"
                continue
            self._offsets[key] = offset
        if added and self._questions:
            logger.info("结算兜底：市场问题索引 +%d（共 %d 条）", added, len(self._questions))
        return added

    def _question_for(self, position) -> str:
        event_id = str(getattr(position, "event_id", "") or "")
        if not event_id:
            return ""
        return self._questions.get(event_id, "")

    # ---------- 结算 ----------

    def _log_event(self, payload: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("写结算兜底日志失败：%s", exc)

    def _note_once(self, position_id: str, reason: str, message: str) -> None:
        """同一个仓位重复产生同一原因时不重复打日志。"""
        if self._REASON_THROTTLE and self._last_note.get(position_id) == reason:
            return
        self._last_note[position_id] = reason
        self.stats["last_note"] = message
        logger.info("结算兜底：%s", message)

    async def tick(self, targets) -> list[dict]:
        """给到点的未结算仓位查一次市场结果，返回本轮真正结算掉的仓位。"""
        if not self.enabled:
            return []
        self._refresh_questions()
        now = time.time()
        settled: list[dict] = []
        lookups = 0
        for item in targets or []:
            ledger, trader, position = item
            position_id = str(getattr(position, "position_id", "") or "")
            token_id = str(getattr(position, "token_id", "") or "")
            team = str(getattr(position, "team", "") or "")
            # 老仓位没有 token_id（实测 CS2 有 34 个），以前这里直接 continue →
            # 只要比分流漏了终局，这些仓位就**永远**补不了结算。
            # 实际上 `settlement_lookup` 没有 token 也能按"市场问题精确匹配"定位
            # （已在 pick_resolved_market 里做唯一性校验，歧义时拒绝结算），
            # 所以这里放宽为"有 token 或有问题文本即可"（2026-09-28）。
            if not position_id:
                continue
            age = now - float(getattr(position, "entry_time", 0) or 0)
            if age < self.min_age:
                # 比分流的宽限期内：正常比赛会在这之前结掉，别抢它的活
                continue
            if lookups >= self.MAX_LOOKUPS_PER_TICK:
                logger.info("结算兜底：本轮已达查询上限 %d，其余下轮处理", self.MAX_LOOKUPS_PER_TICK)
                break
            question = self._question_for(position)
            # 查询只能按**问题文本**做（Gamma public-search 是文本检索；
            # 只有 token_id 时拿它当查询串等于白查），所以缺问题文本仍然跳过 ——
            # 这与"老仓位没有 token_id 也能补结算"是两件事，别混。
            if not question:
                self.stats["skipped"] += 1
                self._note_once(
                    position_id,
                    "NO_QUESTION",
                    f"{ledger} {position_id} {team}：没有市场问题文本"
                    f"（event_id={getattr(position, 'event_id', None)}"
                    f"{'，有 token_id' if token_id else '，token_id 也缺'}），"
                    f"无法查结算，保持未结算",
                )
                continue
            lookups += 1
            self.stats["checked"] += 1
            try:
                price, note = await asyncio.to_thread(
                    settlement_lookup.resolve_outcome_price,
                    question,
                    token_id,
                    team,
                    self.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 兜底失败绝不影响主流程
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                self._note_once(position_id, "ERROR", f"{ledger} {position_id} {team}：查询异常 {exc}")
                continue
            if price is None:
                self.stats["skipped"] += 1
                self._note_once(
                    position_id,
                    note,
                    f"{ledger} {position_id} {team}：{note}，保持未结算"
                    f"（已挂 {age / 3600:.1f}h）",
                )
                continue
            trader.close_position(position_id, price, source="MARKET_RESOLVE")
            settled_row = {
                "t": time.time(),
                "ledger": ledger,
                "position_id": position_id,
                "game_id": getattr(position, "game_id", None),
                "sport": getattr(position, "sport", None),
                "team": team,
                "token_id": token_id,
                "question": question,
                "exit_price": price,
                "age_hours": round(age / 3600.0, 2),
                "note": note,
            }
            settled.append(settled_row)
            self.stats["settled"] += 1
            self._last_note.pop(position_id, None)
            self._log_event(settled_row)
            logger.info(
                "结算兜底：%s %s %s 按市场结果补结算 exit=%s（%s，已挂 %.1fh）",
                ledger,
                position_id,
                team,
                price,
                note,
                age / 3600.0,
            )
        self.stats["last_run_at"] = time.time()
        return settled

    async def run_forever(self, targets_provider) -> None:
        """独立后台循环：与主 tick 解耦，主循环忙/卡住时也能兜底。"""
        if not self.enabled:
            logger.info("模拟盘结算兜底已关闭（PAPER_SETTLE_RECONCILE_ENABLED=0）")
            return
        self._refresh_questions()
        logger.info(
            "模拟盘结算兜底已启动：每 %.0fs 一轮，开仓满 %.0f 分钟仍未结算就查市场结果 → %s",
            self.interval,
            self.min_age / 60.0,
            self.path,
        )
        while True:
            try:
                await self.tick(targets_provider())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底绝不影响主流程
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("模拟盘结算兜底异常（已忽略）：%s", exc)
            await asyncio.sleep(self.interval)

    # ---------- 面板 ----------

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "min_age_seconds": self.min_age,
            "questions_indexed": len(self._questions),
            "checked": self.stats["checked"],
            "settled": self.stats["settled"],
            "skipped": self.stats["skipped"],
            "errors": self.stats["errors"],
            "last_error": self.stats["last_error"],
            "last_run_at": self.stats["last_run_at"],
            "last_note": self.stats["last_note"],
        }
