# -*- coding: utf-8 -*-
"""Paper trading engine for crypto crossing markets."""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class CryptoPosition:
    position_id: str
    symbol: str
    market_question: str
    strike_price: float
    direction: str  # "above" or "below" (the outcome we bought)
    entry_price: float  # Polymarket price at entry
    entry_crypto_price: float  # Binance price at entry
    size: float  # shares
    cost: float  # USDC
    entry_time: float
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "OPEN"


class CryptoTrader:
    def __init__(self, config: Config):
        self.config = config
        self.cash = config.starting_budget
        self.positions: dict[str, CryptoPosition] = {}
        self.signal_log: list[dict] = []
        self._counter = 0
        self.state_path = "trades/crypto_state.json"
        self._ensure_dirs()
        self.load_state()

    def _ensure_dirs(self):
        os.makedirs("trades", exist_ok=True)

    @property
    def open_positions(self) -> list[CryptoPosition]:
        return [p for p in self.positions.values() if p.status == "OPEN"]

    def can_open(self) -> bool:
        return len(self.open_positions) < self.config.max_open_positions and self.cash >= self.config.max_position_size

    def open_position(
        self,
        market_question: str,
        strike: float,
        direction: str,
        entry_price: float,
        crypto_price: float,
    ) -> Optional[CryptoPosition]:
        if not self.can_open():
            return None

        self._counter += 1
        pos_id = f"CR_{int(time.time())}_{self._counter}"
        size = self.config.max_position_size / entry_price

        pos = CryptoPosition(
            position_id=pos_id,
            symbol="BTCUSDT",
            market_question=market_question,
            strike_price=strike,
            direction=direction,
            entry_price=entry_price,
            entry_crypto_price=crypto_price,
            size=size,
            cost=self.config.max_position_size,
            entry_time=time.time(),
        )
        self.positions[pos_id] = pos
        self.cash -= self.config.max_position_size
        self._log_signal(market_question, entry_price, "OPENED", direction, crypto_price)
        self.save_state()
        logger.info("CRYPTO OPENED: %s @ %.3f (crypto=%.0f, strike=%.0f, dir=%s)",
                    market_question[:50], entry_price, crypto_price, strike, direction)
        return pos

    def close_position(self, position_id: str, exit_price: float):
        pos = self.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            return
        pos.exit_price = exit_price
        pos.exit_time = time.time()
        pos.pnl = pos.size * exit_price - pos.cost
        pos.status = "CLOSED_WIN" if pos.pnl > 0 else "CLOSED_LOSS"
        self.cash += pos.size * exit_price
        self.save_state()
        logger.info("CRYPTO CLOSED: %s @ %.3f, PnL=%.2f", position_id, exit_price, pos.pnl)

    def log_signal_rejected(self, market_question: str, reason: str, direction: str, crypto_price: float):
        self._log_signal(market_question, None, reason, direction, crypto_price)

    def _log_signal(self, market_question: str, entry_price: Optional[float], reason: str, direction: str, crypto_price: float):
        self.signal_log.append({
            "t": time.time(),
            "market": market_question[:80],
            "entry_price": entry_price,
            "reason": reason,
            "direction": direction,
            "crypto_price": crypto_price,
        })

    def summary(self) -> dict:
        all_positions = list(self.positions.values())
        closed = [p for p in all_positions if p.status.startswith("CLOSED")]
        total_pnl = sum(p.pnl or 0 for p in closed)
        wins = sum(1 for p in closed if p.pnl and p.pnl > 0)
        return {
            "cash": round(self.cash, 2),
            "open_positions": len(all_positions) - len(closed),
            "total_trades": len(all_positions),
            "closed_trades": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / max(len(closed), 1), 3),
            "total_pnl": round(total_pnl, 2),
        }

    def load_state(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.cash = state.get("cash", self.config.starting_budget)
            self._counter = state.get("counter", 0)
            for pos_id, p in state.get("positions", {}).items():
                self.positions[pos_id] = CryptoPosition(**p)
            self.signal_log = state.get("signal_log", [])
            logger.info("Crypto state loaded: %d positions, %d signals", len(self.positions), len(self.signal_log))
        except Exception as exc:
            logger.warning("Failed to load crypto state: %s", exc)

    def save_state(self):
        state = {
            "cash": self.cash,
            "counter": self._counter,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "signal_log": self.signal_log,
            "saved_at": time.time(),
        }
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("Failed to save crypto state: %s", exc)
