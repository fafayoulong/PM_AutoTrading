# -*- coding: utf-8 -*-
"""Historical backtest for crypto price-crossing markets."""

import argparse
import asyncio
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from src.config import Config


@dataclass
class BacktestTrade:
    date: str
    market_kind: str
    strike: float
    entry_time: str
    entry_price: float
    entry_crypto_price: float
    size: float
    cost: float
    exit_time: str
    exit_price: float
    pnl: float
    result: str
    bars_held: int


@dataclass
class BacktestResult:
    symbol: str
    start_date: str
    end_date: str
    market_kinds: list[str]
    strikes: list[float]
    entry_price: float
    trigger_distance_pct: float
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    total_pnl: float
    roi: float
    max_drawdown: float
    final_equity: float
    trades: list[BacktestTrade]


class CryptoBacktester:
    BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

    def __init__(self, config: Config):
        self.config = config

    async def _fetch_day_klines(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        day_start: datetime,
    ) -> list[dict]:
        day_end = day_start + timedelta(days=1)
        rows = []
        cursor = day_start
        while cursor < day_end:
            params = {
                "symbol": symbol,
                "interval": "1m",
                "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(day_end.timestamp() * 1000) - 1,
                "limit": 1000,
            }
            async with session.get(
                self.BINANCE_KLINES_URL,
                params=params,
                proxy=self.config.proxy_url,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise RuntimeError(f"Binance API {response.status}: {text[:200]}")
                data = await response.json()

            if not data:
                break
            for row in data:
                open_time = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
                if open_time >= day_end:
                    continue
                rows.append({
                    "time": open_time,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })

            cursor = datetime.fromtimestamp(data[-1][0] / 1000, tz=timezone.utc) + timedelta(minutes=1)
            if len(data) < 1000:
                break

        return rows

    def _triggered(self, market_kind: str, bar: dict, strike: float) -> bool:
        if market_kind == "dip":
            return bar["close"] > strike and (bar["close"] - strike) / strike * 100 <= self._distance
        if market_kind == "above":
            return bar["close"] > strike and (bar["close"] - strike) / strike * 100 <= self._distance
        return False

    def _resolved(self, market_kind: str, bars: list[dict], strike: float) -> tuple[bool, int, Optional[dict]]:
        if market_kind == "dip":
            for index, bar in enumerate(bars):
                if bar["low"] <= strike:
                    return True, index, bar
            return False, len(bars) - 1, bars[-1]
        if market_kind == "above":
            final = bars[-1]
            return final["close"] > strike, len(bars) - 1, final
        return False, len(bars) - 1, bars[-1]

    async def run(
        self,
        start_date: str,
        end_date: str,
        strikes: list[float],
        market_kinds: list[str],
        entry_price: float,
        trigger_distance_pct: float,
        symbol: str = "BTCUSDT",
    ) -> BacktestResult:
        if not 0 < entry_price < 1:
            raise ValueError("entry_price must be between 0 and 1")
        if trigger_distance_pct <= 0:
            raise ValueError("trigger_distance_pct must be positive")

        self._distance = trigger_distance_pct
        start = datetime.strptime(start_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        if end < start:
            raise ValueError("end_date must not be before start_date")

        trades: list[BacktestTrade] = []
        equity_curve = [self.config.starting_budget]
        cash = self.config.starting_budget
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            current = start
            while current <= end:
                bars = await self._fetch_day_klines(session, symbol.upper(), current)
                if bars:
                    day_trades = self._simulate_day(
                        current.strftime("%Y%m%d"), bars, strikes, market_kinds, entry_price
                    )
                    for trade in day_trades:
                        if trade.cost <= cash:
                            cash -= trade.cost
                            cash += trade.cost + trade.pnl
                            equity_curve.append(cash)
                            trades.append(trade)
                current += timedelta(days=1)

        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = len(trades) - wins
        total_pnl = sum(trade.pnl for trade in trades)
        peak = equity_curve[0]
        max_drawdown = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

        return BacktestResult(
            symbol=symbol.upper(),
            start_date=start_date,
            end_date=end_date,
            market_kinds=market_kinds,
            strikes=strikes,
            entry_price=entry_price,
            trigger_distance_pct=trigger_distance_pct,
            trade_count=len(trades),
            win_count=wins,
            loss_count=losses,
            win_rate=wins / len(trades) if trades else 0,
            total_pnl=total_pnl,
            roi=total_pnl / self.config.starting_budget,
            max_drawdown=max_drawdown,
            final_equity=cash,
            trades=trades,
        )

    def _simulate_day(
        self,
        date_key: str,
        bars: list[dict],
        strikes: list[float],
        market_kinds: list[str],
        entry_price: float,
    ) -> list[BacktestTrade]:
        trades = []
        size_per_trade = self.config.max_position_size / entry_price

        for market_kind in market_kinds:
            for strike in strikes:
                entry_index = None
                if market_kind == "dip":
                    touch_index = next(
                        (index for index, bar in enumerate(bars) if bar["low"] <= strike),
                        None,
                    )
                    if touch_index is None:
                        continue
                    for index in range(touch_index):
                        if self._triggered(market_kind, bars[index], strike):
                            entry_index = index
                            break
                else:
                    for index, bar in enumerate(bars):
                        if self._triggered(market_kind, bar, strike):
                            entry_index = index
                            break

                if entry_index is None:
                    continue

                win, exit_index, exit_bar = self._resolved(market_kind, bars, strike)
                exit_price = 1.0 if win else 0.0
                cost = self.config.max_position_size
                pnl = size_per_trade * exit_price - cost
                result = "WIN" if win else "LOSS"
                trades.append(BacktestTrade(
                    date=date_key,
                    market_kind=market_kind,
                    strike=strike,
                    entry_time=bars[entry_index]["time"].isoformat(),
                    entry_price=entry_price,
                    entry_crypto_price=bars[entry_index]["close"],
                    size=size_per_trade,
                    cost=cost,
                    exit_time=exit_bar["time"].isoformat(),
                    exit_price=exit_price,
                    pnl=pnl,
                    result=result,
                    bars_held=exit_index - entry_index,
                ))

        return trades


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def print_result(result: BacktestResult) -> None:
    print("=" * 56)
    print("Crypto 穿越盘回测结果")
    print("=" * 56)
    print(f"区间: {result.start_date} - {result.end_date}")
    print(f"标的: {result.symbol}")
    print(f"市场类型: {', '.join(result.market_kinds)}")
    print(f"Strike: {', '.join(str(value) for value in result.strikes)}")
    print(f"触发距离: {result.trigger_distance_pct}%")
    print(f"假设入场价: {result.entry_price:.3f}")
    print("-" * 56)
    print(f"交易数: {result.trade_count}")
    print(f"胜/负: {result.win_count}/{result.loss_count}")
    print(f"胜率: {result.win_rate:.1%}")
    print(f"总收益: {result.total_pnl:+.2f} USDC")
    print(f"ROI: {result.roi:.2%}")
    print(f"最大回撤: {result.max_drawdown:.2%}")
    print(f"期末资金: {result.final_equity:.2f} USDC")
    print("-" * 56)
    if result.trades:
        print("最近 10 笔:")
        for trade in result.trades[-10:]:
            print(
                f"{trade.date} {trade.market_kind:<5} strike={trade.strike:.0f} "
                f"entry={trade.entry_crypto_price:.2f} result={trade.result} "
                f"pnl={trade.pnl:+.2f} held={trade.bars_held}m"
            )


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Backtest crypto crossing markets")
    parser.add_argument("start_date", help="YYYYMMDD")
    parser.add_argument("end_date", help="YYYYMMDD")
    parser.add_argument("--strikes", default="", help="逗号分隔的 strike 价格；为空则根据价格范围自动生成")
    parser.add_argument("--kinds", default="dip,above", help="dip/above，可逗号分隔")
    parser.add_argument("--entry-price", type=float, default=0.90, help="假设 Polymarket YES 入场价")
    parser.add_argument("--distance", type=float, default=0.5, help="触发距离百分比")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--auto-strike-step", type=float, default=500, help="自动 strike 间隔")
    parser.add_argument("--save", default="trades/crypto_backtest.json", help="结果保存路径")
    args = parser.parse_args()

    kinds = [item.strip().lower() for item in args.kinds.split(",") if item.strip()]
    if not kinds or any(kind not in {"dip", "above"} for kind in kinds):
        raise ValueError("--kinds only supports dip and above")

    config = Config()
    backtester = CryptoBacktester(config)
    strike_values = parse_float_list(args.strikes)
    if not strike_values:
        async with aiohttp.ClientSession() as probe_session:
            probe = await backtester._fetch_day_klines(
                probe_session,
                args.symbol.upper(),
                datetime.strptime(args.start_date, "%Y%m%d").replace(tzinfo=timezone.utc),
            )
        if not probe:
            raise RuntimeError("No klines returned for automatic strike generation")
        low = min(bar["low"] for bar in probe)
        high = max(bar["high"] for bar in probe)
        step = args.auto_strike_step
        first = math.floor(low / step) * step
        last = math.ceil(high / step) * step
        strike_values = list(range(int(first), int(last) + int(step), int(step)))

    result = await backtester.run(
        start_date=args.start_date,
        end_date=args.end_date,
        strikes=strike_values,
        market_kinds=kinds,
        entry_price=args.entry_price,
        trigger_distance_pct=args.distance,
        symbol=args.symbol,
    )
    print_result(result)

    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    with open(args.save, "w", encoding="utf-8") as file:
        json.dump(asdict(result), file, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.save}")


if __name__ == "__main__":
    asyncio.run(main_async())
