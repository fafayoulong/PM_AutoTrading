# -*- coding: utf-8 -*-
"""Build a Polymarket-only MLB win-probability dataset."""

import argparse
import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp

from src.config import Config


logger = logging.getLogger(__name__)


def parse_json_list(raw: Any) -> list:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def parse_timestamp(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        pass
    try:
        return int(datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S%z").timestamp())
    except Exception:
        return None


def parse_record(value: Any) -> float:
    if not value or "-" not in str(value):
        return 0.5
    try:
        wins, losses = str(value).split("-", 1)
        total = int(wins) + int(losses)
        return int(wins) / total if total else 0.5
    except Exception:
        return 0.5


class MlbDatasetCollector:
    def __init__(
        self,
        config: Config,
        output_path: str,
        limit: int = 100,
        max_pages: int = 10,
        min_points: int = 30,
        lookback_hours: int = 6,
    ):
        self.config = config
        self.output_path = Path(output_path)
        self.limit = limit
        self.max_pages = max_pages
        self.min_points = min_points
        self.lookback_hours = lookback_hours

    async def collect(self) -> list[dict]:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            events = await self._fetch_events(session)
            rows: list[dict] = []
            for index, event in enumerate(events, 1):
                market = self._select_moneyline(event)
                if market is None:
                    continue
                collected = await self._collect_market(session, event, market)
                if collected:
                    rows.extend(collected)
                    logger.info(
                        "Collected %d points from %s (%d/%d)",
                        len(collected), event.get("title"), index, len(events),
                    )
                else:
                    logger.warning("Skipped market with insufficient history: %s", event.get("title"))
        return rows

    async def _fetch_events(self, session: aiohttp.ClientSession) -> list[dict]:
        events: list[dict] = []
        page_size = 100
        for page in range(self.max_pages):
            params = {
                "tag_slug": "mlb",
                "closed": "true",
                "limit": page_size,
                "offset": page * page_size,
                "order": "endDate",
                "ascending": "false",
            }
            url = f"{self.config.polymarket_gamma_url}/events"
            try:
                async with session.get(url, params=params, proxy=self.config.proxy_url) as resp:
                    if resp.status != 200:
                        logger.warning("Gamma events returned HTTP %d", resp.status)
                        break
                    batch = await resp.json()
            except Exception as exc:
                logger.warning("Gamma events request failed: %s", exc)
                break
            if not batch:
                break
            mlb_events = [event for event in batch if event.get("seriesSlug") == "mlb"]
            events.extend(mlb_events)
            if len(events) >= self.limit or len(batch) < page_size:
                break
        return events[: self.limit]

    @staticmethod
    def _select_moneyline(event: dict) -> Optional[dict]:
        candidates = []
        for market in event.get("markets", []):
            if market.get("sportsMarketType") != "moneyline":
                continue
            if not market.get("active", True) or not market.get("closed", False):
                continue
            outcomes = parse_json_list(market.get("outcomes", "[]"))
            outcome_prices = parse_json_list(market.get("outcomePrices", "[]"))
            token_ids = parse_json_list(market.get("clobTokenIds", "[]"))
            if len(outcomes) != 2 or len(outcome_prices) != 2 or len(token_ids) != 2:
                continue
            try:
                prices = [float(price) for price in outcome_prices]
            except Exception:
                continue
            if abs(sum(prices) - 1.0) > 0.05 or min(prices) < 0.0 or max(prices) > 1.0:
                continue
            candidates.append(market)
        if not candidates:
            return None
        return max(candidates, key=lambda market: float(market.get("volumeNum", 0) or 0))

    async def _collect_market(self, session: aiohttp.ClientSession, event: dict, market: dict) -> list[dict]:
        outcomes = parse_json_list(market.get("outcomes", "[]"))
        outcome_prices = parse_json_list(market.get("outcomePrices", "[]"))
        token_ids = parse_json_list(market.get("clobTokenIds", "[]"))
        if len(outcomes) != 2 or len(outcome_prices) != 2 or len(token_ids) != 2:
            return []

        try:
            final_prices = [float(price) for price in outcome_prices]
        except Exception:
            return []
        if abs(final_prices[0] - 1.0) > 0.05 and abs(final_prices[0]) > 0.05:
            return []
        label = 1 if final_prices[0] >= 0.5 else 0

        game_start_ts = parse_timestamp(event.get("startTime") or market.get("gameStartTime"))
        finish_ts = parse_timestamp(
            event.get("finishedTimestamp")
            or event.get("closedTime")
            or market.get("closedTime")
        )
        if game_start_ts is None:
            return []
        if finish_ts is None or finish_ts <= game_start_ts:
            finish_ts = game_start_ts + 6 * 3600

        teams = event.get("teams", [])
        home_team = next((team.get("name", "") for team in teams if team.get("ordering") == "home"), "")
        away_team = next((team.get("name", "") for team in teams if team.get("ordering") == "away"), "")
        records = {team.get("name", ""): team.get("record", "") for team in teams}

        start_ts = game_start_ts - self.lookback_hours * 3600
        history, used_token_index = await self._fetch_price_history(
            session,
            token_ids,
            start_ts,
            finish_ts + 3600,
        )
        if len(history) < self.min_points:
            return []

        common = {
            "event_id": str(event.get("id", "")),
            "market_id": str(market.get("id", "")),
            "event_title": event.get("title", ""),
            "team_a": outcomes[0],
            "team_b": outcomes[1],
            "home_team": home_team,
            "away_team": away_team,
            "team_a_is_home": 1 if outcomes[0] == home_team else 0,
            "team_a_record": parse_record(records.get(outcomes[0])),
            "team_b_record": parse_record(records.get(outcomes[1])),
            "game_start_ts": game_start_ts,
            "finish_ts": finish_ts,
            "volume": float(market.get("volumeNum", 0) or 0),
            "liquidity": float(market.get("liquidityNum", 0) or 0),
            "label": label,
        }
        rows = []
        for point in history:
            timestamp = int(point.get("t", 0))
            try:
                price = float(point.get("p", 0))
            except Exception:
                continue
            if used_token_index == 1:
                price = 1.0 - price
            if price <= 0.0 or price >= 1.0:
                continue
            row = {"t": timestamp, "price": price, "token_id": str(token_ids[used_token_index])}
            row.update(common)
            rows.append(row)
        return rows

    async def _fetch_price_history(
        self,
        session: aiohttp.ClientSession,
        token_ids: list[str],
        start_ts: int,
        end_ts: int,
    ) -> tuple[list[dict], int]:
        semaphore = asyncio.Semaphore(3)
        async def fetch_one(token_id: str) -> list[dict]:
            async with semaphore:
                params = {
                    "market": token_id,
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "fidelity": 1,
                }
                url = f"{self.config.polymarket_clob_url}/prices-history"
                for attempt in range(3):
                    try:
                        async with session.get(url, params=params, proxy=self.config.proxy_url) as resp:
                            if resp.status == 429:
                                await asyncio.sleep(1 + attempt)
                                continue
                            if resp.status != 200:
                                logger.debug("CLOB history returned HTTP %d", resp.status)
                                return []
                            data = await resp.json()
                            return data.get("history", []) if isinstance(data, dict) else []
                    except Exception as exc:
                        logger.debug("CLOB history failed for %s: %s", token_id, exc)
                        await asyncio.sleep(1 + attempt)
                return []

        first = await fetch_one(token_ids[0])
        if len(first) >= self.min_points:
            return first, 0
        second = await fetch_one(token_ids[1])
        if len(second) > len(first):
            return second, 1
        return first, 0


async def collect_dataset(
    config: Config,
    output_path: str,
    limit: int,
    max_pages: int,
    min_points: int,
    lookback_hours: int,
) -> list[dict]:
    collector = MlbDatasetCollector(
        config=config,
        output_path=output_path,
        limit=limit,
        max_pages=max_pages,
        min_points=min_points,
        lookback_hours=lookback_hours,
    )
    return await collector.collect()


def write_dataset(rows: list[dict], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Polymarket MLB moneyline history.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--min-points", type=int, default=30)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--output", default="trades/mlb_dataset.jsonl")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    rows = asyncio.run(collect_dataset(
        config=Config(),
        output_path=args.output,
        limit=args.limit,
        max_pages=args.max_pages,
        min_points=args.min_points,
        lookback_hours=args.lookback_hours,
    ))
    write_dataset(rows, args.output)
    logger.info("Saved %d rows to %s", len(rows), args.output)


if __name__ == "__main__":
    main()
