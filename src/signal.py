# -*- coding: utf-8 -*-
"""Detect 'game is effectively locked' signals in late NBA games."""

import logging
from dataclasses import dataclass

from src.config import Config
from src.nba_feed import NBAGameState

logger = logging.getLogger(__name__)


@dataclass
class LockSignal:
    game_id: str
    leader_team: str
    underdog_team: str
    leader_score: int
    underdog_score: int
    quarter: int
    clock_seconds: float
    score_diff: int
    required_diff: int
    confidence: float


def required_score_diff(config: Config, clock_seconds: float) -> int:
    """Score differential needed to consider the game locked given remaining time."""
    minutes = max(clock_seconds, 0) / 60.0
    return int(config.min_score_diff + minutes * config.score_diff_per_minute)


def evaluate_lock(
    config: Config,
    game: NBAGameState,
) -> LockSignal | None:
    """Check whether the game state meets the 'locked' criteria."""
    if game.status != "in":
        return None

    clock = game.clock_seconds
    diff = abs(game.home_score - game.away_score)

    if getattr(config, "signal_drill_mode", False):
        # 演练模式：只要比赛进行中且有领先方就触发，用来跑通下游链路
        if diff <= 0:
            return None
        required = 0
        confidence = 0.0
    else:
        if game.quarter < config.min_quarter:
            return None
        if clock < config.max_time_remaining_seconds:
            return None
        if clock > config.min_time_remaining_seconds:
            return None
        required = required_score_diff(config, clock)
        if diff < required:
            return None
        confidence = min(diff / max(required, 1), 1.5)

    # Determine leader
    if game.home_score > game.away_score:
        leader, underdog = game.home_team, game.away_team
        leader_score, underdog_score = game.home_score, game.away_score
    else:
        leader, underdog = game.away_team, game.home_team
        leader_score, underdog_score = game.away_score, game.home_score

    return LockSignal(
        game_id=game.game_id,
        leader_team=leader,
        underdog_team=underdog,
        leader_score=leader_score,
        underdog_score=underdog_score,
        quarter=game.quarter,
        clock_seconds=clock,
        score_diff=diff,
        required_diff=required,
        confidence=confidence,
    )
