# -*- coding: utf-8 -*-
"""Train and evaluate a Polymarket-only MLB win-probability model."""

import argparse
import bisect
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
from scipy.optimize import minimize


logger = logging.getLogger(__name__)


FEATURE_NAMES = [
    "logit_price",
    "logit_open",
    "logit_delta_open",
    "minutes_since_start",
    "team_a_is_home",
    "team_a_record",
    "team_b_record",
    "log_volume",
    "price_delta_10m",
    "price_delta_30m",
    "price_delta_60m",
    "price_roll_mean_30m",
    "price_roll_std_30m",
    "price_roll_mean_60m",
    "price_roll_std_60m",
    "price_ema_10m",
    "price_ema_30m",
    "price_ema_60m",
    "price_trend_30m",
    "price_trend_60m",
]


def read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def stable_logit(price: float, clip: float = 8.0) -> float:
    price = min(max(float(price), 1e-6), 1.0 - 1e-6)
    return float(np.clip(math.log(price / (1.0 - price)), -clip, clip))


def sigmoid(value: float) -> float:
    if value >= 0:
        exp_neg = math.exp(-value)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(value)
    return exp_pos / (1.0 + exp_pos)


def filter_rows(rows: list[dict], include_pre_game: bool, max_minutes: int) -> list[dict]:
    filtered = []
    for row in rows:
        game_start_ts = row.get("game_start_ts")
        finish_ts = row.get("finish_ts")
        timestamp = row.get("t")
        if not game_start_ts or not timestamp:
            continue
        if not include_pre_game and timestamp < game_start_ts:
            continue
        if finish_ts and timestamp > finish_ts:
            continue
        minutes_since_start = (timestamp - game_start_ts) / 60.0
        if max_minutes and minutes_since_start > max_minutes:
            continue
        if not 0.0 < float(row.get("price", 0.0)) < 1.0:
            continue
        filtered.append(row)
    return filtered


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get("market_id") or row.get("event_id") or "")
        if key:
            grouped[key].append(row)
    for market_rows in grouped.values():
        market_rows.sort(key=lambda row: int(row["t"]))
    return grouped


def _rolling_stats(
    times: list[int],
    prices: list[float],
    index: int,
    window_seconds: int,
) -> tuple[float, float]:
    if index == 0:
        return prices[0], 0.0
    cut = bisect.bisect_left(times, times[index] - window_seconds)
    window = prices[cut:index + 1]
    if not window:
        return prices[index], 0.0
    mean = sum(window) / len(window)
    variance = sum((price - mean) ** 2 for price in window) / len(window)
    return mean, math.sqrt(variance)


def _price_at_or_before(times: list[int], prices: list[float], index: int, seconds: int) -> float:
    target = times[index] - seconds
    cut = bisect.bisect_right(times, target) - 1
    return prices[max(cut, 0)]


def build_features(
    rows: list[dict],
) -> tuple[list[dict], list[list[float]], list[int], list[str]]:
    grouped = group_rows(rows)
    metadata: list[dict] = []
    features: list[list[float]] = []
    labels: list[int] = []
    group_ids: list[str] = []

    for group_id, market_rows in grouped.items():
        if len(market_rows) < 30:
            continue
        labels_in_group = {int(row.get("label", 0)) for row in market_rows}
        if len(labels_in_group) != 1:
            continue
        label = labels_in_group.pop()

        times = [int(row["t"]) for row in market_rows]
        prices = [float(row["price"]) for row in market_rows]
        game_start_ts = int(market_rows[0]["game_start_ts"])
        open_price = prices[0]
        open_logit = stable_logit(open_price)

        ema_10 = prices[0]
        ema_30 = prices[0]
        ema_60 = prices[0]
        emas_10: list[float] = []
        emas_30: list[float] = []
        emas_60: list[float] = []
        for index, price in enumerate(prices):
            if index:
                dt = max(times[index] - times[index - 1], 1)
                alpha_10 = 1.0 - math.exp(-dt / 600.0)
                alpha_30 = 1.0 - math.exp(-dt / 1800.0)
                alpha_60 = 1.0 - math.exp(-dt / 3600.0)
                ema_10 = price * alpha_10 + ema_10 * (1.0 - alpha_10)
                ema_30 = price * alpha_30 + ema_30 * (1.0 - alpha_30)
                ema_60 = price * alpha_60 + ema_60 * (1.0 - alpha_60)
            emas_10.append(ema_10)
            emas_30.append(ema_30)
            emas_60.append(ema_60)

        for index, row in enumerate(market_rows):
            price = prices[index]
            logit_price = stable_logit(price)
            mean_30, std_30 = _rolling_stats(times, prices, index, 30 * 60)
            mean_60, std_60 = _rolling_stats(times, prices, index, 60 * 60)
            delta_10 = price - _price_at_or_before(times, prices, index, 10 * 60)
            delta_30 = price - _price_at_or_before(times, prices, index, 30 * 60)
            delta_60 = price - _price_at_or_before(times, prices, index, 60 * 60)
            feature_values = [
                logit_price,
                open_logit,
                logit_price - open_logit,
                (times[index] - game_start_ts) / 60.0,
                float(row.get("team_a_is_home", 0.0)),
                float(row.get("team_a_record", 0.5)),
                float(row.get("team_b_record", 0.5)),
                math.log1p(float(row.get("volume", 0.0))),
                delta_10,
                delta_30,
                delta_60,
                mean_30,
                std_30,
                mean_60,
                std_60,
                emas_10[index],
                emas_30[index],
                emas_60[index],
                price - mean_30,
                price - mean_60,
            ]
            metadata.append({
                "event_id": row.get("event_id"),
                "market_id": row.get("market_id"),
                "t": row.get("t"),
                "team_a": row.get("team_a"),
                "team_b": row.get("team_b"),
                "label": label,
            })
            features.append(feature_values)
            labels.append(label)
            group_ids.append(group_id)

    return metadata, features, labels, group_ids


def split_by_group(
    metadata: list[dict],
    features: list[list[float]],
    labels: list[int],
    group_ids: list[str],
    train_fraction: float = 0.8,
) -> tuple[list[int], list[int]]:
    event_times: dict[str, int] = {}
    for row, group_id in zip(metadata, group_ids):
        timestamp = int(row.get("t", 0))
        event_times.setdefault(group_id, timestamp)
        event_times[group_id] = min(event_times[group_id], timestamp)

    ordered_groups = sorted(event_times, key=lambda group_id: event_times[group_id])
    split_count = int(len(ordered_groups) * train_fraction)
    split_count = max(1, min(len(ordered_groups) - 1, split_count))
    train_groups = set(ordered_groups[:split_count])
    test_groups = set(ordered_groups[split_count:])
    train_indices = [i for i, group_id in enumerate(group_ids) if group_id in train_groups]
    test_indices = [i for i, group_id in enumerate(group_ids) if group_id in test_groups]
    if not test_indices:
        test_indices = train_indices[:]
    return train_indices, test_indices


def fit_logistic(
    features: list[list[float]],
    labels: list[int],
    l2: float = 1.0,
) -> tuple[list[float], list[float], list[float]]:
    matrix = np.asarray(features, dtype=float)
    target = np.asarray(labels, dtype=float)
    means = matrix.mean(axis=0)
    stds = matrix.std(axis=0)
    stds[stds < 1e-8] = 1.0
    normalized = (matrix - means) / stds
    design = np.column_stack([np.ones(len(normalized)), normalized])

    def objective(weights: np.ndarray) -> tuple[float, np.ndarray]:
        logits = design @ weights
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -35.0, 35.0)))
        loss = -np.mean(target * np.log(probabilities + 1e-12) + (1.0 - target) * np.log(1.0 - probabilities + 1e-12))
        penalty = 0.5 * l2 * np.sum(weights[1:] ** 2)
        gradient = design.T @ (probabilities - target) / len(target)
        gradient[1:] += l2 * weights[1:]
        return float(loss + penalty), gradient

    result = minimize(
        objective,
        np.zeros(design.shape[1]),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 500},
    )
    weights = result.x
    return weights.tolist(), means.tolist(), stds.tolist()


def predict_proba(model: dict, feature_values: list[float]) -> float:
    means = model["means"]
    stds = model["stds"]
    normalized = [
        (value - mean) / (std if std > 1e-8 else 1.0)
        for value, mean, std in zip(feature_values, means, stds)
    ]
    logits = model["intercept"] + sum(
        weight * value for weight, value in zip(model["weights"], normalized)
    )
    return sigmoid(logits)


def evaluate(
    model: dict,
    features: list[list[float]],
    labels: list[int],
) -> dict:
    predictions = [predict_proba(model, feature_values) for feature_values in features]
    y_true = np.asarray(labels, dtype=float)
    y_pred = np.asarray(predictions, dtype=float)
    log_loss = float(-np.mean(y_true * np.log(y_pred + 1e-12) + (1.0 - y_true) * np.log(1.0 - y_pred + 1e-12)))
    brier = float(np.mean((y_pred - y_true) ** 2))
    accuracy = float(np.mean((y_pred >= 0.5).astype(float) == y_true))
    auc = compute_auc(labels, predictions)
    calibration = compute_calibration(labels, predictions)
    return {
        "rows": len(labels),
        "log_loss": round(log_loss, 6),
        "brier_score": round(brier, 6),
        "accuracy": round(accuracy, 6),
        "auc": round(auc, 6) if auc is not None else None,
        "calibration": calibration,
    }


def compute_auc(labels: list[int], predictions: list[float]) -> Optional[float]:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ranked = sorted(range(len(predictions)), key=lambda index: predictions[index])
    rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index
        while end + 1 < len(ranked) and predictions[ranked[end + 1]] == predictions[ranked[index]]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            if labels[ranked[position]] == 1:
                rank_sum += average_rank
        index = end + 1
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def compute_calibration(labels: list[int], predictions: list[float]) -> list[dict]:
    bins = [(i / 10.0, (i + 1) / 10.0) for i in range(10)]
    table = []
    for low, high in bins:
        indices = [
            index for index, prediction in enumerate(predictions)
            if (low <= prediction < high) or (high == 1.0 and prediction == 1.0)
        ]
        if not indices:
            continue
        table.append({
            "bucket": f"{low:.1f}-{high:.1f}",
            "count": len(indices),
            "predicted_mean": round(sum(predictions[index] for index in indices) / len(indices), 4),
            "actual_mean": round(sum(labels[index] for index in indices) / len(indices), 4),
        })
    return table


def save_json(path: str, data: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(target)


def save_predictions(path: str, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def train_model(
    input_path: str,
    model_path: str,
    metrics_path: str,
    predictions_path: str,
    train_fraction: float,
    l2: float,
    include_pre_game: bool,
    max_minutes: int,
) -> dict:
    raw_rows = read_jsonl(input_path)
    filtered_rows = filter_rows(raw_rows, include_pre_game=include_pre_game, max_minutes=max_minutes)
    metadata, features, labels, group_ids = build_features(filtered_rows)
    train_indices, test_indices = split_by_group(
        metadata=metadata,
        features=features,
        labels=labels,
        group_ids=group_ids,
        train_fraction=train_fraction,
    )
    if not train_indices:
        raise ValueError("No training rows found")

    train_features = [features[index] for index in train_indices]
    train_labels = [labels[index] for index in train_indices]
    weights, means, stds = fit_logistic(train_features, train_labels, l2=l2)
    model = {
        "type": "mlb_polymarket_logit",
        "feature_names": FEATURE_NAMES,
        "intercept": weights[0],
        "weights": weights[1:],
        "means": means,
        "stds": stds,
        "l2": l2,
        "include_pre_game": include_pre_game,
        "max_minutes": max_minutes,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "train_rows": len(train_indices),
        "test_rows": len(test_indices),
    }

    train_features_only = [features[index] for index in train_indices]
    test_features = [features[index] for index in test_indices]
    train_labels_only = [labels[index] for index in train_indices]
    test_labels = [labels[index] for index in test_indices]
    baseline_model = {
        "type": "baseline",
        "intercept": 0.0,
        "weights": [1.0] + [0.0] * (len(FEATURE_NAMES) - 1),
        "means": [0.0] * len(FEATURE_NAMES),
        "stds": [1.0] * len(FEATURE_NAMES),
    }
    metrics = {
        "model": evaluate(model, test_features, test_labels),
        "baseline": evaluate(baseline_model, test_features, test_labels),
    }

    prediction_rows = []
    for index in test_indices:
        prediction = predict_proba(model, features[index])
        baseline_prediction = sigmoid(features[index][0])
        prediction_rows.append({
            **metadata[index],
            "model_probability": prediction,
            "market_probability": sigmoid(features[index][0]),
            "baseline_probability": baseline_prediction,
            "label": labels[index],
        })

    model["metrics"] = metrics
    save_json(model_path, model)
    save_json(metrics_path, metrics)
    save_predictions(predictions_path, prediction_rows)
    logger.info(
        "Saved model to %s; train=%d test=%d model_log_loss=%.4f baseline_log_loss=%.4f",
        model_path,
        len(train_indices),
        len(test_indices),
        metrics["model"]["log_loss"],
        metrics["baseline"]["log_loss"],
    )
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train MLB win-probability model from Polymarket prices.")
    parser.add_argument("command", choices=["train"])
    parser.add_argument("--input", default="trades/mlb_dataset.jsonl")
    parser.add_argument("--model", default="trades/mlb_logit_model.json")
    parser.add_argument("--metrics", default="trades/mlb_logit_metrics.json")
    parser.add_argument("--predictions", default="trades/mlb_predictions.jsonl")
    parser.add_argument("--train-fraction", type=float, default=0.8)
    parser.add_argument("--l2", type=float, default=1.0)
    parser.add_argument("--include-pre-game", action="store_true")
    parser.add_argument("--max-minutes", type=int, default=420)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.command == "train":
        train_model(
            input_path=args.input,
            model_path=args.model,
            metrics_path=args.metrics,
            predictions_path=args.predictions,
            train_fraction=args.train_fraction,
            l2=args.l2,
            include_pre_game=args.include_pre_game,
            max_minutes=args.max_minutes,
        )


if __name__ == "__main__":
    main()
