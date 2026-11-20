# -*- coding: utf-8 -*-
"""方案 D：用历史数据校准信号阈值。

用法:
    python tools/calibrate_params.py mlb              # 价格闸门校准（离线）
    python tools/calibrate_params.py nba 20260301 20260320   # 局面规则校准（需联网）
"""

import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MLB_DATASET = ROOT / "trades" / "mlb_dataset.jsonl"
PLAYS_CACHE = ROOT / "tmp" / "nba_plays"
REPORT_PATH = ROOT / "trades" / "calibration_report.json"


def load_mlb_by_game() -> dict:
    by_game = defaultdict(list)
    with MLB_DATASET.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            by_game[row["event_id"]].append(row)
    return by_game


def mlb_entry_stats(by_game: dict, threshold: float, max_minutes_left=None) -> dict | None:
    """每场比赛只取一次入场：首次价格达到阈值的那一分钟。"""
    count = wins = 0
    costs = pnl = 0.0
    entries = []
    for rows in by_game.values():
        hit = None
        for row in sorted(rows, key=lambda r: r["t"]):
            if row["t"] < row["game_start_ts"]:
                continue
            if max_minutes_left is not None:
                left = (row["finish_ts"] - row["t"]) / 60.0
                if left > max_minutes_left:
                    continue
            if row["price"] >= threshold:
                hit = row
                break
        if hit is None:
            continue
        count += 1
        wins += int(hit["label"])
        costs += hit["price"]
        pnl += hit["label"] - hit["price"]
        entries.append(hit["price"])
    if not count:
        return None
    return {
        "threshold": threshold,
        "games": count,
        "win_rate": wins / count,
        "avg_entry": statistics.mean(entries),
        "edge_per_share": pnl / count,
        "roi": pnl / costs if costs else 0.0,
    }


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5)
    return (centre - margin) / denominator


def run_mlb() -> dict:
    by_game = load_mlb_by_game()
    print(f"MLB 数据集：{len(by_game)} 场比赛")
    print()
    print("== 价格闸门校准（每场只入场一次，仅盘中，剩余 <= 60 分钟）==")
    print(f"{'阈值':>6} {'场次':>5} {'胜率':>8} {'胜率下界':>9} {'均价':>8} {'每股边':>9} {'ROI':>8}")
    results = []
    for threshold in (0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98):
        stat = mlb_entry_stats(by_game, threshold, max_minutes_left=60)
        if not stat:
            continue
        lower = wilson_lower(round(stat["win_rate"] * stat["games"]), stat["games"])
        stat["win_rate_lower"] = lower
        results.append(stat)
        print(
            f"{threshold:>6.2f} {stat['games']:>5} {stat['win_rate']:>8.4f} {lower:>9.4f} "
            f"{stat['avg_entry']:>8.4f} {stat['edge_per_share']:>+9.4f} {stat['roi']:>+8.2%}"
        )
    return {"sport": "MLB", "rows": results}


def remaining_minutes(period: int, clock_seconds: float) -> float:
    if period <= 4:
        return max(0.0, (4 - period) * 12.0 + clock_seconds / 60.0)
    return max(0.0, clock_seconds / 60.0)


def parse_clock(text: str) -> float:
    try:
        if ":" in text:
            minutes, seconds = text.split(":")
            return float(minutes) * 60 + float(seconds)
        return float(text)
    except (TypeError, ValueError):
        return 0.0


async def load_plays(games) -> dict:
    from src.backtest import Backtester  # noqa: PLC0415
    from src.config import load_config  # noqa: PLC0415

    PLAYS_CACHE.mkdir(parents=True, exist_ok=True)
    bt = Backtester(load_config())
    plays_by_game = {}
    fetched = 0
    for index, game in enumerate(games, 1):
        cache_file = PLAYS_CACHE / f"{game.game_id}.json"
        if cache_file.exists():
            plays_by_game[game.game_id] = json.loads(cache_file.read_text(encoding="utf-8"))
            continue
        plays = await bt.fetch_game_plays(game.game_id)
        if plays:
            cache_file.write_text(json.dumps(plays), encoding="utf-8")
            plays_by_game[game.game_id] = plays
            fetched += 1
        if index % 25 == 0:
            print(f"  已处理 {index}/{len(games)} 场（新抓取 {fetched}）")
    await bt.close()
    return plays_by_game


def first_trigger(
    plays: list,
    min_quarter: int,
    min_diff: int,
    diff_per_minute: float,
    max_remaining_minutes: float = 8.0,
):
    for play in plays:
        period = (play.get("period") or {}).get("number", 0)
        if period < min_quarter:
            continue
        clock = parse_clock((play.get("clock") or {}).get("displayValue", ""))
        remaining = remaining_minutes(period, clock)
        if remaining > max_remaining_minutes:
            continue
        home = int(play.get("homeScore") or 0)
        away = int(play.get("awayScore") or 0)
        diff = abs(home - away)
        required = min_diff + remaining * diff_per_minute
        if diff >= required and diff > 0:
            return {
                "period": period,
                "remaining": remaining,
                "diff": diff,
                "leader_is_home": home > away,
            }
    return None


async def run_nba(start_date: str, end_date: str) -> dict:
    from src.backtest import Backtester  # noqa: PLC0415
    from src.config import load_config  # noqa: PLC0415

    bt = Backtester(load_config())
    games = await bt.fetch_completed_games(start_date, end_date)
    await bt.close()
    print(f"NBA 已完成比赛：{len(games)} 场（{start_date} ~ {end_date}）")
    plays_by_game = await load_plays(games)
    print(f"拿到逐回合数据的比赛：{len(plays_by_game)} 场")
    print()

    winners = {g.game_id: g.winner for g in games}
    candidates = []
    for min_diff in (4, 5, 6, 8, 10):
        for per_minute in (0.0, 0.15, 0.25, 0.4):
            fired = wins = 0
            for game_id, plays in plays_by_game.items():
                trigger = first_trigger(plays, 4, min_diff, per_minute, 8.0)
                if not trigger:
                    continue
                fired += 1
                leader = "home" if trigger["leader_is_home"] else "away"
                if leader == winners.get(game_id):
                    wins += 1
            if fired:
                candidates.append({
                    "min_diff": min_diff,
                    "diff_per_minute": per_minute,
                    "max_remaining_minutes": 8.0,
                    "games": fired,
                    "win_rate": wins / fired,
                    "win_rate_lower": wilson_lower(wins, fired),
                })

    print("== 局面规则候选（Q4 起，剩余 <= 8 分钟，取每场首次触发状态）==")
    print(f"{'min_diff':>8} {'每分增量':>8} {'触发场次':>8} {'胜率':>8} {'胜率下界':>9}")
    for row in sorted(candidates, key=lambda r: (r["win_rate"], r["games"]), reverse=True):
        print(
            f"{row['min_diff']:>8} {row['diff_per_minute']:>8.2f} {row['games']:>8} "
            f"{row['win_rate']:>8.4f} {row['win_rate_lower']:>9.4f}"
        )

    print()
    print("== 时钟窗口敏感性（取上表中两组候选）==")
    window_rows = []
    for min_diff, per_minute in ((8, 0.25), (6, 0.40), (5, 0.0)):
        for window in (4.0, 6.0, 8.0, 12.0):
            fired = wins = 0
            for game_id, plays in plays_by_game.items():
                trigger = first_trigger(plays, 4, min_diff, per_minute, window)
                if not trigger:
                    continue
                fired += 1
                leader = "home" if trigger["leader_is_home"] else "away"
                if leader == winners.get(game_id):
                    wins += 1
            if not fired:
                continue
            row = {
                "min_diff": min_diff,
                "diff_per_minute": per_minute,
                "max_remaining_minutes": window,
                "games": fired,
                "win_rate": wins / fired,
                "win_rate_lower": wilson_lower(wins, fired),
            }
            window_rows.append(row)
            print(
                f"  min_diff={min_diff} per_min={per_minute:.2f} 窗口<={window:.0f}分钟 "
                f"-> {fired:>3} 场 胜率 {row['win_rate']:.4f} 下界 {row['win_rate_lower']:.4f}"
            )
    return {
        "sport": "NBA",
        "range": [start_date, end_date],
        "games": len(games),
        "rows": candidates,
        "window_sweep": window_rows,
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    mode = sys.argv[1].lower()
    started = time.time()
    if mode == "mlb":
        report = run_mlb()
    elif mode == "nba":
        start_date = sys.argv[2] if len(sys.argv) > 2 else "20260301"
        end_date = sys.argv[3] if len(sys.argv) > 3 else "20260320"
        report = asyncio.run(run_nba(start_date, end_date))
    else:
        print(__doc__)
        return
    report["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    history = []
    if REPORT_PATH.exists():
        try:
            history = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = []
    if isinstance(history, dict):
        history = [history]
    history.append(report)
    REPORT_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"用时 {time.time() - started:.1f}s，已写入 {REPORT_PATH}")


if __name__ == "__main__":
    main()
