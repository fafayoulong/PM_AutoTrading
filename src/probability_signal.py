# -*- coding: utf-8 -*-
"""Heuristic probability signals used only for paper data collection."""

from dataclasses import dataclass

from src.config import Config


@dataclass
class ProbabilitySignal:
    game_id: str
    leader_name: str
    underdog_name: str
    leader_score: int
    underdog_score: int
    period: int
    time_remaining_seconds: float
    estimated_probability: float
    rationale: str


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def evaluate_nba_probability(config: Config, game) -> ProbabilitySignal | None:
    if game.status != "in":
        return None

    diff = abs(game.home_score - game.away_score)
    minutes = max(game.clock_seconds, 0.0) / 60.0
    late_game_bonus = max(0.0, 12.0 - minutes) * 0.02
    period_bonus = max(0, game.quarter - 3) * 0.04
    probability = _clamp(0.50 + diff * 0.03 + late_game_bonus + period_bonus, 0.0, 0.93)
    if probability < config.probability_log_min_probability:
        return None

    if game.home_score > game.away_score:
        leader_name = game.home_team_name
        underdog_name = game.away_team_name
        leader_score = game.home_score
        underdog_score = game.away_score
    else:
        leader_name = game.away_team_name
        underdog_name = game.home_team_name
        leader_score = game.away_score
        underdog_score = game.home_score

    return ProbabilitySignal(
        game_id=game.game_id,
        leader_name=leader_name,
        underdog_name=underdog_name,
        leader_score=leader_score,
        underdog_score=underdog_score,
        period=game.quarter,
        time_remaining_seconds=game.clock_seconds,
        estimated_probability=round(probability, 4),
        rationale=f"diff={diff}; remaining={minutes:.1f}m",
    )


def evaluate_mlb_probability(config: Config, game: dict) -> ProbabilitySignal | None:
    if game.get("status") != "in":
        return None

    try:
        inning = int("".join(ch for ch in game.get("status_detail", "") if ch.isdigit()) or 0)
    except ValueError:
        return None

    diff = abs(game["home_score"] - game["away_score"])
    probability = _clamp(0.50 + max(0, inning - 7) * 0.05 + diff * 0.07, 0.0, 0.90)
    if probability < config.probability_log_min_probability:
        return None

    if game["home_score"] > game["away_score"]:
        leader_name = game["home_team_name"]
        underdog_name = game["away_team_name"]
        leader_score = game["home_score"]
        underdog_score = game["away_score"]
    else:
        leader_name = game["away_team_name"]
        underdog_name = game["home_team_name"]
        leader_score = game["away_score"]
        underdog_score = game["home_score"]

    return ProbabilitySignal(
        game_id=game["game_id"],
        leader_name=leader_name,
        underdog_name=underdog_name,
        leader_score=leader_score,
        underdog_score=underdog_score,
        period=inning,
        time_remaining_seconds=0.0,
        estimated_probability=round(probability, 4),
        rationale=f"inning={inning}; diff={diff}",
    )


def evaluate_tennis_probability(config: Config, match: dict) -> ProbabilitySignal | None:
    if not match.get('player1', {}).get('name') or not match.get('player2', {}).get('name'):
        return None
    if match.get('player1', {}).get('name') == '?' or match.get('player2', {}).get('name') == '?':
        return None
    if match.get("status") != "in":
        return None

    player1 = match.get("player1", {})
    player2 = match.get("player2", {})
    lines1 = player1.get("linescores", [])
    lines2 = player2.get("linescores", [])
    if len(lines1) < 2 or len(lines2) < 2:
        return None

    sets1 = sum(1 for a, b in zip(lines1, lines2) if a > b)
    sets2 = sum(1 for a, b in zip(lines1, lines2) if b > a)
    if sets1 == sets2:
        return None

    if sets1 > sets2:
        leader = player1
        underdog = player2
        leader_sets = sets1
        underdog_sets = sets2
        leader_current = lines1[-1]
        underdog_current = lines2[-1]
    else:
        leader = player2
        underdog = player1
        leader_sets = sets2
        underdog_sets = sets1
        leader_current = lines2[-1]
        underdog_current = lines1[-1]

    if leader_current <= underdog_current:
        return None

    game_diff = leader_current - underdog_current
    probability = _clamp(0.50 + (leader_sets - underdog_sets) * 0.12 + game_diff * 0.06, 0.0, 0.90)
    if probability < config.probability_log_min_probability:
        return None

    return ProbabilitySignal(
        game_id=match["match_id"],
        leader_name=leader.get("name", ""),
        underdog_name=underdog.get("name", ""),
        leader_score=leader_sets,
        underdog_score=underdog_sets,
        period=match.get("period", 0),
        time_remaining_seconds=0.0,
        estimated_probability=round(probability, 4),
        rationale=f"sets={leader_sets}-{underdog_sets}; games={leader_current}-{underdog_current}",
    )
