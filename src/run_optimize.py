# -*- coding: utf-8 -*-
"""CLI entry point for parameter optimization."""

import asyncio
import sys

from src.config import load_config
from src.optimize import Optimizer, OptParam, print_optimization_results


async def main():
    if len(sys.argv) < 3:
        print("Usage: python -m src.run_optimize <start_date> <end_date>")
        print("Example: python -m src.run_optimize 20250401 20250415")
        return

    start_date = sys.argv[1]
    end_date = sys.argv[2]
    config = load_config()
    optimizer = Optimizer(config)

    param_grid = [
        OptParam(name="min_score_diff", values=[8, 10, 12, 15, 18]),
        OptParam(name="min_time_remaining_seconds", values=[60, 120, 180, 300]),
        OptParam(name="score_diff_per_minute", values=[0.0, 2.0, 3.0, 5.0]),
    ]

    print(f"Optimizing parameters over {start_date} to {end_date}")
    print(f"Grid: {sum(1 for _ in __import__('itertools').product(*[p.values for p in param_grid]))} combinations")

    results = await optimizer.grid_search(start_date, end_date, param_grid)
    print_optimization_results(results)


if __name__ == "__main__":
    asyncio.run(main())
