# -*- coding: utf-8 -*-
"""Parameter optimizer - grid search for optimal signal thresholds."""

import asyncio
import itertools
import logging
from dataclasses import dataclass

from src.backtest import Backtester
from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class OptParam:
    name: str
    values: list


@dataclass
class OptResult:
    params: dict
    total_trades: int
    win_rate: float
    total_pnl: float
    roi: float


class Optimizer:
    def __init__(self, base_config: Config, proxy: str | None = None):
        self.base_config = base_config
        self.proxy = proxy

    async def grid_search(
        self,
        start_date: str,
        end_date: str,
        param_grid: list[OptParam],
        assumed_entry_price: float = 0.90,
        top_n: int = 10,
    ) -> list[OptResult]:
        param_names = [p.name for p in param_grid]
        param_values = [p.values for p in param_grid]
        combos = list(itertools.product(*param_values))

        logger.info("Grid search: %d combinations over %s", len(combos), param_names)

        bt = Backtester(self.base_config, self.proxy)
        games = await bt.fetch_completed_games(start_date, end_date)
        logger.info("Fetched %d games, caching play data...", len(games))

        all_plays = {}
        for game in games:
            plays = await bt.fetch_game_plays(game.game_id)
            all_plays[game.game_id] = plays
        logger.info("Cached play data for %d games", len(all_plays))

        results = []
        for combo in combos:
            overrides = dict(zip(param_names, combo))
            config = Config()
            for k, v in overrides.items():
                setattr(config, k, v)

            total_trades = 0
            wins = 0
            total_cost = 0.0
            total_pnl = 0.0

            for game in games:
                plays = all_plays.get(game.game_id, [])
                if not plays:
                    continue
                trades = bt.simulate_game(game, plays, config, assumed_entry_price)
                for t in trades:
                    total_trades += 1
                    total_cost += t.cost
                    total_pnl += t.pnl
                    if t.leader_won:
                        wins += 1

            results.append(OptResult(
                params=overrides,
                total_trades=total_trades,
                win_rate=wins / max(total_trades, 1),
                total_pnl=total_pnl,
                roi=total_pnl / max(total_cost, 0.01),
            ))

        await bt.close()
        results.sort(key=lambda r: r.total_pnl, reverse=True)
        return results[:top_n]


def print_optimization_results(results: list[OptResult]):
    print(f"\n{'='*80}")
    print(f"  Parameter Optimization Results (Top {len(results)})")
    print(f"{'='*80}")
    print(f"  {'Rank':>4} {'Params':<50} {'Trades':>6} {'WinRate':>8} {'PnL':>10} {'ROI':>8}")
    print(f"  {'-'*4} {'-'*50} {'-'*6} {'-'*8} {'-'*10} {'-'*8}")
    for i, r in enumerate(results, 1):
        params_str = ", ".join(f"{k}={v}" for k, v in r.params.items())
        print(f"  {i:>4} {params_str:<50} {r.total_trades:>6} {r.win_rate:>7.1%} {r.total_pnl:>+10.2f} {r.roi:>7.1%}")
    print(f"{'='*80}")
