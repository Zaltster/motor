#!/usr/bin/env python3
"""Record labeled sensor events from the vibration tower SSE stream."""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://192.168.0.196:8000"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def get_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_event(event: dict[str, Any], label: str, session_id: str, started_at: float) -> dict[str, Any] | None:
    if event.get("type") != "sensor":
        return None
    if event.get("packet") not in {"accel", "gyro", "angle", "wide61"}:
        return None
    recorded_at = time.time()
    return {
        "label": label,
        "sessionId": session_id,
        "recordedAt": recorded_at,
        "elapsed": recorded_at - started_at,
        "event": event,
    }


def demo_payload(args: argparse.Namespace, duration: float) -> dict[str, Any]:
    return {
        "duration": duration,
        "delayMin": args.demo_delay_min,
        "delayMax": args.demo_delay_max,
        "minDuty": args.min_duty,
        "maxDuty": args.max_duty,
        "cycle": args.cycle,
        "step": args.step,
    }


def parse_demo_sequence(value: str | None) -> list[float]:
    if not value:
        return []
    durations = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        duration = float(part)
        if duration <= 0:
            raise ValueError("--demo-sequence durations must be greater than 0")
        durations.append(duration)
    return durations


def run_demo_sequence(args: argparse.Namespace, durations: list[float], responses: list[dict[str, Any]]) -> None:
    for index, duration in enumerate(durations):
        response = post_json(f"{args.base_url}/api/demo/start", demo_payload(args, duration))
        responses.append({"index": index, "duration": duration, "response": response, "at": time.time()})
        if not response.get("ok"):
            return
        if index < len(durations) - 1:
            wait_for_demo_complete(args.base_url, duration + 8.0)
            if args.demo_gap > 0:
                time.sleep(args.demo_gap)


def wait_for_demo_complete(base_url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    busy_states = {"armed", "waiting_random_delay", "running"}
    while time.time() < deadline:
        try:
            state = get_json(f"{base_url}/health", timeout=3.0).get("demo", {}).get("state")
        except Exception:
            state = None
        if state and state not in busy_states:
            return
        time.sleep(0.25)


def record(args: argparse.Namespace) -> int:
    session_id = args.session_id or f"{args.label}-{utc_stamp()}"
    out_dir = Path(args.output_dir) / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{session_id}.jsonl"
    meta_path = out_dir / f"{session_id}.meta.json"

    demo_durations = parse_demo_sequence(args.demo_sequence)
    if args.trigger_demo and not demo_durations:
        demo_durations = [args.demo_duration]
    demo_responses: list[dict[str, Any]] = []

    metadata = {
        "sessionId": session_id,
        "label": args.label,
        "baseUrl": args.base_url,
        "startedAt": time.time(),
        "duration": args.duration,
        "triggerDemo": bool(demo_durations),
        "demoSequence": demo_durations,
        "demoGap": args.demo_gap,
        "demoResponses": demo_responses,
        "notes": args.notes,
    }
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    event_count = 0
    started_at = time.time()
    deadline = started_at + args.duration
    request = urllib.request.Request(f"{args.base_url}/events", headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=args.duration + 20) as response, out_path.open(
        "w", encoding="utf-8"
    ) as output:
        demo_thread = None
        if demo_durations:
            demo_thread = threading.Thread(
                target=run_demo_sequence,
                args=(args, demo_durations, demo_responses),
                name="demo-sequence",
                daemon=True,
            )
            demo_thread.start()
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
            sample = normalize_event(event, args.label, session_id, started_at)
            if sample is None:
                continue
            output.write(json.dumps(sample, separators=(",", ":")) + "\n")
            event_count += 1
            sensor_id = str(event.get("sensorId") or "unknown")
            counts[sensor_id] = counts.get(sensor_id, 0) + 1
        if demo_thread:
            demo_thread.join(timeout=2.0)

    metadata["finishedAt"] = time.time()
    metadata["demoResponses"] = demo_responses
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    summary = {
        "sessionId": session_id,
        "label": args.label,
        "output": str(out_path),
        "metadata": str(meta_path),
        "events": event_count,
        "counts": counts,
    }
    print(json.dumps(summary, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record labeled vibration tower sensor data.")
    parser.add_argument("label", choices=["no_motion", "ambient_motion", "earthquake"])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--duration", type=float, default=45.0)
    parser.add_argument("--output-dir", default="data/recordings")
    parser.add_argument("--session-id")
    parser.add_argument("--notes", default="")
    parser.add_argument("--trigger-demo", action="store_true")
    parser.add_argument("--demo-sequence", help="Comma-separated demo burst durations, for example 20,25.")
    parser.add_argument("--demo-gap", type=float, default=0.0)
    parser.add_argument("--demo-duration", type=float, default=20.0)
    parser.add_argument("--demo-delay-min", type=float, default=0.0)
    parser.add_argument("--demo-delay-max", type=float, default=0.0)
    parser.add_argument("--min-duty", type=float, default=0.05)
    parser.add_argument("--max-duty", type=float, default=1.0)
    parser.add_argument("--cycle", type=float, default=0.2)
    parser.add_argument("--step", type=float, default=0.25)
    return parser.parse_args()


def main() -> int:
    try:
        return record(parse_args())
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
