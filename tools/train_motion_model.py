#!/usr/bin/env python3
"""Train a small Random Forest classifier from labeled tower recordings."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import LeaveOneGroupOut


LABELS = ["no_motion", "ambient_motion", "earthquake"]
DEFAULT_SENSOR_ORDER = [
    "D0:99:8C:48:4D:38",
    "D1:6E:A1:15:03:57",
    "FA:91:56:1E:26:15",
]
THRESHOLDS = [0.01, 0.05, 0.10, 0.50]
FEATURE_VERSION = "rf_rhythm_v2"
FREQUENCY_BANDS = [(0.5, 2.0), (2.0, 5.0), (5.0, 10.0)]
RESAMPLE_HZ = 20.0


def load_recording(path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError(f"empty recording: {path}")
    label = str(rows[0]["label"])
    session_id = str(rows[0]["sessionId"])
    events = []
    for row in rows:
        event = row["event"]
        events.append(
            {
                "sessionId": session_id,
                "label": label,
                "elapsed": float(row["elapsed"]),
                "sensorId": str(event.get("sensorId") or ""),
                "value": float(event.get("accelMag") or 0.0),
            }
        )
    return label, session_id, events


def sensor_stats(values: list[float]) -> dict[str, float]:
    if not values:
        stats = {"count": 0.0, "mean": 0.0, "max": 0.0, "std": 0.0, "rms": 0.0, "p95": 0.0}
        for threshold in THRESHOLDS:
            stats[f"frac_ge_{threshold:g}"] = 0.0
        return stats
    arr = np.array(values, dtype=float)
    stats = {
        "count": float(len(values)),
        "mean": float(np.mean(arr)),
        "max": float(np.max(arr)),
        "std": float(np.std(arr)),
        "rms": float(math.sqrt(float(np.mean(arr * arr)))),
        "p95": float(np.percentile(arr, 95)),
    }
    for threshold in THRESHOLDS:
        stats[f"frac_ge_{threshold:g}"] = float(np.mean(arr >= threshold))
    return stats


def count_peaks(values: list[float], threshold: float) -> float:
    if len(values) < 3:
        return 0.0
    peaks = 0
    for index in range(1, len(values) - 1):
        if values[index] >= threshold and values[index] >= values[index - 1] and values[index] > values[index + 1]:
            peaks += 1
    return float(peaks)


def linear_trend(times: list[float], values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    x = np.array(times, dtype=float)
    y = np.array(values, dtype=float)
    x = x - float(np.mean(x))
    denominator = float(np.sum(x * x))
    if denominator <= 1e-9:
        return 0.0
    return float(np.sum(x * (y - float(np.mean(y)))) / denominator)


def resample_values(points: list[tuple[float, float]], start: float, end: float, hz: float = RESAMPLE_HZ) -> tuple[np.ndarray, np.ndarray]:
    duration = max(0.0, end - start)
    count = max(2, int(duration * hz))
    grid = np.linspace(start, end, count, endpoint=False)
    if not points:
        return grid, np.zeros(count, dtype=float)
    times = np.array([time for time, _value in points], dtype=float)
    values = np.array([value for _time, value in points], dtype=float)
    if len(points) == 1:
        return grid, np.full(count, float(values[0]), dtype=float)
    order = np.argsort(times)
    times = times[order]
    values = values[order]
    return grid, np.interp(grid, times, values, left=0.0, right=0.0)


def spectral_stats(values: np.ndarray, hz: float = RESAMPLE_HZ) -> dict[str, float]:
    if len(values) < 4 or float(np.max(values)) <= 0.0:
        return {
            "dominant_freq": 0.0,
            "spectral_centroid": 0.0,
            "low_band": 0.0,
            "mid_band": 0.0,
            "high_band": 0.0,
        }
    centered = values - float(np.mean(values))
    spectrum = np.abs(np.fft.rfft(centered)) ** 2
    freqs = np.fft.rfftfreq(len(centered), d=1.0 / hz)
    if len(spectrum) <= 1:
        return {
            "dominant_freq": 0.0,
            "spectral_centroid": 0.0,
            "low_band": 0.0,
            "mid_band": 0.0,
            "high_band": 0.0,
        }
    spectrum[0] = 0.0
    total = float(np.sum(spectrum))
    if total <= 1e-12:
        return {
            "dominant_freq": 0.0,
            "spectral_centroid": 0.0,
            "low_band": 0.0,
            "mid_band": 0.0,
            "high_band": 0.0,
        }
    bands = []
    for low, high in FREQUENCY_BANDS:
        mask = (freqs >= low) & (freqs < high)
        bands.append(float(np.sum(spectrum[mask]) / total))
    return {
        "dominant_freq": float(freqs[int(np.argmax(spectrum))]),
        "spectral_centroid": float(np.sum(freqs * spectrum) / total),
        "low_band": bands[0],
        "mid_band": bands[1],
        "high_band": bands[2],
    }


def correlation_and_lag(a: np.ndarray, b: np.ndarray, hz: float = RESAMPLE_HZ, max_lag_seconds: float = 0.5) -> tuple[float, float]:
    if len(a) < 4 or len(b) < 4:
        return 0.0, 0.0
    a_centered = a - float(np.mean(a))
    b_centered = b - float(np.mean(b))
    denominator = float(np.linalg.norm(a_centered) * np.linalg.norm(b_centered))
    if denominator <= 1e-12:
        return 0.0, 0.0
    max_lag = max(1, int(max_lag_seconds * hz))
    best_corr = 0.0
    best_lag = 0
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            left = a_centered[:lag]
            right = b_centered[-lag:]
        elif lag > 0:
            left = a_centered[lag:]
            right = b_centered[:-lag]
        else:
            left = a_centered
            right = b_centered
        if len(left) < 4 or len(right) < 4:
            continue
        local_denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        if local_denominator <= 1e-12:
            continue
        corr = float(np.sum(left * right) / local_denominator)
        if abs(corr) > abs(best_corr):
            best_corr = corr
            best_lag = lag
    return best_corr, float(best_lag / hz)


def make_feature_names(sensor_order: list[str]) -> list[str]:
    per_sensor = (
        ["count", "mean", "max", "std", "rms", "p95"]
        + [f"frac_ge_{threshold:g}" for threshold in THRESHOLDS]
        + [
            "trend",
            "early_mean",
            "late_mean",
            "late_early_ratio",
            "burstiness",
            "peaks_per_second",
            "duration_ge_0_01",
            "duration_ge_0_05",
            "dominant_freq",
            "spectral_centroid",
            "low_band",
            "mid_band",
            "high_band",
        ]
    )
    names = []
    for index, _sensor_id in enumerate(sensor_order):
        names.extend(f"s{index}_{name}" for name in per_sensor)
    names.extend(
        [
            "active_sensors_0_01",
            "active_sensors_0_05",
            "total_mean",
            "total_max",
            "total_rms",
            "mean_spread",
            "max_spread",
            "max_to_mean_ratio",
            "coherence_mean_corr",
            "coherence_max_corr",
            "coherence_min_abs_lag",
            "coherence_mean_abs_lag",
            "total_trend",
            "total_burstiness",
            "total_peaks_per_second",
            "total_dominant_freq",
            "total_low_band",
            "total_mid_band",
            "total_high_band",
        ]
    )
    return names


def make_window_feature_row(
    by_sensor: dict[str, list[tuple[float, float]]],
    sensor_order: list[str],
    start: float,
    end: float,
) -> list[float]:
    row = []
    sensor_means = []
    sensor_maxes = []
    active_001 = 0
    active_005 = 0
    all_values = []
    all_points = []
    resampled: dict[str, np.ndarray] = {}
    duration = max(1e-9, end - start)

    for sensor_id in sensor_order:
        points = [(elapsed, value) for elapsed, value in by_sensor.get(sensor_id, []) if start <= elapsed < end]
        values = [value for _elapsed, value in points]
        times = [elapsed - start for elapsed, _value in points]
        stats = sensor_stats(values)
        row.extend(stats[name] for name in ["count", "mean", "max", "std", "rms", "p95"])
        row.extend(stats[f"frac_ge_{threshold:g}"] for threshold in THRESHOLDS)
        sensor_means.append(stats["mean"])
        sensor_maxes.append(stats["max"])
        active_001 += int(stats["max"] >= 0.01)
        active_005 += int(stats["max"] >= 0.05)
        all_values.extend(values)
        all_points.extend(points)

        midpoint = start + duration / 2
        early_values = [value for elapsed, value in points if elapsed < midpoint]
        late_values = [value for elapsed, value in points if elapsed >= midpoint]
        early_mean = float(np.mean(early_values)) if early_values else 0.0
        late_mean = float(np.mean(late_values)) if late_values else 0.0
        peak_count = count_peaks(values, max(0.01, stats["mean"] + stats["std"]))
        _grid, series = resample_values(points, start, end)
        resampled[sensor_id] = series
        spectrum = spectral_stats(series)
        row.extend(
            [
                linear_trend(times, values),
                early_mean,
                late_mean,
                late_mean / max(early_mean, 1e-9),
                stats["max"] / max(stats["rms"], 1e-9),
                peak_count / duration,
                stats["frac_ge_0.01"] * duration,
                stats["frac_ge_0.05"] * duration,
                spectrum["dominant_freq"],
                spectrum["spectral_centroid"],
                spectrum["low_band"],
                spectrum["mid_band"],
                spectrum["high_band"],
            ]
        )

    total_stats = sensor_stats(all_values)
    mean_nonzero = max(1e-9, sum(sensor_means) / max(1, len(sensor_means)))
    correlations = []
    abs_lags = []
    for left_index, left_sensor in enumerate(sensor_order):
        for right_sensor in sensor_order[left_index + 1 :]:
            corr, lag = correlation_and_lag(resampled.get(left_sensor, np.array([])), resampled.get(right_sensor, np.array([])))
            correlations.append(abs(corr))
            abs_lags.append(abs(lag))
    total_points = sorted(all_points)
    total_times = [elapsed - start for elapsed, _value in total_points]
    total_values = [value for _elapsed, value in total_points]
    total_peak_count = count_peaks(total_values, max(0.01, total_stats["mean"] + total_stats["std"]))
    _grid, total_series = resample_values(total_points, start, end)
    total_spectrum = spectral_stats(total_series)
    row.extend(
        [
            float(active_001),
            float(active_005),
            total_stats["mean"],
            total_stats["max"],
            total_stats["rms"],
            max(sensor_means) - min(sensor_means),
            max(sensor_maxes) - min(sensor_maxes),
            max(sensor_maxes) / mean_nonzero,
            float(np.mean(correlations)) if correlations else 0.0,
            float(np.max(correlations)) if correlations else 0.0,
            float(np.min(abs_lags)) if abs_lags else 0.0,
            float(np.mean(abs_lags)) if abs_lags else 0.0,
            linear_trend(total_times, total_values),
            total_stats["max"] / max(total_stats["rms"], 1e-9),
            total_peak_count / duration,
            total_spectrum["dominant_freq"],
            total_spectrum["low_band"],
            total_spectrum["mid_band"],
            total_spectrum["high_band"],
        ]
    )
    return row


def build_window_features(
    events: list[dict[str, Any]],
    sensor_order: list[str],
    window_seconds: float,
    stride_seconds: float,
) -> tuple[list[list[float]], list[str], list[str]]:
    if not events:
        return [], [], []
    label = str(events[0]["label"])
    session_id = str(events[0]["sessionId"])
    by_sensor: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for event in events:
        by_sensor[event["sensorId"]].append((event["elapsed"], event["value"]))
    max_elapsed = max(event["elapsed"] for event in events)
    features: list[list[float]] = []
    labels: list[str] = []
    groups: list[str] = []
    start = 0.0
    while start + window_seconds <= max_elapsed + 1e-9:
        end = start + window_seconds
        features.append(make_window_feature_row(by_sensor, sensor_order, start, end))
        labels.append(label)
        groups.append(session_id)
        start += stride_seconds
    return features, labels, groups


def discover_recordings(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*/*.jsonl") if path.parent.name in LABELS)


def print_confusion(y_true: list[str], y_pred: list[str]) -> None:
    matrix = confusion_matrix(y_true, y_pred, labels=LABELS)
    print("Confusion matrix rows=true cols=pred")
    print("labels:", ", ".join(LABELS))
    for label, row in zip(LABELS, matrix.tolist(), strict=True):
        print(f"{label:15s} {row}")


def train(args: argparse.Namespace) -> int:
    recording_paths = discover_recordings(Path(args.recordings_dir))
    if not recording_paths:
        raise RuntimeError(f"no recordings found under {args.recordings_dir}")

    sensor_order = args.sensor_order or DEFAULT_SENSOR_ORDER
    feature_names = make_feature_names(sensor_order)
    all_features: list[list[float]] = []
    all_labels: list[str] = []
    all_groups: list[str] = []
    session_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()

    for path in recording_paths:
        label, session_id, events = load_recording(path)
        features, labels, groups = build_window_features(events, sensor_order, args.window, args.stride)
        all_features.extend(features)
        all_labels.extend(labels)
        all_groups.extend(groups)
        session_counts[session_id] = len(features)
        label_counts[label] += len(features)

    x = np.array(all_features, dtype=float)
    y = np.array(all_labels)
    groups = np.array(all_groups)
    print(f"Recordings: {len(recording_paths)}")
    print(f"Windows: {len(y)}")
    print("Window labels:", dict(label_counts))
    print("Sessions:", dict(session_counts))

    logo = LeaveOneGroupOut()
    y_true: list[str] = []
    y_pred: list[str] = []
    for train_index, test_index in logo.split(x, y, groups):
        model = RandomForestClassifier(
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            min_samples_leaf=args.min_samples_leaf,
            class_weight="balanced",
            random_state=args.seed,
        )
        model.fit(x[train_index], y[train_index])
        y_true.extend(y[test_index].tolist())
        y_pred.extend(model.predict(x[test_index]).tolist())

    print_confusion(y_true, y_pred)
    print(classification_report(y_true, y_pred, labels=LABELS, zero_division=0))

    final_model = RandomForestClassifier(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
        class_weight="balanced",
        random_state=args.seed,
    )
    final_model.fit(x, y)

    importances = sorted(zip(feature_names, final_model.feature_importances_, strict=True), key=lambda pair: pair[1], reverse=True)
    print("Top features:")
    for name, value in importances[:12]:
        print(f"  {name}: {value:.4f}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "motion_random_forest.joblib"
    manifest_path = output_dir / "motion_random_forest.manifest.json"
    joblib.dump(final_model, model_path)
    manifest = {
        "modelType": "RandomForestClassifier",
        "featureVersion": FEATURE_VERSION,
        "labels": LABELS,
        "sensorOrder": sensor_order,
        "featureNames": feature_names,
        "windowSeconds": args.window,
        "strideSeconds": args.stride,
        "recordings": [str(path) for path in recording_paths],
        "parameters": {
            "nEstimators": args.n_estimators,
            "maxDepth": args.max_depth,
            "minSamplesLeaf": args.min_samples_leaf,
            "seed": args.seed,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Saved model: {model_path}")
    print(f"Saved manifest: {manifest_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the vibration tower motion classifier.")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--output-dir", default="models")
    parser.add_argument("--window", type=float, default=2.0)
    parser.add_argument("--stride", type=float, default=0.5)
    parser.add_argument("--n-estimators", type=int, default=100)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--min-samples-leaf", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sensor-order", action="append")
    return parser.parse_args()


def main() -> int:
    return train(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
