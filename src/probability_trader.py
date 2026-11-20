# -*- coding: utf-8 -*-
"""Paper-only trading engine for probability candidates."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from src.config import Config
from src.paper_trader import PaperTrader
from src.probability_signal import ProbabilitySignal


class ProbabilityPaperTrader(PaperTrader):
    def __init__(self, config: Config):
        super().__init__(
            config,
            starting_budget=config.probability_paper_budget,
            trades_path=config.probability_trades_path,
            state_path=config.probability_state_path,
            signal_events_path=config.probability_events_path,
        )
        self._last_event_id: Optional[str] = None

    def can_open(self, sport: str = "Unknown") -> bool:
        if not self.config.probability_enabled:
            return False
        sport_positions = [p for p in self.open_positions if p.sport == sport]
        if len(sport_positions) >= self.config.probability_max_open_positions_per_sport:
            return False
        if len(self.open_positions) >= self.config.probability_max_open_positions_total:
            return False
        return self.cash >= self.config.probability_max_position_size

    def open_position(
        self,
        signal: ProbabilitySignal,
        entry_price: float,
        sport: str = "Unknown",
        **kwargs,
    ):
        if not self.config.probability_enabled:
            return None
        # details 必须透传：token_id 在里面，持仓期间盘口采集要用它定位市场
        details = kwargs.get("details") or {}
        return super().open_position(
            signal,
            entry_price,
            sport,
            strategy="PROBABILITY_PAPER",
            estimated_probability=signal.estimated_probability,
            position_size=self.config.probability_max_position_size,
            event_id=self._last_event_id,
            details=details,
        )

    def log_event(
        self,
        signal: ProbabilitySignal,
        reason: str,
        sport: str,
        details: Optional[dict] = None,
    ):
        event = {
            "t": time.time(),
            "event_id": f"PEVT_{time.time_ns()}",
            "beijing_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "game_id": signal.game_id,
            "team": signal.leader_name,
            "entry_price": (details or {}).get("best_ask"),
            "reason": reason,
            "sport": sport,
            "details": {
                "strategy": "probability_paper",
                "estimated_probability": signal.estimated_probability,
                **(details or {}),
            },
        }
        self._last_event_id = event["event_id"]
        self.signal_log.append(event)
        self._append_signal_event(event)
        self.save_state()
