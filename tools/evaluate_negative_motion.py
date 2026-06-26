#!/usr/bin/env python3
"""Evaluate false earthquake predictions on non-earthquake recordings."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from train_motion_model import build_window_features, discover_recordings, load_recording


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Report false earthquake predictions on negative recordings.")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--model", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--labels", nargs="+", default=["no_motion", "ambient_motion", "slap"])
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    model = joblib.load(args.model)
    manifest = load_manifest(Path(args.manifest))
    sensor_order = list(manifest["sensorOrder"])
    window_seconds = float(manifest["windowSeconds"])
    stride_seconds = float(manifest.get("strideSeconds") or 0.5)
    classes = list(model.classes_)
    earthquake_index = classes.index("earthquake")

    target_labels = set(args.labels)
    sessions: list[dict[str, Any]] = []
    total_windows = 0
    false_earthquake_windows = 0
    by_label: dict[str, Counter[str]] = {label: Counter() for label in target_labels}

    for path in discover_recordings(Path(args.recordings_dir)):
        label, session_id, events = load_recording(path)
        if label not in target_labels:
            continue
        features, labels, _groups = build_window_features(events, sensor_order, window_seconds, stride_seconds)
        if not features:
            continue
        x = np.array(features, dtype=float)
        predictions = model.predict(x)
        probabilities = model.predict_proba(x)
        prediction_counts = Counter(str(prediction) for prediction in predictions)
        false_count = prediction_counts.get("earthquake", 0)
        max_earthquake_probability = float(np.max(probabilities[:, earthquake_index]))

        total_windows += len(labels)
        false_earthquake_windows += false_count
        by_label[label].update(prediction_counts)
        sessions.append(
            {
                "path": str(path),
                "sessionId": session_id,
                "label": label,
                "windows": len(labels),
                "falseEarthquakeWindows": false_count,
                "falseEarthquakeRate": false_count / len(labels),
                "maxEarthquakeProbability": max_earthquake_probability,
                "predictions": dict(prediction_counts),
            }
        )

    sessions.sort(key=lambda item: (item["falseEarthquakeWindows"], item["maxEarthquakeProbability"]), reverse=True)
    result = {
        "model": args.model,
        "manifest": args.manifest,
        "recordingsDir": args.recordings_dir,
        "labels": sorted(target_labels),
        "windowSeconds": window_seconds,
        "strideSeconds": stride_seconds,
        "sessions": len(sessions),
        "windows": total_windows,
        "falseEarthquakeWindows": false_earthquake_windows,
        "falseEarthquakeRate": 0.0 if total_windows == 0 else false_earthquake_windows / total_windows,
        "predictionsByLabel": {label: dict(counts) for label, counts in sorted(by_label.items())},
        "worstSessions": sessions[:20],
    }

    print(json.dumps(result, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
