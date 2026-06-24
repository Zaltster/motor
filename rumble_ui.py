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
import socketserver
import struct
import termios
import threading
import time
import tty
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


class RumbleController:
    def __init__(self, bus: EventBus, dry_run: bool) -> None:
        self.bus = bus
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._busy = False

    def start(self, duration: float, max_duty: float, cycle: float, step: float = 0.75, min_duty: float = 0.05) -> None:
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

        with self._lock:
            if self._busy:
                raise RuntimeError("rumble already running")
            self._busy = True

        thread = threading.Thread(
            target=self._run,
            args=(duration, min_duty, max_duty, cycle, step),
            name="earthquake",
            daemon=True,
        )
        thread.start()

    def _run(self, duration: float, min_duty: float, max_duty: float, cycle: float, step: float) -> None:
        motor = DryMotor() if self.dry_run else Motor()
        start = time.monotonic()
        self.bus.publish(
            {
                "type": "rumble",
                "state": "start",
                "mode": "earthquake",
                "duration": duration,
                "duty": 0,
                "minDuty": min_duty,
                "maxDuty": max_duty,
                "cycle": cycle,
                "dryRun": self.dry_run,
            }
        )
        try:
            motor.off()
            time.sleep(0.05)
            elapsed = 0.0
            while elapsed < duration:
                chunk = min(step, duration - elapsed)
                duty = random.uniform(min_duty, max_duty)
                self.bus.publish(
                    {
                        "type": "rumble",
                        "state": "running",
                        "mode": "earthquake",
                        "elapsed": time.monotonic() - start,
                        "duration": duration,
                        "duty": duty,
                    }
                )
                pulse(motor, "forward", duty, chunk, cycle)
                elapsed += chunk
        except Exception as exc:
            self.bus.publish({"type": "rumble", "state": "error", "message": str(exc)})
        finally:
            try:
                motor.off()
            finally:
                motor.close()
            self.bus.publish({"type": "rumble", "state": "stop", "duty": 0, "mode": "earthquake"})
            with self._lock:
                self._busy = False


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
        max_ble_devices: int,
    ) -> None:
        self.bus = bus
        self.ports = ports
        self.baud = baud
        self.simulate = simulate
        self.mode = mode
        self.ble_names = ble_names
        self.ble_addresses = [address.upper() for address in ble_addresses]
        self.max_ble_devices = max_ble_devices
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="sensor-reader", daemon=True).start()

    def _run(self) -> None:
        self.bus.publish({"type": "sensorStatus", "state": "starting", "mode": self.mode})
        if self.simulate:
            self._simulate()
            return

        if self.mode == "serial" or (self.mode == "auto" and self._serial_ports()):
            self._run_serial()
            return

        if self.mode in {"auto", "ble"}:
            try:
                asyncio.run(self._run_ble())
                return
            except Exception as exc:
                self.bus.publish({"type": "sensorStatus", "state": "error", "message": f"BLE error: {exc}"})
                if self.mode == "ble":
                    self._simulate()
                    return

    def _serial_ports(self) -> list[str]:
        if self.ports:
            return self.ports
        port = auto_serial_port()
        return [port] if port is not None else []

    def _run_serial(self) -> None:
        ports = self._serial_ports()
        if not ports:
            self.bus.publish({"type": "sensorStatus", "state": "simulated", "message": "no serial sensor found"})
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
                        self.bus.publish(event)
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
            }
        )
        while not self._stop.is_set():
            self.bus.publish({"type": "sensorStatus", "state": "scanning", "message": "Scanning for WIT BLE sensors"})
            devices = await BleakScanner.discover(timeout=15.0)
            targets = [
                device
                for device in devices
                if any(token in ((device.name or "").upper()) for token in wanted)
                or device.address.upper() in self.ble_addresses
            ]
            targets = sorted(targets, key=lambda device: getattr(device, "rssi", -999) or -999, reverse=True)
            targets = targets[: self.max_ble_devices]
            self.bus.publish(
                {
                    "type": "sensorStatus",
                    "state": "discovered",
                    "count": len(targets),
                    "devices": [
                        {
                            "address": device.address,
                            "name": device.name or device.address,
                            "rssi": getattr(device, "rssi", None),
                        }
                        for device in targets
                    ],
                }
            )
            if not targets:
                await asyncio.sleep(4.0)
                continue

            tasks = [
                asyncio.create_task(self._read_ble_device(device, delay=index * 4.0))
                for index, device in enumerate(targets)
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

    async def _read_ble_device(self, device: Any, delay: float = 0.0) -> None:
        from bleak import BleakClient

        sensor_id = device.address
        sensor_name = device.name or device.address
        buffer = bytearray()
        if delay:
            await asyncio.sleep(delay)

        def on_notify(_sender: Any, data: bytearray) -> None:
            buffer.extend(data)
            while len(buffer) >= 11:
                start = buffer.find(0x55)
                if start < 0:
                    buffer.clear()
                    return
                if start:
                    del buffer[:start]
                if len(buffer) < 11:
                    return
                packet = bytes(buffer[:11])
                del buffer[:11]
                event = parse_wit_packet(packet)
                if event is not None:
                    event["sensorId"] = sensor_id
                    event["sensorName"] = sensor_name
                    event["transport"] = "ble"
                    self.bus.publish(event)

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
            self.bus.publish(
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


def make_handler(bus: EventBus, controller: RumbleController):
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
                self._json({"ok": True})
                return
            if self.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def do_POST(self) -> None:
            if self.path != "/api/rumble":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                controller.start(
                    duration=float(payload.get("duration", 2.0)),
                    max_duty=float(payload.get("maxDuty", payload.get("duty", 1.0))),
                    cycle=float(payload.get("cycle", 0.2)),
                    step=float(payload.get("step", 0.75)),
                    min_duty=float(payload.get("minDuty", 0.05)),
                )
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True})

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
    parser.add_argument("--max-ble-devices", type=int, default=int(os.environ.get("MAX_BLE_DEVICES", "3")))
    parser.add_argument("--simulate-sensor", action="store_true", default=os.environ.get("SIMULATE_SENSOR") == "1")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("MOTOR_DRY_RUN") == "1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bus = EventBus()
    controller = RumbleController(bus, dry_run=args.dry_run)
    ble_names = args.ble_name or os.environ.get("WIT_BLE_NAMES", "WTVB,WIT,WT").split(",")
    ble_addresses = args.ble_address or os.environ.get("WIT_BLE_ADDRESSES", "").split(",")
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
        args.max_ble_devices,
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
