#!/usr/bin/env python3
"""Local HTTP helper for dashboard-triggered training recordings."""

from __future__ import annotations

import argparse
import json
import math
import random
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import TextIOWrapper
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from live_motion_inference import feature_row, load_manifest
from record_sensor_data import normalize_event
from train_motion_model import DEFAULT_SENSOR_ORDER


LABELS = ["no_motion", "ambient_motion", "slap", "earthquake"]
DEFAULT_BASE_URL = "http://192.168.0.196:8000"
MIN_EARTHQUAKE_WINDOW_MAX = 0.05
MIN_EARTHQUAKE_WINDOW_RMS = 0.015


DISPLAY_LABELS = {
    "no_motion": "No motion",
    "ambient_motion": "Non-Earthquake motion detected",
    "slap": "Slap / impact detected",
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

    def sample_counts(self) -> dict[str, int]:
        return {
            label: len(list((self.output_dir / label).glob("*.jsonl")))
            for label in LABELS
        }

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
            if events == 0:
                out_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                with self.lock:
                    self.active = False
                    self.status = {
                        "active": False,
                        "message": "discarded",
                        "label": label,
                        "sessionId": session_id,
                        "events": 0,
                        "reason": "no sensor events captured",
                    }
                return
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
            if events == 0:
                out_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            with self.lock:
                self.active = False
                self.status = {
                    "active": False,
                    "message": "error",
                    "label": label,
                    "sessionId": session_id,
                    "error": str(exc),
                }


class SessionCaptureState:
    def __init__(self, output_dir: str) -> None:
        self.output_dir = Path(output_dir)
        self.lock = threading.Lock()
        self.active = False
        self.session_id = ""
        self.started_at = 0.0
        self.output_path: Path | None = None
        self.meta_path: Path | None = None
        self.output: TextIOWrapper | None = None
        self.sensor_events = 0
        self.prediction_events = 0
        self.counts: dict[str, int] = {}
        self.status: dict[str, Any] = {"active": False, "message": "ready"}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.status)

    def start(self, readiness: dict[str, Any], model_path: str, manifest_path: str, notes: str) -> dict[str, Any]:
        with self.lock:
            if self.active:
                raise RuntimeError("session recording already active")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.session_id = f"session-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            self.started_at = time.time()
            self.output_path = self.output_dir / f"{self.session_id}.jsonl"
            self.meta_path = self.output_dir / f"{self.session_id}.meta.json"
            self.output = self.output_path.open("w", encoding="utf-8")
            self.sensor_events = 0
            self.prediction_events = 0
            self.counts = {}
            metadata = {
                "sessionId": self.session_id,
                "startedAt": self.started_at,
                "source": "local_training_recorder_session_capture",
                "notes": notes,
                "sensorReadiness": readiness,
                "modelPath": model_path,
                "manifestPath": manifest_path,
            }
            self.meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            self.active = True
            self.status = {
                "active": True,
                "message": "recording",
                "sessionId": self.session_id,
                "startedAt": self.started_at,
                "sensorEvents": 0,
                "predictionEvents": 0,
                "counts": {},
                "output": str(self.output_path),
                "metadata": str(self.meta_path),
            }
            return dict(self.status)

    def stop(self, reason: str = "manual") -> dict[str, Any]:
        with self.lock:
            if not self.active:
                return dict(self.status)
            finished_at = time.time()
            if self.output is not None:
                self.output.flush()
                self.output.close()
                self.output = None
            metadata = {
                "sessionId": self.session_id,
                "startedAt": self.started_at,
                "finishedAt": finished_at,
                "duration": finished_at - self.started_at,
                "source": "local_training_recorder_session_capture",
                "reason": reason,
                "sensorEvents": self.sensor_events,
                "predictionEvents": self.prediction_events,
                "counts": self.counts,
                "output": str(self.output_path) if self.output_path else "",
            }
            if self.meta_path is not None:
                self.meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            self.active = False
            self.status = {
                "active": False,
                "message": "complete",
                "sessionId": self.session_id,
                "duration": finished_at - self.started_at,
                "sensorEvents": self.sensor_events,
                "predictionEvents": self.prediction_events,
                "counts": dict(self.counts),
                "output": str(self.output_path) if self.output_path else "",
                "metadata": str(self.meta_path) if self.meta_path else "",
            }
            return dict(self.status)

    def record_sensor(self, event: dict[str, Any], recorded_at: float) -> None:
        with self.lock:
            if not self.active or self.output is None:
                return
            sensor_id = str(event.get("sensorId") or "unknown")
            self.sensor_events += 1
            self.counts[sensor_id] = self.counts.get(sensor_id, 0) + 1
            row = {
                "kind": "sensor",
                "sessionId": self.session_id,
                "recordedAt": recorded_at,
                "elapsed": recorded_at - self.started_at,
                "sensorId": sensor_id,
                "event": event,
            }
            self.output.write(json.dumps(row, separators=(",", ":")) + "\n")
            self.status.update(
                {
                    "sensorEvents": self.sensor_events,
                    "predictionEvents": self.prediction_events,
                    "counts": dict(self.counts),
                }
            )

    def record_prediction(self, prediction: dict[str, Any]) -> None:
        with self.lock:
            if not self.active or self.output is None:
                return
            recorded_at = time.time()
            self.prediction_events += 1
            row = {
                "kind": "prediction",
                "sessionId": self.session_id,
                "recordedAt": recorded_at,
                "elapsed": recorded_at - self.started_at,
                "prediction": prediction,
            }
            self.output.write(json.dumps(row, separators=(",", ":")) + "\n")
            self.output.flush()
            self.status.update(
                {
                    "sensorEvents": self.sensor_events,
                    "predictionEvents": self.prediction_events,
                    "counts": dict(self.counts),
                }
            )


class LivePredictionState:
    def __init__(
        self,
        base_url: str,
        model_path: str,
        manifest_path: str,
        interval: float,
        sensor_stale_seconds: float,
        session_capture: SessionCaptureState,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_path = model_path
        self.manifest_path = manifest_path
        self.interval = interval
        self.sensor_stale_seconds = sensor_stale_seconds
        self.session_capture = session_capture
        self.lock = threading.Lock()
        self.buffer_lock = threading.Lock()
        self.buffer: deque[dict[str, Any]] = deque()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.model: Any | None = None
        self.labels: list[str] = []
        self.raw_history: deque[str] = deque(maxlen=8)
        self.display_state = "no_motion"
        self.state_since = time.time()
        self.earthquake_candidate_since: float | None = None
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

    def sensor_readiness(self) -> dict[str, Any]:
        now = time.time()
        with self.buffer_lock:
            recent = {
                str(event.get("sensorId") or "")
                for event in self.buffer
                if now - float(event.get("recordedAt") or 0.0) <= self.sensor_stale_seconds
            }
            newest_by_sensor: dict[str, float] = {}
            for event in self.buffer:
                sensor_id = str(event.get("sensorId") or "")
                recorded_at = float(event.get("recordedAt") or 0.0)
                if sensor_id:
                    newest_by_sensor[sensor_id] = max(recorded_at, newest_by_sensor.get(sensor_id, 0.0))
        expected = [str(sensor_id) for sensor_id in self.sensor_order]
        connected = [sensor_id for sensor_id in expected if sensor_id in recent]
        missing = [sensor_id for sensor_id in expected if sensor_id not in recent]
        ages = {
            sensor_id: round(now - newest_by_sensor[sensor_id], 2)
            for sensor_id in expected
            if sensor_id in newest_by_sensor
        }
        return {
            "ready": len(missing) == 0 and len(expected) > 0,
            "connected": connected,
            "missing": missing,
            "connectedCount": len(connected),
            "expectedCount": len(expected),
            "staleSeconds": self.sensor_stale_seconds,
            "agesSeconds": ages,
        }

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
            with self.buffer_lock:
                self.buffer.append(
                    {
                        "recordedAt": now,
                        "sensorId": str(event.get("sensorId") or ""),
                        "value": value,
                    }
                )
                while self.buffer and now - self.buffer[0]["recordedAt"] > self.window_seconds * 2:
                    self.buffer.popleft()
            self.session_capture.record_sensor(event, now)
            if now - last_emit >= self.interval:
                last_emit = now
                self._predict()

    def _predict(self) -> None:
        if self.model is None:
            return
        with self.buffer_lock:
            buffered_events = list(self.buffer)
        row = np.array([feature_row(buffered_events, self.sensor_order, self.window_seconds)], dtype=float)
        motion = self._motion_stats(buffered_events)
        probabilities = self.model.predict_proba(row)[0]
        probability_map = {label: float(prob) for label, prob in zip(self.labels, probabilities, strict=True)}
        pairs = sorted(probability_map.items(), key=lambda item: item[1], reverse=True)
        raw_prediction = pairs[0][0]
        self.raw_history.append(raw_prediction)
        prediction = self._display_prediction(raw_prediction, probability_map, motion)
        confidence = probability_map.get(prediction, 0.0)
        if prediction == "ambient_motion":
            confidence = max(confidence, probability_map.get("slap", 0.0))
        payload = {
            "ready": True,
            "prediction": prediction,
            "displayLabel": DISPLAY_LABELS.get(prediction, prediction.replace("_", " ")),
            "confidence": round(confidence, 4),
            "rawPrediction": raw_prediction,
            "rawDisplayLabel": DISPLAY_LABELS.get(raw_prediction, raw_prediction.replace("_", " ")),
            "rawConfidence": round(float(pairs[0][1]), 4),
            "stateAgeSeconds": round(time.time() - self.state_since, 2),
            "probabilities": {label: round(prob, 4) for label, prob in probability_map.items()},
            "displayProbabilities": {
                DISPLAY_LABELS.get(label, label.replace("_", " ")): round(prob, 4)
                for label, prob in probability_map.items()
            },
            "samples": len(buffered_events),
            "updatedAt": time.time(),
            "modelPath": self.model_path,
            "windowSeconds": self.window_seconds,
            "motion": motion,
        }
        with self.lock:
            self.snapshot_payload = payload
        self.session_capture.record_prediction(payload)

    def _motion_stats(self, buffered_events: list[dict[str, Any]]) -> dict[str, Any]:
        now = time.time()
        recent = [
            event
            for event in buffered_events
            if now - float(event.get("recordedAt") or 0.0) <= self.window_seconds
        ]
        values = [float(event.get("value") or 0.0) for event in recent]
        max_value = max(values) if values else 0.0
        rms = math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0
        by_sensor: dict[str, float] = {}
        for event in recent:
            sensor_id = str(event.get("sensorId") or "")
            if not sensor_id:
                continue
            by_sensor[sensor_id] = max(by_sensor.get(sensor_id, 0.0), float(event.get("value") or 0.0))
        active_sensors = sum(1 for sensor_id in self.sensor_order if by_sensor.get(sensor_id, 0.0) >= 0.01)
        earthquake_evidence = max_value >= MIN_EARTHQUAKE_WINDOW_MAX or rms >= MIN_EARTHQUAKE_WINDOW_RMS
        return {
            "max": round(max_value, 6),
            "rms": round(rms, 6),
            "sampleCount": len(values),
            "activeSensorsAbove001": active_sensors,
            "earthquakeEvidence": earthquake_evidence,
            "minEarthquakeMax": MIN_EARTHQUAKE_WINDOW_MAX,
            "minEarthquakeRms": MIN_EARTHQUAKE_WINDOW_RMS,
        }

    def _display_prediction(self, raw_prediction: str, probabilities: dict[str, float], motion: dict[str, Any]) -> str:
        now = time.time()
        earthquake_probability = probabilities.get("earthquake", 0.0)
        no_motion_probability = probabilities.get("no_motion", 0.0)
        ambient_probability = probabilities.get("ambient_motion", 0.0)
        slap_probability = probabilities.get("slap", 0.0)
        recent_earthquake_votes = sum(1 for label in self.raw_history if label == "earthquake")
        has_earthquake_motion = bool(motion.get("earthquakeEvidence"))

        is_earthquake_candidate = raw_prediction == "earthquake" and earthquake_probability >= 0.82 and has_earthquake_motion
        if is_earthquake_candidate:
            if self.earthquake_candidate_since is None:
                self.earthquake_candidate_since = now
        else:
            self.earthquake_candidate_since = None
        candidate_age = 0.0 if self.earthquake_candidate_since is None else now - self.earthquake_candidate_since
        enter_earthquake = (
            is_earthquake_candidate
            and candidate_age >= 2.0
            and recent_earthquake_votes >= 4
        )
        if enter_earthquake and self.display_state != "earthquake":
            self._set_display_state("earthquake", now)
            self.earthquake_candidate_since = None
            self.low_earthquake_since = None
            self.quiet_since = None
            return self.display_state

        if self.display_state == "earthquake":
            state_age = now - self.state_since
            if not has_earthquake_motion and no_motion_probability >= 0.65:
                if self.low_earthquake_since is None:
                    self.low_earthquake_since = now
                if state_age >= 2.0 and now - self.low_earthquake_since >= 1.5:
                    self._set_display_state("no_motion", now)
                    self.low_earthquake_since = None
                    return self.display_state
            if earthquake_probability < 0.35 or not has_earthquake_motion:
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
        elif ambient_probability >= 0.35 or slap_probability >= 0.35 or raw_prediction in {"ambient_motion", "slap"}:
            self._set_display_state("ambient_motion", now)
        else:
            self._set_display_state(raw_prediction, now)
        return self.display_state

    def _set_display_state(self, state: str, now: float) -> None:
        if state != self.display_state:
            self.display_state = state
            self.state_since = now


def make_handler(state: RecorderState, prediction_state: LivePredictionState, session_capture: SessionCaptureState):
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
                self._json(
                    {
                        "ok": True,
                        "recorder": state.snapshot(),
                        "sessionRecorder": session_capture.snapshot(),
                        "sampleCounts": state.sample_counts(),
                        "sensorReadiness": prediction_state.sensor_readiness(),
                    }
                )
                return
            if path == "/prediction":
                self._json({"ok": True, "prediction": prediction_state.snapshot()})
                return
            if path != "/status":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/session/stop":
                self._json({"ok": True, "sessionRecorder": session_capture.stop()})
                return
            if path not in {"/record/start", "/session/start"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if path == "/session/start":
                    readiness = prediction_state.sensor_readiness()
                    if not readiness["ready"]:
                        raise RuntimeError(
                            "sensors not ready: "
                            f"{readiness['connectedCount']}/{readiness['expectedCount']} streaming; "
                            f"missing {', '.join(readiness['missing'])}"
                        )
                    notes = str(payload.get("notes") or "dashboard session recording")
                    session = session_capture.start(
                        readiness,
                        prediction_state.model_path,
                        prediction_state.manifest_path,
                        notes,
                    )
                    self._json({"ok": True, "sessionRecorder": session})
                    return
                label = str(payload.get("label") or "")
                duration = float(payload["duration"]) if "duration" in payload else None
                base_url = str(payload.get("baseUrl") or state.default_base_url).rstrip("/")
                notes = str(payload.get("notes") or "dashboard-triggered recording")
                trigger_demo = bool(payload.get("triggerDemo")) or label == "earthquake"
                readiness = prediction_state.sensor_readiness()
                if not readiness["ready"]:
                    raise RuntimeError(
                        "sensors not ready: "
                        f"{readiness['connectedCount']}/{readiness['expectedCount']} streaming; "
                        f"missing {', '.join(readiness['missing'])}"
                    )
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
    parser.add_argument("--session-output-dir", default="data/session_recordings")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--min-duration", type=float, default=5.0)
    parser.add_argument("--max-duration", type=float, default=10.0)
    parser.add_argument("--model", default="models/window_5s/motion_random_forest.joblib")
    parser.add_argument("--manifest", default="models/window_5s/motion_random_forest.manifest.json")
    parser.add_argument("--prediction-interval", type=float, default=0.5)
    parser.add_argument("--sensor-stale-seconds", type=float, default=3.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_duration <= 0 or args.max_duration < args.min_duration:
        raise ValueError("duration range is invalid")
    state = RecorderState(args.output_dir, args.base_url.rstrip("/"), args.min_duration, args.max_duration)
    session_capture = SessionCaptureState(args.session_output_dir)
    prediction_state = LivePredictionState(
        args.base_url.rstrip("/"),
        args.model,
        args.manifest,
        args.prediction_interval,
        args.sensor_stale_seconds,
        session_capture,
    )
    prediction_state.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state, prediction_state, session_capture))
    print(f"local training recorder listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
