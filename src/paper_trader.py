# -*- coding: utf-8 -*-
"""Paper trading engine - tracks virtual positions and P&L."""

import json
import logging
import os
from pathlib import Path
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from src.config import Config
from src.entry_price import buffered_entry_price, tick_size_of
from src.signal import LockSignal
from src.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    position_id: str
    game_id: str
    market_id: str
    team: str
    side: str  # "YES" or "NO"
    entry_price: float
    size: float  # shares
    cost: float  # USDC spent
    entry_time: float
    # 入场那一刻盘口的**卖一价**。entry_price 记的是"实盘会成交在哪个价"
    # （卖一 + live_entry_buffer_ticks，见 src/entry_price.py），两者差一个 tick。
    # 没有这个字段就分不清"模拟盘便宜"是记账口径还是市场变化，2026-09-28 补。
    ask_at_entry: Optional[float] = None
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "OPEN"  # "OPEN", "CLOSED_WIN", "CLOSED_LOSS", "CLOSED_SETTLED"
    sport: str = "Unknown"
    strategy: str = "SWEEP_LOCK"
    estimated_probability: Optional[float] = None
    event_id: Optional[str] = None
    # CLOB token id：持仓期间盘口采集（src/position_paths.py）要靠它定位市场。
    # 旧仓位没有这个字段，load_state 时会尝试从 signal_events.jsonl 回填。
    token_id: Optional[str] = None
    # 结算来源（2026-09-20 加）：账本上有三条结算路径，出问题时必须能一眼看出
    # 是哪条结的 —— "SCORE_FEED"（比分流，正常路径）、"MARKET_RESOLVE"（市场对账
    # 兜底，见 src/settlement_reconcile.py）、"RECALC"（重启时 tmp/recalc_paper_cash.py
    # 补录）。旧仓位没有该字段 → 面板显示为「旧记录」。
    settle_source: Optional[str] = None


class PaperTrader:
    def __init__(
        self,
        config: Config,
        *,
        starting_budget: Optional[float] = None,
        trades_path: Optional[str] = None,
        state_path: Optional[str] = None,
        signal_events_path: Optional[str] = None,
    ):
        self.config = config
        self.starting_budget = starting_budget if starting_budget is not None else config.starting_budget
        self.trades_path = trades_path or config.trades_path
        self.state_path = state_path or config.state_path
        self.signal_events_path = signal_events_path or config.signal_events_path
        self.cash = self.starting_budget
        self.positions: dict[str, PaperPosition] = {}
        self.signal_log: list[dict] = []
        self.equity_history: list[dict] = []
        self._signal_events_path = Path(self.signal_events_path)
        self._counter = 0
        self._ensure_dirs()
        self.load_state()

    def _ensure_dirs(self):
        os.makedirs(os.path.dirname(self.trades_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(self.config.log_path) or ".", exist_ok=True)

    def load_state(self):
        """Load previous session state from state.json if it exists."""
        if not os.path.exists(self.state_path):
            logger.info("No previous state file found, starting fresh")
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            self.cash = state.get("cash", self.starting_budget)
            self._counter = state.get("counter", 0)

            positions_data = state.get("positions", {})
            # 只取当前 dataclass 认识的字段：这样 state.json 里多出「更新的版本写入的
            # 字段」时（例如回滚代码后遇到 token_id），旧代码不会因为 TypeError 整份放弃
            # 账本 —— load_state 的兜底是「重新开始」，静默丢账比缺字段危险得多。
            known_fields = set(PaperPosition.__dataclass_fields__)
            for pos_id, p in positions_data.items():
                self.positions[pos_id] = PaperPosition(
                    **{key: value for key, value in p.items() if key in known_fields}
                )

            self.signal_log = state.get("signal_log", [])
            self.equity_history = state.get("equity_history", [])
            # state 里的信号日志只是「最近若干条」的快照（完整历史在 signal_events.jsonl），
            # 权益点也只在内存里保留最近若干点，避免 state.json 无上限膨胀
            self._trim_history()
            self._warn_if_ledger_rolled_back()
            self._backfill_token_ids()
            self._resync_trades_log()

            logger.info(
                "Loaded state: cash=%.2f, %d positions, %d signals, %d equity points",
                self.cash, len(self.positions), len(self.signal_log), len(self.equity_history)
            )
        except Exception as exc:
            logger.warning("Failed to load state: %s, starting fresh", exc)

    def _backfill_token_ids(self) -> int:
        """给缺 token_id 的仓位回填（老仓位的 token 只在 signal_events 里）。

        只填不覆盖：已有 token_id 的仓位保持原值。signal_events.jsonl 是可滚动的
        追加日志，找不到就留空 —— 采集层会跳过并把数量报给面板。
        """
        missing = [p for p in self.positions.values() if not p.token_id]
        if not missing:
            return 0
        wanted = {p.event_id for p in missing if p.event_id}
        if not wanted:
            return 0
        mapping: dict[str, str] = {}
        try:
            with self._signal_events_path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_id = str(event.get("event_id") or "")
                    if event_id not in wanted:
                        continue
                    token = str((event.get("details") or {}).get("token_id") or "")
                    if token:
                        mapping[event_id] = token
        except FileNotFoundError:
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("回填 token_id 失败：%s", exc)
            return 0
        filled = 0
        for position in missing:
            token = mapping.get(str(position.event_id or ""))
            if token:
                position.token_id = token
                filled += 1
        if filled:
            logger.info("已从 signal_events 回填 %d 个持仓的 token_id", filled)
        return filled

    def _trim_history(self):
        signal_limit = max(0, int(getattr(self.config, "paper_signal_log_limit", 500)))
        equity_limit = max(0, int(getattr(self.config, "paper_equity_history_limit", 5000)))
        if signal_limit and len(self.signal_log) > signal_limit:
            self.signal_log = self.signal_log[-signal_limit:]
        if equity_limit and len(self.equity_history) > equity_limit:
            self.equity_history = self.equity_history[-equity_limit:]

    def _resync_trades_log(self) -> int:
        """把「state 里已结算、日志里最后一行还是 OPEN」的仓位补一条结算行。

        为什么需要（2026-09-28 踩到）
        ----------------------------
        `trades/paper_trades.jsonl` 是**追加日志**：开仓追加一条 OPEN、结算追加一条
        CLOSED。但账务修复工具（`tmp/recalc_paper_cash.py` / `fix_settlement_prices.py`）
        只写 `state.json`，于是日志里会长期留下"最后一行仍是 OPEN"的仓位。
        只有 state 才知道真相（`state.json` 的持仓 ∪ 日志，state 优先 —— 见重算脚本），
        但所有**分析脚本**都在读日志，于是凭空多出"永远没结算"的仓位：
        实测 13 个仓位（8 笔实际已结算）在日志里是 OPEN，日志口径 ROI +2.40%
        而 state 口径是 +2.12% —— 一度让我把一个健康的账本误判成"CS2 亏损没入账"。

        这里在启动时用 state（权威）自愈日志：只补"缺的那一条 CLOSED 行"，
        不改任何已有行、不动现金。幂等：补过之后最后一行为 CLOSED，不再重复补。
        """
        try:
            path = Path(self.trades_path)
            if not path.exists():
                return 0
            last_status: dict[str, str] = {}
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pos_id = str(row.get("position_id") or "")
                    if pos_id:
                        last_status[pos_id] = str(row.get("status") or "")
            repaired = 0
            for pos_id, position in self.positions.items():
                if not str(position.status or "").startswith("CLOSED"):
                    continue
                if str(last_status.get(pos_id) or "").startswith("CLOSED"):
                    continue
                self._append_trade(position)
                repaired += 1
            if repaired:
                logger.info(
                    "模拟盘日志自愈：补记 %d 笔「state 已结算、日志仍 OPEN」的仓位 → %s",
                    repaired,
                    path,
                )
            return repaired
        except Exception as exc:  # noqa: BLE001 - 自愈失败不能拖住启动
            logger.debug("模拟盘日志自愈失败：%s", exc)
            return 0

    def _warn_if_ledger_rolled_back(self):
        """检测账本是否被旧进程写回（曾发生过：旧进程退出时覆盖重算结果）。

        只告警、不自动改账：账务重算需要用户确认后运行 tmp/recalc_paper_cash.py。
        """
        try:
            from pathlib import Path

            excluded_path = Path("trades/paper_recalc_excluded.json")
            if not excluded_path.exists():
                return
            excluded = set(json.loads(excluded_path.read_text(encoding="utf-8")) or [])
            rolled_back = excluded & set(self.positions)
            if rolled_back:
                logger.warning(
                    "账本疑似被旧进程覆盖（发现 %d 笔已作废的重复记录）。"
                    "如需恢复重算结果，请运行：python tmp/recalc_paper_cash.py --apply",
                    len(rolled_back),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("账本回滚检查失败：%s", exc)

    def save_state(self):
        """Persist current state to state.json."""
        self._trim_history()
        self._check_ledger_invariant()
        state = {
            "cash": self.cash,
            "counter": self._counter,
            "positions": {pos_id: asdict(pos) for pos_id, pos in self.positions.items()},
            "signal_log": self.signal_log,
            "equity_history": self.equity_history,
            "saved_at": time.time(),
        }
        try:
            # 原子写：避免写到一半崩溃导致账本损坏
            atomic_write_json(self.state_path, state)
        except Exception as exc:
            logger.warning("Failed to save state: %s", exc)

    def _check_ledger_invariant(self) -> bool:
        """校验「现金 = 起始资金 + Σ已结算盈亏 − 未结算成本」。

        这条恒等式必须永远成立：开仓扣成本、结算收赔付，两边同步变化。
        一旦不成立，说明有记录在现金没动的情况下被增删（历史上出现过
        "手工补结算/导入"导致的口径漂移，每次校准都要凭空吸收几百美元）。
        这里只告警不改账，把问题在发生的那一刻钉在日志里。
        """
        closed = [p for p in self.positions.values() if p.status.startswith("CLOSED")]
        settled_pnl = sum(p.pnl or 0.0 for p in closed)
        open_cost = sum(p.cost for p in self.open_positions)
        expected = self.starting_budget + settled_pnl - open_cost
        drift = self.cash - expected
        if abs(drift) > 0.5:
            logger.warning(
                "模拟盘账本口径漂移：现金 %.2f 与恒等式应得 %.2f 相差 %+.2f"
                "（已结算 %d 笔，未结算 %d 笔占成本 %.2f）——"
                "如确认无误可用 tmp/recalc_paper_cash.py --apply 校准",
                self.cash, expected, drift, len(closed), len(self.open_positions), open_cost,
            )
            return False
        return True

    @property
    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions.values() if p.status == "OPEN"]

    def can_open(self, sport: str = "Unknown") -> bool:
        rules = self.config.sport_rules.get(sport)
        if not rules or not rules.enabled:
            return False
        sport_positions = [p for p in self.open_positions if p.sport == sport]
        return len(sport_positions) < rules.max_open_positions and self.cash >= rules.max_position_size

    def open_position(
        self,
        signal: LockSignal,
        entry_price: float,
        sport: str = "Unknown",
        *,
        strategy: str = "SWEEP_LOCK",
        estimated_probability: Optional[float] = None,
        position_size: Optional[float] = None,
        event_id: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> Optional[PaperPosition]:
        rules = self.config.sport_rules.get(sport)
        if not rules or not rules.enabled:
            self.log_signal_rejected(signal, "SPORT_DISABLED", sport)
            return None
        # 记账价 = **实盘会成交在哪个价**，不是盘口的卖一价（2026-09-28）。
        # 实盘下单是在卖一之上加 live_entry_buffer_ticks 个 tick 才能吃到
        # （见 src/entry_price.py），而这里过去直接记卖一价 → 同一套信号
        # 模拟盘永远比实盘便宜 1 个 tick（实测 107/107 匹配单，中位 +0.0100），
        # 使模拟盘 ROI 系统性高估约 1.1pp，"实盘亏模拟盘赚"有一半是这么来的。
        raw_ask = float(entry_price)
        tick = tick_size_of((details or {}).get("tick_size"))
        fill_price = buffered_entry_price(
            raw_ask,
            tick,
            int(getattr(rules, "live_entry_buffer_ticks", 0) or 0),
            float(getattr(rules, "min_expected_profit", 0.0) or 0.0),
        )
        entry_price = fill_price
        # 把**实时的**入场价也写进详情（2026-09-18）：详情里的 best_ask 来自预匹配的
        # Gamma 快照（TTL 30 分钟），而成交用的是实时盘口价，两者差 0.15~0.20 会被
        # 误读成"滑点太大"（实测 #441 快照 0.74 / 实时 0.89，成交价就是 0.89）。
        if details is not None:
            details = {
                **details,
                "live_entry_price": raw_ask,
                "expected_fill_price": fill_price,
                "entry_buffer_ticks": int(getattr(rules, "live_entry_buffer_ticks", 0) or 0),
                "tick_size": tick,
            }
        if any(position.game_id == signal.game_id for position in self.positions.values()):
            self.log_signal_rejected(signal, "DUPLICATE_GAME", sport)
            return None
        # 同一**市场**也只做一次：同一场比赛会以两个 game_id 出现（PM 实时流 6 位 id
        # vs ESPN 9 位 id），只比 game_id 会漏，于是同一 token 建两笔仓
        # （2026-09-16 实测 HOU；09-12 SD 亦然）。
        incoming_token = str((details or {}).get("token_id") or "")
        if incoming_token and any(
            str(getattr(position, "token_id", "") or "") == incoming_token
            for position in self.positions.values()
        ):
            self.log_signal_rejected(signal, "DUPLICATE_GAME", sport)
            return None
        if not self.can_open(sport):
            logger.info("Cannot open position: budget or position limit reached")
            return None

        self._counter += 1
        pos_id = f"POS_{int(time.time())}_{self._counter}"
        event_id = event_id or f"SGN_{time.time_ns()}"
        position_size = position_size if position_size is not None else rules.max_position_size
        size = position_size / entry_price  # shares bought
        cost = position_size
        # token_id 由调用方通过 details 传入（与下单用的是同一个盘口标的）；
        # 采集层据此在持仓期间抓 best_bid/best_ask。
        token_id = str((details or {}).get("token_id") or "") or None

        pos = PaperPosition(
            position_id=pos_id,
            game_id=signal.game_id,
            market_id="",
            team=getattr(signal, "leader_team", None) or signal.leader_name,
            side="YES",
            entry_price=entry_price,
            size=size,
            cost=cost,
            entry_time=time.time(),
            ask_at_entry=raw_ask,
            sport=sport,
            strategy=strategy,
            estimated_probability=estimated_probability,
            event_id=event_id,
            token_id=token_id,
        )
        self.positions[pos_id] = pos
        self.cash -= cost
        self._append_trade(pos)
        self._log_signal(signal, entry_price, "OPENED", sport=sport, details=details, event_id=event_id)
        logger.info("OPENED %s: %s YES @ %.3f, size=%.1f shares, cost=%.2f",
                    pos_id, getattr(signal, "leader_team", None) or signal.leader_name, entry_price, size, cost)
        return pos

    def log_signal_rejected(
        self,
        signal: LockSignal,
        reason: str,
        sport: str = "Unknown",
        details: Optional[dict] = None,
    ):
        self._log_signal(signal, None, reason, sport, details)

    def close_position(self, position_id: str, exit_price: float, *, source: str = "SCORE_FEED"):
        pos = self.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            return
        pos.exit_price = exit_price
        pos.exit_time = time.time()
        pos.pnl = pos.size * exit_price - pos.cost
        pos.settle_source = source
        if exit_price >= 0.99:
            pos.status = "CLOSED_WIN"
        elif exit_price <= 0.01:
            pos.status = "CLOSED_LOSS"
        else:
            pos.status = "CLOSED_SETTLED"
        self.cash += pos.size * exit_price
        self._append_trade(pos)
        self._record_equity()
        logger.info("CLOSED %s @ %.3f, PnL=%.2f（结算来源 %s）", position_id, exit_price, pos.pnl, source)

    def _record_equity(self):
        s = self.summary()
        self.equity_history.append({"t": time.time(), "equity": s["equity"], "pnl": s["total_pnl"]})
        self.save_state()

    def summary(self) -> dict:
        all_positions = list(self.positions.values())
        closed = [p for p in all_positions if p.status.startswith("CLOSED")]
        open_count = len(all_positions) - len(closed)
        open_cost = sum(p.size * (p.exit_price or p.entry_price) for p in self.open_positions)
        equity = round(self.cash + open_cost, 2)
        # 盈亏与净值必须同源：都从 cash 推导。
        # 历史上「盈亏=已结算记录求和」在清理/补录后会与 cash 脱节
        # （记录被删了 cash 没回滚），首页就会出现净值 +1753 / 盈亏 -1247 这种自相矛盾。
        total_pnl = round(equity - self.starting_budget, 2)
        settled_pnl = round(sum(p.pnl or 0 for p in closed), 2)
        wins = sum(1 for p in closed if p.pnl and p.pnl > 0)
        return {
            "cash": round(self.cash, 2),
            "equity": equity,
            "open_positions": open_count,
            "total_trades": len(all_positions),
            "closed_trades": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / max(len(closed), 1), 3),
            "total_pnl": total_pnl,
            "settled_pnl": settled_pnl,
            "starting_budget": self.starting_budget,
        }

    def _append_trade(self, pos: PaperPosition):
        line = json.dumps(asdict(pos), ensure_ascii=False)
        with open(self.trades_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _log_signal(
        self,
        signal: LockSignal,
        entry_price: Optional[float],
        reason: str,
        sport: str = "NBA",
        details: Optional[dict] = None,
        event_id: Optional[str] = None,
    ):
        # 把**这一刻生效的门槛**固化进事件（2026-09-29）：门槛会变（网球地板
        # 0.86→0.60、入场价区间闸门开关…），而面板是按事件渲染的 —— 不记下来的话
        # 历史日志就只能靠"当前设置"去解释，出现"价格 0.84 却写着门槛≥0.86 还通过"
        # 这种自相矛盾的显示（用户 2026-09-29 报的就是这个）。
        # 调用方已经给了同名字段就不覆盖（例如足球会把 win_probability 写进来）。
        details = dict(details or {})
        try:
            rules = self.config.sport_rules.get(sport)
            if rules is not None:
                details.setdefault("thresholds", {
                    "live_min_entry_price": float(getattr(rules, "live_min_entry_price", 0.0) or 0.0),
                    "max_entry_price": float(getattr(rules, "max_entry_price", 0.0) or 0.0),
                    "min_expected_profit": float(getattr(rules, "min_expected_profit", 0.0) or 0.0),
                    "live_entry_buffer_ticks": int(getattr(rules, "live_entry_buffer_ticks", 0) or 0),
                    "entry_band_enabled": bool(getattr(self.config, "entry_band_enabled", False)),
                    "entry_band_min": float(getattr(self.config, "entry_band_min", 0.0) or 0.0),
                    "entry_band_max": float(getattr(self.config, "entry_band_max", 1.0) or 1.0),
                })
        except Exception as exc:  # noqa: BLE001 - 记录门槛失败不能拖累下单
            logger.debug("记录门槛快照失败：%s", exc)
        event = {
            "t": time.time(),
            "event_id": event_id or f"SGN_{time.time_ns()}",
            "beijing_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "game_id": signal.game_id,
            "team": getattr(signal, "leader_team", None) or signal.leader_name,
            "entry_price": entry_price,
            "reason": reason,
            "sport": sport,
            "details": details,
        }
        self.signal_log.append(event)
        self._append_signal_event(event)
        self.save_state()

    def _append_signal_event(self, event: dict):
        try:
            self._signal_events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._signal_events_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to append signal event: %s", exc)

    def stats_by_sport(self) -> dict:
        """按运动统计已结算仓位：笔数 / 胜场 / 盈亏 / **成本** / ROI。

        ROI 的分母用**实际成本求和**（`cost` 来自每笔建仓时的 position_size），
        不用"笔数 × 固定金额" —— 早期限额与策略（概率账本 $50/笔、主账本 $500/笔）
        都改过，拍脑袋乘出来的分母会让历史行情的 ROI 失真。
        没有成本的运动返回 roi=None（面板显示「—」），不返回 0 —— 0 会被读成"打平"。
        """
        sport_map = {}
        for s in self.signal_log:
            sport_map[s["game_id"]] = s.get("sport", "Unknown")
        stats = {}
        closed = [p for p in self.positions.values() if p.status.startswith("CLOSED")]
        for p in closed:
            # 持仓自带 sport；只有旧记录缺字段时才回退到信号日志映射
            # （日志会被滚动淘汰，否则历史交易会被算成 Unknown）
            sport = p.sport if p.sport and p.sport != "Unknown" else (
                sport_map.get(p.game_id) or "Unknown"
            )
            if sport not in stats:
                stats[sport] = {"trades": 0, "wins": 0, "pnl": 0.0, "cost": 0.0}
            stats[sport]["trades"] += 1
            if p.pnl and p.pnl > 0:
                stats[sport]["wins"] += 1
            stats[sport]["pnl"] = round(stats[sport]["pnl"] + (p.pnl or 0), 2)
            stats[sport]["cost"] = round(stats[sport]["cost"] + float(p.cost or 0.0), 2)
        for item in stats.values():
            item["roi"] = round(item["pnl"] / item["cost"], 4) if item["cost"] > 0 else None
        return stats

    def settlement_sources(self) -> dict:
        """已结算仓位按结算来源计数（面板上标出来，好判断哪条路径在干活）。

        2026-09-20：模拟盘原来只有比分流一条结算路径，CS2 1681114 的终局消息
        丢在 WS 重连里，仓位 39 小时挂着 OPEN，账面就成了"从不输"。有了来源
        计数，这类问题在面板上能立刻看见。
        """
        counts: dict[str, int] = {}
        for p in self.positions.values():
            if not p.status.startswith("CLOSED"):
                continue
            key = p.settle_source or "UNKNOWN"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def stale_open_positions(self, max_age_seconds: float) -> list[dict]:
        """超过 `max_age_seconds` 仍未结算的仓位（正常比赛早就该结完）。

        这类仓位要么是比分流漏了终局，要么是市场还没结算 —— 两种都该在面板上
        显式提示，而不是混在"持仓"里看起来像正常在跑。
        """
        now = time.time()
        stale = []
        for p in self.open_positions:
            age = now - float(p.entry_time or 0)
            if age < max_age_seconds:
                continue
            stale.append({
                "position_id": p.position_id,
                "game_id": p.game_id,
                "sport": p.sport,
                "team": p.team,
                "entry_time": p.entry_time,
                "age_hours": round(age / 3600.0, 2),
                "cost": p.cost,
            })
        stale.sort(key=lambda row: row["age_hours"], reverse=True)
        return stale

    def settle_game(
        self,
        game_id: str,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        *,
        allow_draw: bool = False,
    ):
        """按最终比分结算。

        allow_draw：足球等「平局是真实结果」的运动要传 True —— 平局时两边都不是赢家
        （Polymarket 的「Will <队> win」是二元盘，平局时该盘归零）。
        不传就沿用原逻辑（平局判给客队），因为篮球/棒球/橄榄球不会以平局结束。
        """
        if allow_draw and home_score == away_score:
            for pos in self.open_positions:
                if pos.game_id == game_id:
                    self.close_position(pos.position_id, 0.0)
            return
        winner = home_team if home_score > away_score else away_team
        for pos in self.open_positions:
            if pos.game_id != game_id:
                continue
            if pos.team == winner:
                self.close_position(pos.position_id, 1.0)
            else:
                self.close_position(pos.position_id, 0.0)
