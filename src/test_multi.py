# -*- coding: utf-8 -*-
"""Quick test for multi-sport signal detection."""

import asyncio

from src.config import load_config
from src.nba_feed import NBAFeed
from src.multi_signal import evaluate_mlb_lock, evaluate_tennis_lock


async def main():
    config = load_config()
    feed = NBAFeed(config)
    await feed.start()

    mlb = await feed.fetch_mlb_games()
    print(f"MLB in-progress games: {len(mlb)}")
    for g in mlb:
        sig = evaluate_mlb_lock(config, g)
        status = "LOCKED" if sig else "not locked"
        print(f"  {g['home_team']} vs {g['away_team']} {g['home_score']}-{g['away_score']} {g['status_detail']} => {status}")

    tennis = await feed.fetch_tennis_matches()
    print(f"Tennis in-progress matches: {len(tennis)}")
    for m in tennis:
        sig = evaluate_tennis_lock(config, m)
        p1 = m['player1']['name']
        p2 = m['player2']['name']
        if sig:
            status = f"LOCKED: {sig.leader_name} leads {sig.sets_leader}-{sig.sets_underdog} sets, {sig.current_set_leader}-{sig.current_set_underdog} games"
        else:
            status = "not locked"
        print(f"  {p1} vs {p2} ({m['status_detail']}) => {status}")

    await feed.stop()


if __name__ == "__main__":
    asyncio.run(main())
