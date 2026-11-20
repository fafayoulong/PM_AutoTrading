# -*- coding: utf-8 -*-
"""CLI entry point for backtesting."""

import asyncio
import sys

from src.backtest import Backtester, print_backtest_results
from src.config import load_config


async def main():
    if len(sys.argv) < 3:
        print("Usage: python -m src.run_backtest <start_date> <end_date>")
        print("Example: python -m src.run_backtest 20250401 20250415")
        return

    start_date = sys.argv[1]
    end_date = sys.argv[2]
    config = load_config()
    bt = Backtester(config)

    print(f"Running backtest: {start_date} to {end_date}")
    print(f"Config: min_diff={config.min_score_diff}, "
          f"min_clock={config.min_time_remaining_seconds}s, "
          f"diff_per_min={config.score_diff_per_minute}")

    result = await bt.run(start_date, end_date, config)
    await bt.close()
    print_backtest_results(result, f"({start_date} - {end_date})")


if __name__ == "__main__":
    asyncio.run(main())
