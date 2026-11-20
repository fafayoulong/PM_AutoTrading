# -*- coding: utf-8 -*-
"""Sensitivity backtest using real historical Polymarket crypto market prices."""

import argparse
import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from src.config import Config


@dataclass
class HistoryMarket:
    event_id: str
    market_id: str
    question: str
    market_kind: str
    strike: float
    token_id: str
    trade_date: str
    end_time: str
    prices: list[dict]


@dataclass
class HistoryTrade:
    date: str
    market_id: str
    question: str
    market_kind: str
    strike: float
    entry_time: str
    entry_crypto_price: float
    entry_market_price: float
    size: float
    cost: float
    exit_time: str
    exit_price: float
    pnl: float
    result: str
    bars_held: int


@dataclass
class SensitivityRun:
    max_entry_price: float
    trigger_distance_pct: float
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    total_pnl: float
    roi: float
    max_drawdown: float
    final_equity: float
    trades: list[HistoryTrade]


@dataclass
class SensitivityReport:
    symbol: str
    asset: str
    start_date: str
    end_date: str
    market_count: int
    market_count_with_prices: int
    runs: list[SensitivityRun]


class CryptoHistoryBacktester:
    GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
    CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
    BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

    def __init__(self, config: Config):
        self.config = config

    async def _fetch_binance_day(
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
                })
            cursor = datetime.fromtimestamp(data[-1][0] / 1000, tz=timezone.utc) + timedelta(minutes=1)
            if len(data) < 1000:
                break
        return rows

    async def _fetch_day_markets(
        self,
        session: aiohttp.ClientSession,
        day_start: datetime,
        asset: str = "Bitcoin",
    ) -> list[dict]:
        day_end = day_start + timedelta(days=1)
        params = {
            "limit": 100,
            "closed": "true",
            "tag_slug": "crypto",
            "order": "endDate",
            "ascending": "false",
            "end_date_min": day_start.isoformat().replace("+00:00", "Z"),
            "end_date_max": day_end.isoformat().replace("+00:00", "Z"),
        }
        async with session.get(
            self.GAMMA_EVENTS_URL,
            params=params,
            proxy=self.config.proxy_url,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Gamma API {response.status}: {text[:200]}")
            events = await response.json()

        markets = []
        asset_pattern = re.compile(re.escape(asset), re.I)
        above_pattern = re.compile(rf"{re.escape(asset)} above ([\d,]+)", re.I)
        dip_pattern = re.compile(rf"{re.escape(asset)} (?:dip to|below) ([\d,]+)", re.I)

        for event in events:
            title = event.get("title", "")
            if not asset_pattern.search(title):
                continue
            for market in event.get("markets", []):
                question = market.get("question", "")
                above_match = above_pattern.search(question)
                dip_match = dip_pattern.search(question)
                if not above_match and not dip_match:
                    continue

                token_ids_raw = market.get("clobTokenIds", "[]")
                try:
                    token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
                except (TypeError, json.JSONDecodeError):
                    continue
                if not token_ids:
                    continue

                strike_text = above_match.group(1) if above_match else dip_match.group(1)
                end_text = market.get("endDate") or event.get("endDate")
                if not end_text:
                    continue
                end_time = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
                markets.append({
                    "event_id": event.get("id", ""),
                    "market_id": market.get("id", ""),
                    "question": question,
                    "market_kind": "above" if above_match else "dip",
                    "strike": float(strike_text.replace(",", "")),
                    "token_id": token_ids[0],
                    "end_time": end_time,
                })
        return markets

    async def _fetch_market_price_history(
        self,
        session: aiohttp.ClientSession,
        token_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        params = {
            "market": token_id,
            "startTs": int(start_time.timestamp()),
            "endTs": int(end_time.timestamp()),
            "fidelity": 1,
        }
        async with session.get(
            self.CLOB_PRICES_HISTORY_URL,
            params=params,
            proxy=self.config.proxy_url,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status != 200:
                return []
            payload = await response.json()

        prices = []
        for point in payload.get("history", []):
            prices.append({
                "time": datetime.fromtimestamp(point["t"], tz=timezone.utc),
                "price": float(point["p"]),
            })
        return prices

    async def load_history(
        self,
        start_date: str,
        end_date: str,
        symbol: str = "BTCUSDT",
        asset: str = "Bitcoin",
    ) -> tuple[list[dict], list[HistoryMarket]]:
        start = datetime.strptime(start_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        if end < start:
            raise ValueError("end_date must not be before start_date")

        bars = []
        markets: list[HistoryMarket] = []
        async with aiohttp.ClientSession() as session:
            current = start
            while current <= end:
                day_bars = await self._fetch_binance_day(session, symbol.upper(), current)
                bars.extend(day_bars)
                day_markets = await self._fetch_day_markets(session, current, asset)
                for raw_market in day_markets:
                    prices = await self._fetch_market_price_history(
                        session,
                        raw_market["token_id"],
                        current,
                        min(raw_market["end_time"], current + timedelta(days=1)),
                    )
                    if prices:
                        markets.append(HistoryMarket(
                            event_id=raw_market["event_id"],
                            market_id=raw_market["market_id"],
                            question=raw_market["question"],
                            market_kind=raw_market["market_kind"],
                            strike=raw_market["strike"],
                            token_id=raw_market["token_id"],
                            trade_date=current.date().isoformat(),
                            end_time=raw_market["end_time"].isoformat(),
                            prices=prices,
                        ))
                current += timedelta(days=1)
        return bars, markets

    @staticmethod
    def _price_at(market: HistoryMarket, timestamp: datetime, tolerance_seconds: int = 180) -> Optional[float]:
        latest = None
        for point in market.prices:
            if point["time"] <= timestamp + timedelta(seconds=60):
                latest = point
            else:
                break
        if latest and timestamp + timedelta(seconds=60) - latest["time"] <= timedelta(seconds=tolerance_seconds):
            return latest["price"]
        return None

    def evaluate(
        self,
        bars: list[dict],
        markets: list[HistoryMarket],
        max_entry_price: float,
        trigger_distance_pct: float,
        symbol: str = "BTCUSDT",
    ) -> SensitivityRun:
        trades: list[HistoryTrade] = []
        equity_curve = [self.config.starting_budget]
        cash = self.config.starting_budget

        for market in markets:
            day_key = market.trade_date
            day_bars = [bar for bar in bars if bar["time"].date().isoformat() == day_key]
            if not day_bars:
                continue

            entry_index = None
            entry_price = None
            for index, bar in enumerate(day_bars):
                distance_pct = abs(bar["close"] - market.strike) / market.strike * 100
                if market.market_kind == "above":
                    triggered = bar["close"] > market.strike and distance_pct <= trigger_distance_pct
                else:
                    triggered = bar["close"] > market.strike and distance_pct <= trigger_distance_pct
                if not triggered:
                    continue

                market_price = self._price_at(market, bar["time"])
                if market_price is None or market_price > max_entry_price:
                    continue

                entry_index = index
                entry_price = market_price
                break

            if entry_index is None or entry_price is None:
                continue

            if market.market_kind == "dip":
                resolved_win = any(bar["low"] <= market.strike for bar in day_bars)
                exit_index = next(
                    (index for index, bar in enumerate(day_bars) if bar["low"] <= market.strike),
                    len(day_bars) - 1,
                )
            else:
                final_bar = day_bars[-1]
                resolved_win = final_bar["close"] > market.strike
                exit_index = len(day_bars) - 1

            exit_price = 1.0 if resolved_win else 0.0
            cost = self.config.max_position_size
            size = cost / entry_price
            pnl = size * exit_price - cost
            trade = HistoryTrade(
                date=day_key,
                market_id=market.market_id,
                question=market.question,
                market_kind=market.market_kind,
                strike=market.strike,
                entry_time=day_bars[entry_index]["time"].isoformat(),
                entry_crypto_price=day_bars[entry_index]["close"],
                entry_market_price=entry_price,
                size=size,
                cost=cost,
                exit_time=day_bars[exit_index]["time"].isoformat(),
                exit_price=exit_price,
                pnl=pnl,
                result="WIN" if resolved_win else "LOSS",
                bars_held=exit_index - entry_index,
            )
            if trade.cost <= cash:
                cash -= trade.cost
                cash += trade.cost + trade.pnl
                equity_curve.append(cash)
                trades.append(trade)

        wins = sum(1 for trade in trades if trade.pnl > 0)
        total_pnl = sum(trade.pnl for trade in trades)
        peak = equity_curve[0]
        max_drawdown = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

        return SensitivityRun(
            max_entry_price=max_entry_price,
            trigger_distance_pct=trigger_distance_pct,
            trade_count=len(trades),
            win_count=wins,
            loss_count=len(trades) - wins,
            win_rate=wins / len(trades) if trades else 0.0,
            total_pnl=total_pnl,
            roi=total_pnl / self.config.starting_budget,
            max_drawdown=max_drawdown,
            final_equity=cash,
            trades=trades,
        )

    async def run_sensitivity(
        self,
        start_date: str,
        end_date: str,
        max_entry_prices: list[float],
        trigger_distances: list[float],
        symbol: str = "BTCUSDT",
        asset: str = "Bitcoin",
    ) -> SensitivityReport:
        bars, markets = await self.load_history(start_date, end_date, symbol, asset)
        runs = [
            self.evaluate(bars, markets, max_entry_price, trigger_distance, symbol)
            for max_entry_price in max_entry_prices
            for trigger_distance in trigger_distances
        ]
        return SensitivityReport(
            symbol=symbol.upper(),
            asset=asset,
            start_date=start_date,
            end_date=end_date,
            market_count=len(markets),
            market_count_with_prices=len([market for market in markets if market.prices]),
            runs=runs,
        )


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def print_report(report: SensitivityReport) -> None:
    print("=" * 72)
    print("Polymarket Crypto 穿越盘真实历史价格敏感性分析")
    print("=" * 72)
    print(f"区间: {report.start_date} - {report.end_date}")
    print(f"标的: {report.symbol}")
    print(f"历史市场: {report.market_count_with_prices}/{report.market_count}")
    print("-" * 72)
    print(f"{'max_entry':>10} {'distance%':>10} {'trades':>7} {'win%':>8} {'ROI':>8} {'PnL':>12} {'drawdown':>10}")
    for run in sorted(report.runs, key=lambda item: item.roi, reverse=True):
        print(
            f"{run.max_entry_price:>10.2f} {run.trigger_distance_pct:>10.2f} "
            f"{run.trade_count:>7} {run.win_rate:>8.1%} {run.roi:>8.2%} "
            f"{run.total_pnl:>12.2f} {run.max_drawdown:>10.2%}"
        )
    print("-" * 72)
    best = max(report.runs, key=lambda item: (item.roi, item.trade_count))
    print(f"最佳组合: entry<={best.max_entry_price:.2f}, distance<={best.trigger_distance_pct:.2f}%")
    if best.trades:
        print("最佳组合最近 10 笔:")
        for trade in best.trades[-10:]:
            print(
                f"{trade.date} {trade.market_kind:<5} strike={trade.strike:.0f} "
                f"entry_pm={trade.entry_market_price:.3f} entry_btc={trade.entry_crypto_price:.2f} "
                f"result={trade.result} pnl={trade.pnl:+.2f}"
            )


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Real historical Polymarket crypto sensitivity backtest")
    parser.add_argument("start_date", help="YYYYMMDD")
    parser.add_argument("end_date", help="YYYYMMDD")
    parser.add_argument("--max-entry-prices", default="0.90,0.92,0.94,0.95,0.97")
    parser.add_argument("--distances", default="0.1,0.25,0.5,1.0")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--asset", default="Bitcoin")
    parser.add_argument("--save", default="trades/crypto_history_sensitivity.json")
    args = parser.parse_args()

    config = Config()
    backtester = CryptoHistoryBacktester(config)
    report = await backtester.run_sensitivity(
        start_date=args.start_date,
        end_date=args.end_date,
        max_entry_prices=parse_float_list(args.max_entry_prices),
        trigger_distances=parse_float_list(args.distances),
        symbol=args.symbol,
        asset=args.asset,
    )
    print_report(report)

    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    with open(args.save, "w", encoding="utf-8") as file:
        json.dump(asdict(report), file, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.save}")


if __name__ == "__main__":
    asyncio.run(main_async())
