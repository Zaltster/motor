#!/usr/bin/env python3
"""Local HTTP helper for dashboard-triggered training recordings."""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from live_motion_inference import feature_row, load_manifest
from record_sensor_data import normalize_event
from train_motion_model import DEFAULT_SENSOR_ORDER


LABELS = {"no_motion", "ambient_motion", "earthquake"}
DEFAULT_BASE_URL = "http://192.168.0.196:8000"


DISPLAY_LABELS = {
    "no_motion": "No motion",
    "ambient_motion": "Non-Earthquake motion detected",
    "earthquake": "Earthquake detected",
}


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class RecorderState:
    def __init__(self, output_dir: str, default_base_url: str, min_duration: float, max_duration: float) -> None:
        self.output_dir = Path(output_dir)
        self.default_base_url = default_base_url
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.lock = threading.Lock()
        self.active = False
        self.status: dict[str, Any] = {"active": False, "message": "ready"}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.status)

    def start(self, label: str, duration: float | None, base_url: str, notes: str, trigger_demo: bool) -> dict[str, Any]:
        if label not in LABELS:
            raise ValueError(f"unknown label: {label}")
        if duration is None:
            duration = random.uniform(self.min_duration, self.max_duration)
        if duration <= 0 or duration > 300:
            raise ValueError("duration must be between 0 and 300 seconds")
        with self.lock:
            if self.active:
                raise RuntimeError("recording already active")
            self.active = True
            session_id = f"{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            self.status = {
                "active": True,
                "message": "recording",
                "label": label,
                "sessionId": session_id,
                "duration": duration,
                "triggerDemo": trigger_demo,
                "startedAt": time.time(),
                "events": 0,
                "counts": {},
            }
        thread = threading.Thread(
            target=self._record,
            args=(label, session_id, duration, base_url, notes, trigger_demo),
            name="local-training-recording",
            daemon=True,
        )
        thread.start()
        return self.snapshot()

    def _record(self, label: str, session_id: str, duration: float, base_url: str, notes: str, trigger_demo: bool) -> None:
        out_dir = self.output_dir / label
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{session_id}.jsonl"
        meta_path = out_dir / f"{session_id}.meta.json"
        counts: dict[str, int] = {}
        events = 0
        started_at = time.time()
        demo_response = None
        metadata = {
            "sessionId": session_id,
            "label": label,
            "baseUrl": base_url,
            "startedAt": started_at,
            "duration": duration,
            "triggerDemo": trigger_demo,
            "demoResponse": demo_response,
            "notes": notes,
            "source": "local_training_recorder",
        }
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        try:
            request = urllib.request.Request(f"{base_url}/events", headers={"Accept": "text/event-stream"})
            deadline = started_at + duration
            with urllib.request.urlopen(request, timeout=duration + 20) as response, out_path.open(
                "w", encoding="utf-8"
            ) as output:
                if trigger_demo:
                    demo_response = post_json(
                        f"{base_url}/api/demo/start",
                        {
                            "duration": duration,
                            "delayMin": 0,
                            "delayMax": 0,
                            "minDuty": 0.05,
                            "maxDuty": 1,
                            "cycle": 0.2,
                            "step": 0.25,
                        },
                    )
                    metadata["demoResponse"] = demo_response
                    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                while time.time() < deadline:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    sample = normalize_event(event, label, session_id, started_at)
                    if sample is None:
                        continue
                    output.write(json.dumps(sample, separators=(",", ":")) + "\n")
                    events += 1
                    sensor_id = str(event.get("sensorId") or "unknown")
                    counts[sensor_id] = counts.get(sensor_id, 0) + 1
                    with self.lock:
                        self.status.update({"events": events, "counts": dict(counts)})
            metadata["finishedAt"] = time.time()
            metadata["events"] = events
            metadata["counts"] = counts
            meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            with self.lock:
                self.active = False
                self.status = {
                    "active": False,
                    "message": "complete",
                    "label": label,
                    "sessionId": session_id,
                    "events": events,
                    "counts": counts,
                    "output": str(out_path),
                    "metadata": str(meta_path),
                }
        except Exception as exc:
            with self.lock:
                self.active = False
                self.status = {
                    "active": False,
                    "message": "error",
                    "label": label,
                    "sessionId": session_id,
                    "error": str(exc),
                }


class LivePredictionState:
    def __init__(self, base_url: str, model_path: str, manifest_path: str, interval: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_path = model_path
        self.manifest_path = manifest_path
        self.interval = interval
        self.lock = threading.Lock()
        self.buffer: deque[dict[str, Any]] = deque()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.model: Any | None = None
        self.labels: list[str] = []
        self.raw_history: deque[str] = deque(maxlen=8)
        self.display_state = "no_motion"
        self.state_since = time.time()
        self.low_earthquake_since: float | None = None
        self.quiet_since: float | None = None
        self.sensor_order = DEFAULT_SENSOR_ORDER
        self.window_seconds = 5.0
        self.snapshot_payload: dict[str, Any] = {
            "ready": False,
            "message": "loading model",
            "modelPath": model_path,
            "manifestPath": manifest_path,
        }

    def start(self) -> None:
        try:
            self.model = joblib.load(self.model_path)
            manifest = load_manifest(self.manifest_path)
            self.labels = list(getattr(self.model, "classes_", []))
            self.sensor_order = list(manifest.get("sensorOrder") or DEFAULT_SENSOR_ORDER)
            self.window_seconds = float(manifest.get("windowSeconds") or 5.0)
            with self.lock:
                self.snapshot_payload = {
                    "ready": False,
                    "message": "waiting for sensor samples",
                    "modelPath": self.model_path,
                    "manifestPath": self.manifest_path,
                    "windowSeconds": self.window_seconds,
                    "sensorOrder": self.sensor_order,
                    "samples": 0,
                }
        except Exception as exc:
            with self.lock:
                self.snapshot_payload = {
                    "ready": False,
                    "message": "model load failed",
                    "error": str(exc),
                    "modelPath": self.model_path,
                    "manifestPath": self.manifest_path,
                }
            return

        self.thread = threading.Thread(target=self._run, name="live-motion-prediction", daemon=True)
        self.thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.snapshot_payload)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                request = urllib.request.Request(f"{self.base_url}/events", headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    self._read_stream(response)
            except Exception as exc:
                with self.lock:
                    current = dict(self.snapshot_payload)
                    current.update({"ready": False, "message": "prediction stream disconnected", "error": str(exc)})
                    self.snapshot_payload = current
                time.sleep(1.0)

    def _read_stream(self, response: Any) -> None:
        last_emit = 0.0
        while not self.stop_event.is_set():
            raw_line = response.readline()
            if not raw_line:
                return
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("type") != "sensor" or event.get("packet") not in {"accel", "wide61"}:
                continue
            now = time.time()
            try:
                value = float(event.get("accelMag") or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            self.buffer.append(
                {
                    "recordedAt": now,
                    "sensorId": str(event.get("sensorId") or ""),
                    "value": value,
                }
            )
            while self.buffer and now - self.buffer[0]["recordedAt"] > self.window_seconds * 2:
                self.buffer.popleft()
            if now - last_emit >= self.interval:
                last_emit = now
                self._predict()

    def _predict(self) -> None:
        if self.model is None:
            return
        row = np.array([feature_row(self.buffer, self.sensor_order, self.window_seconds)], dtype=float)
        probabilities = self.model.predict_proba(row)[0]
        probability_map = {label: float(prob) for label, prob in zip(self.labels, probabilities, strict=True)}
        pairs = sorted(probability_map.items(), key=lambda item: item[1], reverse=True)
        raw_prediction = pairs[0][0]
        self.raw_history.append(raw_prediction)
        prediction = self._display_prediction(raw_prediction, probability_map)
        payload = {
            "ready": True,
            "prediction": prediction,
            "displayLabel": DISPLAY_LABELS.get(prediction, prediction.replace("_", " ")),
            "confidence": round(probability_map.get(prediction, 0.0), 4),
            "rawPrediction": raw_prediction,
            "rawDisplayLabel": DISPLAY_LABELS.get(raw_prediction, raw_prediction.replace("_", " ")),
            "rawConfidence": round(float(pairs[0][1]), 4),
            "stateAgeSeconds": round(time.time() - self.state_since, 2),
            "probabilities": {label: round(prob, 4) for label, prob in probability_map.items()},
            "displayProbabilities": {
                DISPLAY_LABELS.get(label, label.replace("_", " ")): round(prob, 4)
                for label, prob in probability_map.items()
            },
            "samples": len(self.buffer),
            "updatedAt": time.time(),
            "modelPath": self.model_path,
            "windowSeconds": self.window_seconds,
        }
        with self.lock:
            self.snapshot_payload = payload

    def _display_prediction(self, raw_prediction: str, probabilities: dict[str, float]) -> str:
        now = time.time()
        earthquake_probability = probabilities.get("earthquake", 0.0)
        no_motion_probability = probabilities.get("no_motion", 0.0)
        ambient_probability = probabilities.get("ambient_motion", 0.0)
        recent_earthquake_votes = sum(1 for label in self.raw_history if label == "earthquake")

        enter_earthquake = earthquake_probability >= 0.92 or (
            earthquake_probability >= 0.78 and recent_earthquake_votes >= 2
        )
        if enter_earthquake and self.display_state != "earthquake":
            self._set_display_state("earthquake", now)
            self.low_earthquake_since = None
            self.quiet_since = None
            return self.display_state

        if self.display_state == "earthquake":
            state_age = now - self.state_since
            if earthquake_probability < 0.35:
                if self.low_earthquake_since is None:
                    self.low_earthquake_since = now
            else:
                self.low_earthquake_since = None
            if state_age >= 8.0 and self.low_earthquake_since is not None and now - self.low_earthquake_since >= 4.0:
                if no_motion_probability >= 0.65:
                    self._set_display_state("no_motion", now)
                else:
                    self._set_display_state("ambient_motion", now)
                self.low_earthquake_since = None
            return self.display_state

        if no_motion_probability >= 0.65 and raw_prediction == "no_motion":
            if self.quiet_since is None:
                self.quiet_since = now
            if self.display_state == "ambient_motion" and now - self.quiet_since < 2.0:
                return self.display_state
            self._set_display_state("no_motion", now)
            return self.display_state
        self.quiet_since = None

        if raw_prediction == "earthquake":
            self._set_display_state("ambient_motion", now)
        elif ambient_probability >= 0.35 or raw_prediction == "ambient_motion":
            self._set_display_state("ambient_motion", now)
        else:
            self._set_display_state(raw_prediction, now)
        return self.display_state

    def _set_display_state(self, state: str, now: float) -> None:
        if state != self.display_state:
            self.display_state = state
            self.state_since = now


def make_handler(state: RecorderState, prediction_state: LivePredictionState):
    class Handler(BaseHTTPRequestHandler):
        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            super().end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", flush=True)

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/status":
                self._json({"ok": True, "recorder": state.snapshot()})
                return
            if path == "/prediction":
                self._json({"ok": True, "prediction": prediction_state.snapshot()})
                return
            if path != "/status":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path != "/record/start":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                label = str(payload.get("label") or "")
                duration = float(payload["duration"]) if "duration" in payload else None
                base_url = str(payload.get("baseUrl") or state.default_base_url).rstrip("/")
                notes = str(payload.get("notes") or "dashboard-triggered recording")
                trigger_demo = bool(payload.get("triggerDemo")) or label == "earthquake"
                recorder = state.start(label, duration, base_url, notes, trigger_demo)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "recorder": recorder})

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local training recorder API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", default="data/recordings")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--min-duration", type=float, default=5.0)
    parser.add_argument("--max-duration", type=float, default=10.0)
    parser.add_argument("--model", default="models/window_5s/motion_random_forest.joblib")
    parser.add_argument("--manifest", default="models/window_5s/motion_random_forest.manifest.json")
    parser.add_argument("--prediction-interval", type=float, default=0.5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_duration <= 0 or args.max_duration < args.min_duration:
        raise ValueError("duration range is invalid")
    state = RecorderState(args.output_dir, args.base_url.rstrip("/"), args.min_duration, args.max_duration)
    prediction_state = LivePredictionState(
        args.base_url.rstrip("/"),
        args.model,
        args.manifest,
        args.prediction_interval,
    )
    prediction_state.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state, prediction_state))
    print(f"local training recorder listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
