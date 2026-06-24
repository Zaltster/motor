#!/usr/bin/env python3
"""Browser UI for L298N rumble control and WIT motion sensor telemetry."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import glob
import json
import math
import os
import queue
import random
import shutil
import socketserver
import struct
import subprocess
import termios
import threading
import time
import tty
from collections import deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

from l298n_motor import DryMotor, Motor, pulse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

BAUD_RATES = {
    rate: getattr(termios, f"B{rate}")
    for rate in (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
    if hasattr(termios, f"B{rate}")
}

WIT_NOTIFY_UUIDS = {
    "0000ffe4-0000-1000-8000-00805f9a34fb",
    "49535343-1e4d-4bd9-ba61-23c647249616",
}

WIT_SERVICE_UUIDS = {
    "0000ffe5-0000-1000-8000-00805f9a34fb",
}


class EventBus:
    def __init__(self) -> None:
        self._clients: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        client: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        if event.get("type") == "sensorStatus":
            print(f"sensorStatus: {json.dumps(event, separators=(',', ':'))}", flush=True)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(event)
            except queue.Full:
                try:
                    client.get_nowait()
                    client.put_nowait(event)
                except queue.Empty:
                    pass


FLOOR_LABELS = {
    "/dev/ttyUSB0": "Top Floor",
    "/dev/ttyUSB1": "Middle Floor",
    "/dev/ttyUSB2": "Bottom Floor",
}


class DemoController:
    def __init__(self, bus: EventBus, dry_run: bool) -> None:
        self.bus = bus
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "type": "demo",
            "state": "idle",
            "duration": 20.0,
            "delay": 0.0,
            "elapsed": 0.0,
            "duty": 0.0,
            "dryRun": dry_run,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(
        self,
        duration: float = 20.0,
        max_duty: float = 1.0,
        cycle: float = 0.2,
        step: float = 0.25,
        min_duty: float = 0.05,
        delay_min: float = 5.0,
        delay_max: float = 20.0,
    ) -> None:
        if duration <= 0 or duration > 60:
            raise ValueError("duration must be greater than 0 and no more than 60 seconds")
        if not 0 <= min_duty <= 1:
            raise ValueError("min duty must be between 0 and 1")
        if not 0 <= max_duty <= 1:
            raise ValueError("max duty must be between 0 and 1")
        if min_duty > max_duty:
            raise ValueError("min duty must be less than or equal to max duty")
        if cycle <= 0:
            raise ValueError("cycle must be greater than 0")
        if step <= 0:
            raise ValueError("step must be greater than 0")
        if delay_min < 0 or delay_max < delay_min:
            raise ValueError("delay range is invalid")

        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("demo already running")
            self._stop.clear()

        thread = threading.Thread(
            target=self._run,
            args=(duration, min_duty, max_duty, cycle, step, delay_min, delay_max),
            name="earthquake-demo",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _publish(self, **state: Any) -> None:
        event = {"type": "demo", **state, "dryRun": self.dry_run}
        with self._lock:
            self._state.update(event)
            snapshot = dict(self._state)
        self.bus.publish(snapshot)

    def _earthquake_profile(self, duration: float) -> dict[str, Any]:
        s_arrival = random.uniform(0.12, 0.30) * duration
        peak_time = random.uniform(0.35, 0.70) * duration
        coda_decay = random.uniform(0.18, 0.42) * duration
        burst_count = random.randint(2, 5)
        return {
            "sArrival": s_arrival,
            "peakTime": max(s_arrival + 0.5, peak_time),
            "codaDecay": max(1.0, coda_decay),
            "modA": random.uniform(0.7, 1.6),
            "modB": random.uniform(1.8, 3.4),
            "phaseA": random.uniform(0, math.tau),
            "phaseB": random.uniform(0, math.tau),
            "bursts": [
                {
                    "center": random.uniform(s_arrival, duration * 0.92),
                    "width": random.uniform(0.45, 1.8),
                    "amp": random.uniform(0.08, 0.28),
                }
                for _ in range(burst_count)
            ],
        }

    def _earthquake_duty(self, elapsed: float, duration: float, min_duty: float, max_duty: float, profile: dict[str, Any]) -> float:
        s_arrival = float(profile["sArrival"])
        peak_time = float(profile["peakTime"])
        coda_decay = float(profile["codaDecay"])
        if elapsed < s_arrival:
            phase = elapsed / max(0.1, s_arrival)
            envelope = 0.04 + 0.16 * phase
        elif elapsed < peak_time:
            phase = (elapsed - s_arrival) / max(0.1, peak_time - s_arrival)
            envelope = 0.18 + 0.72 * phase
        else:
            envelope = 0.16 + 0.82 * math.exp(-(elapsed - peak_time) / coda_decay)

        modulation = (
            0.08 * math.sin(math.tau * elapsed / float(profile["modA"]) + float(profile["phaseA"]))
            + 0.05 * math.sin(math.tau * elapsed / float(profile["modB"]) + float(profile["phaseB"]))
        )
        burst = 0.0
        for item in profile["bursts"]:
            distance = (elapsed - float(item["center"])) / float(item["width"])
            burst += float(item["amp"]) * math.exp(-distance * distance)
        noise = random.uniform(-0.07, 0.07)
        intensity = max(0.0, min(1.0, envelope + modulation + burst + noise))
        return max(min_duty, min(max_duty, min_duty + (max_duty - min_duty) * intensity))

    def _run(
        self,
        duration: float,
        min_duty: float,
        max_duty: float,
        cycle: float,
        step: float,
        delay_min: float,
        delay_max: float,
    ) -> None:
        motor = DryMotor() if self.dry_run else Motor()
        delay = random.uniform(delay_min, delay_max)
        self._publish(
            state="armed",
            mode="earthquake",
            duration=duration,
            delay=delay,
            elapsed=0.0,
            duty=0.0,
            minDuty=min_duty,
            maxDuty=max_duty,
            cycle=cycle,
        )
        try:
            motor.off()
            time.sleep(0.05)
            wait_start = time.monotonic()
            while not self._stop.is_set():
                waited = time.monotonic() - wait_start
                if waited >= delay:
                    break
                self._publish(state="waiting_random_delay", elapsed=0.0, delayRemaining=max(0.0, delay - waited))
                time.sleep(min(0.5, delay - waited))
            if self._stop.is_set():
                self._publish(state="stopped", duty=0.0)
                return

            start = time.monotonic()
            profile = self._earthquake_profile(duration)
            self._publish(state="running", elapsed=0.0, duty=0.0, delayRemaining=0.0)
            elapsed = 0.0
            while elapsed < duration and not self._stop.is_set():
                elapsed = time.monotonic() - start
                chunk = min(step, max(0.0, duration - elapsed))
                if chunk <= 0:
                    break
                duty = self._earthquake_duty(elapsed, duration, min_duty, max_duty, profile)
                current_cycle = max(0.08, min(0.35, cycle * random.uniform(0.72, 1.35)))
                self._publish(state="running", elapsed=elapsed, duration=duration, duty=duty, cycle=current_cycle)
                pulse(motor, "forward", duty, chunk, current_cycle)
        except Exception as exc:
            self._publish(state="error", message=str(exc), duty=0.0)
        finally:
            try:
                motor.off()
            finally:
                motor.close()
            if not self._stop.is_set():
                self._publish(state="complete", duty=0.0, elapsed=duration)


class SensorClassifier:
    def __init__(self, bus: EventBus, demo: DemoController, expected_sensors: int = 3) -> None:
        self.bus = bus
        self.demo = demo
        self.expected_sensors = expected_sensors
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._latest: dict[str, tuple[float, float]] = {}
        self._last_label = ""
        self._last_emit = 0.0
        self._candidate_label = ""
        self._candidate_since = 0.0
        self._stable_label = "No motion"
        self._lock = threading.Lock()

    def ingest(self, event: dict[str, Any]) -> None:
        sensor_id = str(event.get("sensorId") or "")
        if not sensor_id:
            return
        raw_value = max(0.0, float(event.get("accelMag") or 0.0))
        value = abs(raw_value - 1.0) if event.get("packet") == "accel" else raw_value
        ts = float(event.get("ts") or time.time())
        with self._lock:
            samples = self._samples.setdefault(sensor_id, deque())
            samples.append((ts, value))
            cutoff = ts - 2.0
            while samples and samples[0][0] < cutoff:
                samples.popleft()
            self._latest[sensor_id] = (ts, value)
            classification = self._classify_locked(ts)
        if classification is not None:
            self.bus.publish(classification)

    def _classify_locked(self, now: float) -> dict[str, Any] | None:
        active_latest = {
            sensor_id: value
            for sensor_id, (ts, value) in self._latest.items()
            if now - ts <= 1.0
        }
        energies: dict[str, float] = {}
        peaks: dict[str, float] = {}
        for sensor_id, samples in self._samples.items():
            window = [value for ts, value in samples if now - ts <= 0.75]
            if not window:
                continue
            energies[sensor_id] = sum(value * value for value in window) / len(window)
            peaks[sensor_id] = max(window)

        active_count = len(active_latest)
        energized = [sensor_id for sensor_id, energy in energies.items() if energy >= 0.0025 or peaks[sensor_id] >= 0.08]
        total = sum(active_latest.values())
        strongest = max(peaks.values(), default=0.0)
        sustained = [
            sensor_id
            for sensor_id, samples in self._samples.items()
            if len([value for ts, value in samples if now - ts <= 1.5 and value >= 0.04]) >= 6
        ]

        if active_count == 0:
            raw_label = "No motion"
            confidence = 0.0
        elif active_count >= self.expected_sensors and len(energized) >= 2 and len(sustained) >= 2:
            raw_label = "Earthquake detected"
            confidence = min(0.95, 0.35 + 0.18 * len(energized) + 0.12 * len(sustained) + total * 5.0)
        elif len(energized) >= 1:
            raw_label = "Non-Earthquake motion detected"
            confidence = min(0.8, 0.35 + 0.15 * len(energized) + total * 5.0)
        else:
            raw_label = "No motion"
            confidence = min(0.4, total * 10.0)

        label = self._stabilize_label_locked(raw_label, now)
        if label == self._last_label and now - self._last_emit < 0.5:
            return None
        self._last_label = label
        self._last_emit = now
        return {
            "type": "classification",
            "label": label,
            "confidence": confidence,
            "activeSensors": active_count,
            "energizedSensors": len(energized),
            "sustainedSensors": len(sustained),
            "totalShake": total,
            "sensors": [
                {
                    "id": sensor_id,
                    "name": FLOOR_LABELS.get(sensor_id, sensor_id),
                    "value": active_latest.get(sensor_id, 0.0),
                    "energy": energies.get(sensor_id, 0.0),
                    "peak": peaks.get(sensor_id, 0.0),
                    "connected": sensor_id in active_latest,
                }
                for sensor_id in sorted(set(self._samples) | set(active_latest))
            ],
        }

    def _stabilize_label_locked(self, label: str, now: float) -> str:
        if label != self._candidate_label:
            self._candidate_label = label
            self._candidate_since = now

        required = 0.0
        if label == "Earthquake detected":
            required = 2.0
        elif label == "Non-Earthquake motion detected":
            required = 0.75
        elif self._stable_label != "No motion":
            required = 1.0

        if now - self._candidate_since >= required:
            self._stable_label = label
        return self._stable_label


def auto_serial_port() -> str | None:
    patterns = [
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/serial/by-id/*",
        "/dev/tty.usbserial*",
        "/dev/tty.usbmodem*",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


def open_serial(path: str, baud: int) -> int:
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    speed = BAUD_RATES.get(baud)
    if speed is None:
        os.close(fd)
        raise ValueError(f"unsupported baud rate: {baud}")
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    attrs[2] |= termios.CLOCAL | termios.CREAD
    attrs[2] &= ~termios.CSTOPB
    attrs[2] &= ~termios.PARENB
    attrs[2] &= ~termios.CSIZE
    attrs[2] |= termios.CS8
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def int16(low: int, high: int) -> int:
    return struct.unpack("<h", bytes([low, high]))[0]


def parse_wit_packet(packet: bytes) -> dict[str, Any] | None:
    if len(packet) != 11 or packet[0] != 0x55:
        return None
    if sum(packet[:10]) & 0xFF != packet[10]:
        return None

    kind = packet[1]
    values = [int16(packet[i], packet[i + 1]) for i in (2, 4, 6, 8)]

    if kind == 0x51:
        ax = values[0] / 32768.0 * 16.0
        ay = values[1] / 32768.0 * 16.0
        az = values[2] / 32768.0 * 16.0
        return {
            "type": "sensor",
            "packet": "accel",
            "ax": ax,
            "ay": ay,
            "az": az,
            "accelMag": math.sqrt(ax * ax + ay * ay + az * az),
            "tempC": values[3] / 100.0,
        }
    if kind == 0x52:
        return {
            "type": "sensor",
            "packet": "gyro",
            "gx": values[0] / 32768.0 * 2000.0,
            "gy": values[1] / 32768.0 * 2000.0,
            "gz": values[2] / 32768.0 * 2000.0,
            "tempC": values[3] / 100.0,
        }
    if kind == 0x53:
        return {
            "type": "sensor",
            "packet": "angle",
            "roll": values[0] / 32768.0 * 180.0,
            "pitch": values[1] / 32768.0 * 180.0,
            "yaw": values[2] / 32768.0 * 180.0,
            "version": values[3],
        }
    return None


def parse_wit_61_frame(frame: bytes) -> dict[str, Any] | None:
    if len(frame) != 32 or frame[0] != 0x55 or frame[1] != 0x61:
        return None
    words = [int16(frame[i], frame[i + 1]) for i in range(2, 32, 2)]
    # WTVB01-BT50 wired adapters are currently sending this wider 0x61 frame.
    # The public Python SDK documents the 11-byte packets, so expose a stable
    # motion metric from the active 16-bit channels until we pin every field.
    motion_words = words[7:13] if len(words) >= 13 else words
    motion = math.sqrt(sum(value * value for value in motion_words)) / 1000.0
    return {
        "type": "sensor",
        "packet": "wide61",
        "ax": words[7] / 1000.0 if len(words) > 7 else 0.0,
        "ay": words[8] / 1000.0 if len(words) > 8 else 0.0,
        "az": words[9] / 1000.0 if len(words) > 9 else 0.0,
        "accelMag": motion,
        "tempC": words[6] / 100.0 if len(words) > 6 else None,
        "rawWords": words,
    }


def read_wit_event(buffer: bytearray) -> dict[str, Any] | None:
    while buffer:
        start = buffer.find(0x55)
        if start < 0:
            buffer.clear()
            return None
        if start:
            del buffer[:start]
        if len(buffer) < 2:
            return None
        if buffer[1] == 0x61:
            if len(buffer) < 32:
                return None
            frame = bytes(buffer[:32])
            del buffer[:32]
            return parse_wit_61_frame(frame)
        if len(buffer) < 11:
            return None
        packet = bytes(buffer[:11])
        event = parse_wit_packet(packet)
        if event is None:
            del buffer[0]
            continue
        del buffer[:11]
        return event
    return None


class SensorReader:
    def __init__(
        self,
        bus: EventBus,
        ports: list[str],
        baud: int,
        simulate: bool,
        mode: str,
        ble_names: list[str],
        ble_addresses: list[str],
        ble_service_uuids: list[str],
        max_ble_devices: int,
        classifier: SensorClassifier | None = None,
    ) -> None:
        self.bus = bus
        self.ports = ports
        self.baud = baud
        self.simulate = simulate
        self.mode = mode
        self.ble_names = ble_names
        self.ble_addresses = [address.upper() for address in ble_addresses]
        self.ble_service_uuids = [uuid.lower() for uuid in ble_service_uuids]
        self.max_ble_devices = max_ble_devices
        self.classifier = classifier
        self._stop = threading.Event()
        self._transport_lock = threading.Lock()
        self._last_transport_sample: dict[str, float] = {}

    def _publish_sensor(self, event: dict[str, Any]) -> None:
        self.bus.publish(event)
        if (
            event.get("type") == "sensor"
            and event.get("packet") in {"accel", "wide61"}
            and self.classifier
            and self._should_classify_transport(event)
        ):
            self.classifier.ingest(event)

    def _should_classify_transport(self, event: dict[str, Any]) -> bool:
        transport = str(event.get("transport") or "")
        if transport not in {"serial", "ble"}:
            return True
        now = time.time()
        with self._transport_lock:
            self._last_transport_sample[transport] = now
            serial_active = now - self._last_transport_sample.get("serial", 0.0) <= 2.5
        if serial_active:
            return transport == "serial"
        return transport == "ble"

    def start(self) -> None:
        threading.Thread(target=self._run, name="sensor-reader", daemon=True).start()

    def _run(self) -> None:
        self.bus.publish({"type": "sensorStatus", "state": "starting", "mode": self.mode})
        if self.simulate:
            self._simulate()
            return

        if self.mode == "auto":
            self._run_auto()
            return

        if self.mode == "serial":
            self._run_serial()
            return

        if self.mode == "ble":
            try:
                asyncio.run(self._run_ble())
                return
            except Exception as exc:
                self.bus.publish({"type": "sensorStatus", "state": "error", "message": f"BLE error: {exc}"})
                self._simulate()
                return

    def _run_auto(self) -> None:
        ports = self._serial_ports()
        if ports:
            threading.Thread(target=self._run_serial, args=(False,), name="serial-auto", daemon=True).start()
        else:
            self.bus.publish(
                {
                    "type": "sensorStatus",
                    "state": "serial-unavailable",
                    "message": "No serial sensor ports found; using BLE",
                }
            )
        try:
            asyncio.run(self._run_ble())
            return
        except Exception as exc:
            self.bus.publish({"type": "sensorStatus", "state": "error", "message": f"BLE error: {exc}"})
            if not ports:
                self._simulate()
                return
            while not self._stop.is_set():
                time.sleep(1.0)

    def _serial_ports(self) -> list[str]:
        if self.ports:
            return self.ports
        port = auto_serial_port()
        return [port] if port is not None else []

    def _run_serial(self, fallback_simulate: bool = True) -> None:
        ports = self._serial_ports()
        if not ports:
            self.bus.publish({"type": "sensorStatus", "state": "simulated", "message": "no serial sensor found"})
            if fallback_simulate:
                self._simulate()
            return

        self.bus.publish({"type": "sensorStatus", "state": "serial-ready", "ports": ports, "baud": self.baud})
        threads = [
            threading.Thread(
                target=self._read_serial_port,
                args=(port, f"WIT {index + 1}"),
                name=f"serial-{index + 1}",
                daemon=True,
            )
            for index, port in enumerate(ports)
        ]
        for thread in threads:
            thread.start()
        while not self._stop.is_set():
            time.sleep(1.0)

    def _read_serial_port(self, port: str, sensor_name: str) -> None:
        self.bus.publish(
            {
                "type": "sensorStatus",
                "state": "connecting",
                "transport": "serial",
                "port": port,
                "baud": self.baud,
                "name": sensor_name,
            }
        )
        try:
            fd = open_serial(port, self.baud)
        except Exception as exc:
            self.bus.publish(
                {
                    "type": "sensorStatus",
                    "state": "error",
                    "transport": "serial",
                    "port": port,
                    "name": sensor_name,
                    "message": str(exc),
                }
            )
            return

        self.bus.publish(
            {
                "type": "sensorStatus",
                "state": "connected",
                "transport": "serial",
                "port": port,
                "baud": self.baud,
                "name": sensor_name,
            }
        )
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = os.read(fd, 256)
                except BlockingIOError:
                    time.sleep(0.02)
                    continue
                if not chunk:
                    time.sleep(0.02)
                    continue
                buffer.extend(chunk)
                while True:
                    event = read_wit_event(buffer)
                    if event is None:
                        break
                    if event is not None:
                        event["sensorId"] = port
                        event["sensorName"] = sensor_name
                        event["transport"] = "serial"
                        self._publish_sensor(event)
        finally:
            os.close(fd)

    async def _run_ble(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise RuntimeError("bleak is not installed") from exc

        wanted = [name.upper() for name in self.ble_names]
        self.bus.publish(
            {
                "type": "sensorStatus",
                "state": "ble-ready",
                "names": self.ble_names,
                "addresses": self.ble_addresses,
                "serviceUuids": self.ble_service_uuids,
            }
        )
        while not self._stop.is_set():
            self.bus.publish({"type": "sensorStatus", "state": "scanning", "message": "Scanning for WIT BLE sensors"})
            discovered = await BleakScanner.discover(
                timeout=30.0,
                return_adv=True,
                service_uuids=self.ble_service_uuids or list(WIT_SERVICE_UUIDS),
            )
            targets = [
                (device, advertisement)
                for device, advertisement in discovered.values()
                if any(token in ((device.name or advertisement.local_name or "").upper()) for token in wanted)
                or device.address.upper() in self.ble_addresses
                or any(
                    service_uuid.lower() in self.ble_service_uuids or service_uuid.lower() in WIT_SERVICE_UUIDS
                    for service_uuid in advertisement.service_uuids
                )
            ]
            targets = sorted(targets, key=lambda target: target[1].rssi or -999, reverse=True)
            targets = targets[: self.max_ble_devices]
            self.bus.publish(
                {
                    "type": "sensorStatus",
                    "state": "discovered",
                    "count": len(targets),
                    "devices": [
                        {
                            "address": device.address,
                            "name": device.name or advertisement.local_name or device.address,
                            "rssi": advertisement.rssi,
                        }
                        for device, advertisement in targets
                    ],
                }
            )
            if not targets:
                disconnected = self._disconnect_stale_ble_connections()
                if disconnected:
                    await asyncio.sleep(2.0)
                    continue
                await asyncio.sleep(4.0)
                continue

            tasks = [
                asyncio.create_task(
                    self._read_ble_device(
                        device,
                        sensor_name=device.name or advertisement.local_name or device.address,
                        delay=index * 4.0,
                    )
                )
                for index, (device, advertisement) in enumerate(targets)
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await asyncio.sleep(1.0)

    def _disconnect_stale_ble_connections(self) -> int:
        if shutil.which("bluetoothctl") is None:
            return 0
        try:
            listed = subprocess.run(
                ["bluetoothctl", "devices", "Connected"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except Exception:
            return 0
        disconnected = 0
        for line in listed.stdout.splitlines():
            parts = line.split(maxsplit=2)
            if len(parts) < 2 or parts[0] != "Device":
                continue
            address = parts[1]
            name = parts[2] if len(parts) > 2 else ""
            should_check = address.upper() in self.ble_addresses or any(
                token and token.upper() in name.upper() for token in self.ble_names if len(token) > 2
            )
            if not should_check:
                try:
                    info = subprocess.run(
                        ["bluetoothctl", "info", address],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=5.0,
                    ).stdout.lower()
                except Exception:
                    info = ""
                should_check = any(uuid in info for uuid in self.ble_service_uuids or WIT_SERVICE_UUIDS)
            if not should_check:
                continue
            subprocess.run(["bluetoothctl", "disconnect", address], check=False, capture_output=True, text=True, timeout=8.0)
            disconnected += 1
        if disconnected:
            self.bus.publish(
                {
                    "type": "sensorStatus",
                    "state": "ble-disconnect-stale",
                    "count": disconnected,
                    "message": "Disconnected stale WIT BLE links before rescanning",
                }
            )
        return disconnected

    async def _read_ble_device(self, device: Any, sensor_name: str, delay: float = 0.0) -> None:
        from bleak import BleakClient

        sensor_id = device.address
        buffer = bytearray()
        if delay:
            await asyncio.sleep(delay)

        def on_notify(_sender: Any, data: bytearray) -> None:
            buffer.extend(data)
            while True:
                event = read_wit_event(buffer)
                if event is None:
                    break
                if event is not None:
                    event["sensorId"] = sensor_id
                    event["sensorName"] = sensor_name
                    event["transport"] = "ble"
                    self._publish_sensor(event)

        try:
            async with BleakClient(device, timeout=15.0) as client:
                self.bus.publish(
                    {
                        "type": "sensorStatus",
                        "state": "connected",
                        "transport": "ble",
                        "address": sensor_id,
                        "name": sensor_name,
                    }
                )
                notify_chars = []
                for service in client.services:
                    for char in service.characteristics:
                        if char.uuid.lower() in WIT_NOTIFY_UUIDS and (
                            "notify" in char.properties or "indicate" in char.properties
                        ):
                            notify_chars.append(char)
                if not notify_chars:
                    for service in client.services:
                        for char in service.characteristics:
                            if "notify" in char.properties or "indicate" in char.properties:
                                notify_chars.append(char)
                if not notify_chars:
                    self.bus.publish(
                        {
                            "type": "sensorStatus",
                            "state": "error",
                            "message": f"{sensor_name} has no notifying BLE characteristics",
                        }
                    )
                    return
                for char in notify_chars:
                    await client.start_notify(char.uuid, on_notify)
                self.bus.publish(
                    {
                        "type": "sensorStatus",
                        "state": "subscribed",
                        "transport": "ble",
                        "address": sensor_id,
                        "name": sensor_name,
                        "characteristics": [char.uuid for char in notify_chars],
                    }
                )
                while client.is_connected and not self._stop.is_set():
                    await asyncio.sleep(0.5)
        except Exception as exc:
            self.bus.publish(
                {
                    "type": "sensorStatus",
                    "state": "error",
                    "transport": "ble",
                    "address": sensor_id,
                    "name": sensor_name,
                    "message": str(exc) or repr(exc),
                }
            )

    def _simulate(self) -> None:
        start = time.monotonic()
        while not self._stop.is_set():
            t = time.monotonic() - start
            ax = math.sin(t * 4.0) * 0.07 + random.uniform(-0.02, 0.02)
            ay = math.cos(t * 3.0) * 0.05 + random.uniform(-0.02, 0.02)
            az = 1.0 + math.sin(t * 6.0) * 0.04 + random.uniform(-0.015, 0.015)
            self._publish_sensor(
                {
                    "type": "sensor",
                    "packet": "accel",
                    "sensorId": "simulated",
                    "sensorName": "Simulated",
                    "ax": ax,
                    "ay": ay,
                    "az": az,
                    "accelMag": math.sqrt(ax * ax + ay * ay + az * az),
                    "simulated": True,
                }
            )
            time.sleep(0.1)


def make_handler(bus: EventBus, controller: DemoController):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(STATIC), **kwargs)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def do_GET(self) -> None:
            if self.path == "/events":
                self._events()
                return
            if self.path == "/health":
                self._json({"ok": True, "demo": controller.snapshot()})
                return
            if self.path == "/api/demo/state":
                self._json({"ok": True, "demo": controller.snapshot()})
                return
            if self.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def do_POST(self) -> None:
            if self.path == "/api/demo/stop":
                controller.stop()
                self._json({"ok": True, "demo": controller.snapshot()})
                return
            if self.path not in {"/api/rumble", "/api/demo/start"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                controller.start(
                    duration=float(payload.get("duration", 20.0)),
                    max_duty=float(payload.get("maxDuty", payload.get("duty", 1.0))),
                    cycle=float(payload.get("cycle", 0.2)),
                    step=float(payload.get("step", 0.25)),
                    min_duty=float(payload.get("minDuty", 0.05)),
                    delay_min=float(payload.get("delayMin", 5.0)),
                    delay_max=float(payload.get("delayMax", 20.0)),
                )
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "demo": controller.snapshot()})

        def _events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            client = bus.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = client.get(timeout=15)
                        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
                        self.wfile.write(b"data: " + data + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                bus.unsubscribe(client)

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the rumble control UI.")
    parser.add_argument("--host", default=os.environ.get("RUMBLE_UI_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RUMBLE_UI_PORT", "8000")))
    parser.add_argument("--sensor-port", default=os.environ.get("WIT_SERIAL_PORT"))
    parser.add_argument("--sensor-ports", default=os.environ.get("WIT_SERIAL_PORTS"))
    parser.add_argument("--sensor-baud", type=int, default=int(os.environ.get("WIT_BAUD", "9600")))
    parser.add_argument("--sensor-mode", choices=["auto", "ble", "serial"], default=os.environ.get("SENSOR_MODE", "auto"))
    parser.add_argument("--ble-name", action="append", default=None)
    parser.add_argument("--ble-address", action="append", default=None)
    parser.add_argument("--ble-service-uuid", action="append", default=None)
    parser.add_argument("--max-ble-devices", type=int, default=int(os.environ.get("MAX_BLE_DEVICES", "3")))
    parser.add_argument("--simulate-sensor", action="store_true", default=os.environ.get("SIMULATE_SENSOR") == "1")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("MOTOR_DRY_RUN") == "1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bus = EventBus()
    controller = DemoController(bus, dry_run=args.dry_run)
    classifier = SensorClassifier(bus, controller)
    ble_names = args.ble_name or os.environ.get("WIT_BLE_NAMES", "WTVB,WIT,WT").split(",")
    ble_addresses = args.ble_address or os.environ.get("WIT_BLE_ADDRESSES", "").split(",")
    ble_service_uuids = args.ble_service_uuid or os.environ.get(
        "WIT_BLE_SERVICE_UUIDS",
        ",".join(sorted(WIT_SERVICE_UUIDS)),
    ).split(",")
    serial_ports = []
    if args.sensor_ports:
        serial_ports.extend(port.strip() for port in args.sensor_ports.split(",") if port.strip())
    elif args.sensor_port:
        serial_ports.append(args.sensor_port)
    sensor = SensorReader(
        bus,
        serial_ports,
        args.sensor_baud,
        args.simulate_sensor,
        args.sensor_mode,
        [name.strip() for name in ble_names if name.strip()],
        [address.strip() for address in ble_addresses if address.strip()],
        [uuid.strip() for uuid in ble_service_uuids if uuid.strip()],
        args.max_ble_devices,
        classifier,
    )
    sensor.start()

    handler = make_handler(bus, controller)
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        mode = "dry-run" if args.dry_run else "hardware"
        print(f"rumble UI listening on http://{args.host}:{args.port} ({mode})", flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
