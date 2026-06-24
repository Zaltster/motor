#!/usr/bin/env python3
"""Run live Random Forest inference against the vibration tower SSE stream."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import numpy as np
import joblib

from train_motion_model import DEFAULT_SENSOR_ORDER, make_window_feature_row


def feature_row(buffer: deque[dict[str, Any]], sensor_order: list[str], window_seconds: float) -> list[float]:
    now = time.time()
    start = now - window_seconds
    by_sensor: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for event in buffer:
        if now - event["recordedAt"] <= window_seconds:
            by_sensor[event["sensorId"]].append((event["recordedAt"], event["value"]))
    return make_window_feature_row(by_sensor, sensor_order, start, now)


def load_manifest(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(args: argparse.Namespace) -> int:
    model = joblib.load(args.model)
    manifest = load_manifest(args.manifest)
    labels = list(model.classes_)
    sensor_order = manifest.get("sensorOrder") or DEFAULT_SENSOR_ORDER
    window_seconds = float(args.window or manifest.get("windowSeconds") or 2.0)
    buffer: deque[dict[str, Any]] = deque()
    last_emit = 0.0
    request = urllib.request.Request(f"{args.base_url.rstrip('/')}/events", headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=None) as response:
        while True:
            line = response.readline().decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("type") != "sensor" or event.get("packet") not in {"accel", "wide61"}:
                continue
            now = time.time()
            buffer.append(
                {
                    "recordedAt": now,
                    "sensorId": str(event.get("sensorId") or ""),
                    "value": float(event.get("accelMag") or 0.0),
                }
            )
            while buffer and now - buffer[0]["recordedAt"] > window_seconds * 2:
                buffer.popleft()
            if now - last_emit < args.interval:
                continue
            last_emit = now
            row = np.array([feature_row(buffer, sensor_order, window_seconds)], dtype=float)
            probabilities = model.predict_proba(row)[0]
            pairs = sorted(zip(labels, probabilities, strict=True), key=lambda item: item[1], reverse=True)
            print(
                json.dumps(
                    {
                        "prediction": pairs[0][0],
                        "confidence": round(float(pairs[0][1]), 4),
                        "probabilities": {label: round(float(prob), 4) for label, prob in zip(labels, probabilities, strict=True)},
                        "samples": len(buffer),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live motion inference.")
    parser.add_argument("--base-url", default="http://192.168.0.196:8000")
    parser.add_argument("--model", default="models/motion_random_forest.joblib")
    parser.add_argument("--manifest", default="models/motion_random_forest.manifest.json")
    parser.add_argument("--window", type=float)
    parser.add_argument("--interval", type=float, default=0.5)
    return parser.parse_args()


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
