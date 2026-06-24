#!/usr/bin/env python3
"""Inspect session-level errors for the motion Random Forest."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import LeaveOneGroupOut

from train_motion_model import DEFAULT_SENSOR_ORDER, LABELS, build_window_features, discover_recordings, load_recording


def summarize_recording(path: Path) -> dict[str, object]:
    values_by_sensor: dict[str, list[float]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        event = row["event"]
        values_by_sensor[str(event.get("sensorId") or "")].append(float(event.get("accelMag") or 0.0))
    all_values = [value for values in values_by_sensor.values() for value in values]
    return {
        "events": len(all_values),
        "mean": float(np.mean(all_values)) if all_values else 0.0,
        "max": float(np.max(all_values)) if all_values else 0.0,
        "frac_ge_001": float(np.mean(np.array(all_values) >= 0.01)) if all_values else 0.0,
        "frac_ge_005": float(np.mean(np.array(all_values) >= 0.05)) if all_values else 0.0,
        "sensor_max": {sensor: max(values) if values else 0.0 for sensor, values in sorted(values_by_sensor.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze motion model errors.")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--window", type=float, default=2.0)
    parser.add_argument("--stride", type=float, default=0.5)
    parser.add_argument("--n-estimators", type=int, default=150)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--min-samples-leaf", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    paths = discover_recordings(Path(args.recordings_dir))
    features = []
    labels = []
    groups = []
    session_path = {}
    for path in paths:
        _label, session_id, events = load_recording(path)
        session_path[session_id] = path
        x_part, y_part, g_part = build_window_features(events, DEFAULT_SENSOR_ORDER, args.window, args.stride)
        features.extend(x_part)
        labels.extend(y_part)
        groups.extend(g_part)

    x = np.array(features, dtype=float)
    y = np.array(labels)
    g = np.array(groups)
    predictions_by_session: dict[str, Counter[str]] = defaultdict(Counter)
    true_by_session: dict[str, str] = {}
    logo = LeaveOneGroupOut()
    for train_index, test_index in logo.split(x, y, g):
        model = RandomForestClassifier(
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            min_samples_leaf=args.min_samples_leaf,
            class_weight="balanced",
            random_state=args.seed,
        )
        model.fit(x[train_index], y[train_index])
        predictions = model.predict(x[test_index])
        for index, prediction in zip(test_index, predictions, strict=True):
            session = str(g[index])
            predictions_by_session[session][str(prediction)] += 1
            true_by_session[session] = str(y[index])

    rows = []
    for session, counts in sorted(predictions_by_session.items()):
        true_label = true_by_session[session]
        total = sum(counts.values())
        wrong = total - counts[true_label]
        summary = summarize_recording(session_path[session])
        rows.append((wrong / total if total else 0.0, session, true_label, total, counts, summary))

    print("Worst sessions by wrong-window fraction")
    for wrong_frac, session, true_label, total, counts, summary in sorted(rows, reverse=True)[:15]:
        print(
            json.dumps(
                {
                    "session": session,
                    "true": true_label,
                    "windows": total,
                    "wrongFraction": round(wrong_frac, 3),
                    "predictions": dict(counts),
                    "mean": round(float(summary["mean"]), 4),
                    "max": round(float(summary["max"]), 4),
                    "frac_ge_001": round(float(summary["frac_ge_001"]), 3),
                    "frac_ge_005": round(float(summary["frac_ge_005"]), 3),
                    "sensor_max": summary["sensor_max"],
                },
                separators=(",", ":"),
            )
        )

    print("\nAmbient sessions predicted as no_motion")
    for _wrong_frac, session, true_label, total, counts, summary in rows:
        if true_label != "ambient_motion":
            continue
        no_motion = counts.get("no_motion", 0)
        if not no_motion:
            continue
        print(
            json.dumps(
                {
                    "session": session,
                    "windows": total,
                    "noMotionPredictions": no_motion,
                    "ambientPredictions": counts.get("ambient_motion", 0),
                    "earthquakePredictions": counts.get("earthquake", 0),
                    "mean": round(float(summary["mean"]), 4),
                    "max": round(float(summary["max"]), 4),
                    "frac_ge_001": round(float(summary["frac_ge_001"]), 3),
                    "frac_ge_005": round(float(summary["frac_ge_005"]), 3),
                    "sensor_max": summary["sensor_max"],
                },
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
