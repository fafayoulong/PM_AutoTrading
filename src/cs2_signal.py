# -*- coding: utf-8 -*-
"""CS2 扫尾盘信号：系列赛拿到赛点 + 当前地图拿到赛点且领先足够多。

数据来自 Polymarket 体育实时流，score 形如：
    "012-005|1-0|Bo3"
    当前地图回合比分 | 已赢地图数 | 赛制
"""

import logging
from dataclasses import dataclass
from typing import Optional

from src.config import Config

logger = logging.getLogger(__name__)

LIVE_STATES = {"running", "inprogress", "live", "in"}


@dataclass
class CS2Signal:
    game_id: str
    leader_team: str
    underdog_team: str
    maps_leader: int
    maps_underdog: int
    round_leader: int
    round_underdog: int
    maps_to_win: int
    period: str
    status_detail: str


def parse_cs2_score(score: str) -> Optional[tuple[int, int, int, int, Optional[int]]]:
    """返回 (home_rounds, away_rounds, home_maps, away_maps, maps_to_win)。"""
    if not score:
        return None
    parts = [p.strip() for p in str(score).split("|")]
    if len(parts) < 3:
        return None
    try:
        rounds = parts[0].split("-")
        maps = parts[1].split("-")
        if len(rounds) != 2 or len(maps) != 2:
            return None
        home_rounds, away_rounds = int(rounds[0]), int(rounds[1])
        home_maps, away_maps = int(maps[0]), int(maps[1])
    except (TypeError, ValueError):
        return None

    maps_to_win = None
    fmt = parts[2].lower()
    if fmt.startswith("bo") and fmt[2:].isdigit():
        best_of = int(fmt[2:])
        if best_of >= 1:
            maps_to_win = best_of // 2 + 1
    return home_rounds, away_rounds, home_maps, away_maps, maps_to_win


def evaluate_cs2_lock(config: Config, game: dict) -> Optional[CS2Signal]:
    if not game.get("live") or game.get("ended"):
        return None
    if str(game.get("status", "")).lower() not in LIVE_STATES:
        return None

    parsed = parse_cs2_score(game.get("score", ""))
    if not parsed:
        return None
    home_rounds, away_rounds, home_maps, away_maps, maps_to_win = parsed
    if not maps_to_win:
        return None

    drill = getattr(config, "signal_drill_mode", False)

    if not drill:
        if maps_to_win <= 1:
            # Bo1：没有系列赛概念，一张图定胜负，领先方按当前地图回合判定
            if home_rounds == away_rounds:
                return None
            leader_is_home = home_rounds > away_rounds
            maps_leader = home_maps
            maps_underdog = away_maps
        else:
            if home_maps == away_maps:
                return None
            leader_is_home = home_maps > away_maps
            maps_leader = home_maps if leader_is_home else away_maps
            maps_underdog = away_maps if leader_is_home else home_maps
            # 必须已经拿到赛点（再赢一张图就赢下系列赛）
            if maps_leader != maps_to_win - 1:
                return None

        round_leader = home_rounds if leader_is_home else away_rounds
        round_underdog = away_rounds if leader_is_home else home_rounds
        if round_leader < config.cs2_map_point_rounds:
            return None
        if round_leader - round_underdog < config.cs2_min_map_round_lead:
            return None
    else:
        # 演练：只要有领先方就触发
        if home_rounds + home_maps == away_rounds + away_maps:
            return None
        leader_is_home = (home_rounds + home_maps) > (away_rounds + away_maps)
        maps_leader = home_maps if leader_is_home else away_maps
        maps_underdog = away_maps if leader_is_home else home_maps
        round_leader = home_rounds if leader_is_home else away_rounds
        round_underdog = away_rounds if leader_is_home else home_rounds

    leader_team = game.get("homeTeam") if leader_is_home else game.get("awayTeam")
    underdog_team = game.get("awayTeam") if leader_is_home else game.get("homeTeam")
    if not leader_team or not underdog_team:
        return None

    return CS2Signal(
        game_id=str(game.get("gameId", "")),
        leader_team=leader_team,
        underdog_team=underdog_team,
        maps_leader=maps_leader,
        maps_underdog=maps_underdog,
        round_leader=round_leader,
        round_underdog=round_underdog,
        maps_to_win=maps_to_win,
        period=str(game.get("period") or ""),
        status_detail=f"maps {maps_leader}-{maps_underdog} | rounds {round_leader}-{round_underdog} | {game.get('score','')}",
    )
