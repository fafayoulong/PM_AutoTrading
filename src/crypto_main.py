# -*- coding: utf-8 -*-
"""Crypto price crossing bot - monitors Binance real-time prices vs Polymarket crossing markets."""

import asyncio
import logging
import os
import re

from src.config import load_config
from src.crypto_feed import BinanceFeed
from src.crypto_dashboard import CryptoDashboard
from src.crypto_signal import evaluate_crossing
from src.crypto_trader import CryptoTrader

logger = logging.getLogger(__name__)


class CryptoBot:
    def __init__(self):
        self.config = load_config()
        self.binance = BinanceFeed(self.config)
        self.trader = CryptoTrader(self.config)
        self.dashboard = CryptoDashboard(self.config, self.trader)
        self.crossing_markets: list[dict] = []
        self.last_market_refresh = 0
        self.market_refresh_interval = 60  # refresh every 60s

    async def run(self):
        self.binance.on_price_update(self._on_price)
        self.binance.on_price_update(self._update_btc_price)
        await self.dashboard.start()
        logger.info("CryptoBot started. Budget=%.2f, max_entry=%.3f",
                    self.config.starting_budget, self.config.max_entry_price)

        # Run Binance WS in background, refresh markets periodically
        binance_task = asyncio.create_task(self.binance.start(["btcusdt"]))
        market_task = asyncio.create_task(self._market_refresh_loop())

        try:
            await asyncio.gather(binance_task, market_task)
        except KeyboardInterrupt:
            logger.info("CryptoBot interrupted")
        finally:
            self.trader.save_state()
            await self.binance.stop()
            await self.dashboard.stop()
            self._print_summary()

    async def _update_btc_price(self, price_data):
        self.dashboard.update_btc_price(price_data.price)

    async def _market_refresh_loop(self):
        """Periodically fetch active Polymarket crossing markets."""
        while True:
            try:
                markets = await self.binance.fetch_active_crossing_markets("Bitcoin")
                if markets:
                    self.crossing_markets = markets
                    self.dashboard.update_crossing_markets(markets)
                    logger.info("Refreshed %d crossing markets", len(markets))
            except Exception as exc:
                logger.warning("Market refresh error: %s", exc)
            await asyncio.sleep(self.market_refresh_interval)

    async def _on_price(self, price_data):
        """Called on every Binance price update (~1-2 seconds)."""
        btc_price = price_data.price
        symbol = price_data.symbol

        if not self.crossing_markets:
            return

        now = price_data.timestamp

        for market in self.crossing_markets:
            strike = market.get("strike")
            if strike is None:
                continue

            question = market.get("question", "")
            best_bid = market.get("best_bid", 0)
            best_ask = market.get("best_ask", 1)

            # Skip markets that are already settled (price near 0 or 1)
            if best_bid > 0.98 or best_ask < 0.02:
                continue

            # Determine direction from question
            if "above" in question.lower():
                signal = evaluate_crossing(btc_price, strike, min_distance_pct=0.5)
            elif "dip to" in question.lower():
                # For "dip to" markets, the signal is when price is near the strike from above
                signal = evaluate_crossing(btc_price, strike, min_distance_pct=0.5)
            else:
                continue

            if signal is None:
                continue

            logger.info(
                "CROSSING SIGNAL: %s | crypto=%.0f strike=%.0f dist=%.2f%% dir=%s type=%s | pm_bid=%.3f pm_ask=%.3f",
                question[:60], btc_price, strike, signal.distance_pct, signal.direction,
                signal.signal_type, best_bid, best_ask,
            )

            # Determine entry price: if we think price will be ABOVE strike, buy YES
            if signal.direction == "above":
                entry_price = best_ask
            else:
                entry_price = best_ask  # for "dip to" we buy YES too

            if entry_price > self.config.max_entry_price:
                self.trader.log_signal_rejected(question, "PRICE_TOO_HIGH", signal.direction, btc_price)
                continue

            expected_profit = 1.0 - entry_price
            if expected_profit < self.config.min_expected_profit:
                self.trader.log_signal_rejected(question, "PROFIT_TOO_LOW", signal.direction, btc_price)
                continue

            pos = self.trader.open_position(
                market_question=question,
                strike=strike,
                direction=signal.direction,
                entry_price=entry_price,
                crypto_price=btc_price,
            )

    def _print_summary(self):
        s = self.trader.summary()
        logger.info("=== CryptoBot Summary ===")
        for k, v in s.items():
            logger.info("  %s: %s", k, v)


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/crypto_bot.log", encoding="utf-8"),
        ],
    )


async def main():
    setup_logging()
    bot = CryptoBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
