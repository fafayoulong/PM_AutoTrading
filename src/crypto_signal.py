# -*- coding: utf-8 -*-
"""Signal engine for crypto price crossing detection."""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CrossingSignal:
    symbol: str  # "BTCUSDT"
    asset: str  # "Bitcoin"
    current_price: float
    strike_price: float
    direction: str  # "above" or "below"
    distance_pct: float  # how close to strike, as percentage
    signal_type: str  # "approaching" or "crossed"
    timestamp: float


def evaluate_crossing(
    current_price: float,
    strike_price: float,
    min_distance_pct: float = 0.5,
) -> CrossingSignal | None:
    """Check if current price is approaching or has crossed a strike level.

    Args:
        current_price: Real-time price from Binance
        strike_price: The threshold price from Polymarket market
        min_distance_pct: Signal fires when within this % of the strike

    Returns:
        CrossingSignal if price is near/crossed strike, None otherwise
    """
    if strike_price <= 0 or current_price <= 0:
        return None

    distance_pct = abs(current_price - strike_price) / strike_price * 100

    if distance_pct > min_distance_pct:
        return None

    if current_price >= strike_price:
        direction = "above"
    else:
        direction = "below"

    signal_type = "crossed" if distance_pct < 0.01 else "approaching"

    import time
    return CrossingSignal(
        symbol="BTCUSDT",
        asset="Bitcoin",
        current_price=current_price,
        strike_price=strike_price,
        direction=direction,
        distance_pct=distance_pct,
        signal_type=signal_type,
        timestamp=time.time(),
    )
