#!/usr/bin/env python3
"""Scan for and stream WIT-style BLE sensor packets from macOS."""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import time
from typing import Any

from bleak import BleakClient, BleakScanner


WIT_NOTIFY_UUIDS = {
    "0000ffe4-0000-1000-8000-00805f9a34fb",
    "49535343-1e4d-4bd9-ba61-23c647249616",
}


def int16(low: int, high: int) -> int:
    return struct.unpack("<h", bytes([low, high]))[0]


def parse_wit_packet(packet: bytes) -> dict[str, Any] | None:
    if len(packet) != 11 or packet[0] != 0x55:
        return None
    if sum(packet[:10]) & 0xFF != packet[10]:
        return None
    values = [int16(packet[i], packet[i + 1]) for i in (2, 4, 6, 8)]
    if packet[1] == 0x51:
        ax = values[0] / 32768.0 * 16.0
        ay = values[1] / 32768.0 * 16.0
        az = values[2] / 32768.0 * 16.0
        return {
            "packet": "accel",
            "ax": ax,
            "ay": ay,
            "az": az,
            "accelMag": math.sqrt(ax * ax + ay * ay + az * az),
            "tempC": values[3] / 100.0,
        }
    if packet[1] == 0x52:
        return {
            "packet": "gyro",
            "gx": values[0] / 32768.0 * 2000.0,
            "gy": values[1] / 32768.0 * 2000.0,
            "gz": values[2] / 32768.0 * 2000.0,
            "tempC": values[3] / 100.0,
        }
    if packet[1] == 0x53:
        return {
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
    motion_words = words[7:13] if len(words) >= 13 else words
    return {
        "packet": "wide61",
        "accelMag": math.sqrt(sum(value * value for value in motion_words)) / 1000.0,
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


async def scan(args: argparse.Namespace) -> list[Any]:
    print(f"Scanning for BLE devices for {args.scan_seconds:.1f}s...")
    service_filter = args.scan_service_uuid or None
    if service_filter:
        print("CoreBluetooth service filter:", ", ".join(service_filter))
    devices = await BleakScanner.discover(timeout=args.scan_seconds, return_adv=True, service_uuids=service_filter)
    rows = []
    for device, advertisement in devices.values():
        name = device.name or advertisement.local_name or ""
        service_uuids = sorted(str(uuid).lower() for uuid in advertisement.service_uuids)
        rows.append((name, device.address, advertisement.rssi, service_uuids))
    rows.sort(key=lambda row: (row[0] == "", row[0].upper(), row[1]))
    for name, address, rssi, service_uuids in rows:
        services = ",".join(service_uuids[:4])
        print(f"{address:36} rssi={rssi!s:>4} name={name or '<unnamed>'} services={services}")
    wanted = [token.upper() for token in args.name_token]
    service_tokens = [token.lower() for token in args.service_token]
    matches = [
        device
        for device, advertisement in devices.values()
        if device.address.upper() in [address.upper() for address in args.address]
        or any(token in ((device.name or advertisement.local_name or "").upper()) for token in wanted)
        or any(token in str(uuid).lower() for token in service_tokens for uuid in advertisement.service_uuids)
    ]
    return matches


async def stream_device(device: Any, seconds: float) -> None:
    print(f"Connecting to {device.address} {device.name or ''}".rstrip())
    buffer = bytearray()
    count = 0

    def on_notify(_sender: Any, data: bytearray) -> None:
        nonlocal count
        buffer.extend(data)
        while True:
            event = read_wit_event(buffer)
            if event is None:
                break
            count += 1
            event["count"] = count
            event["ts"] = round(time.time(), 3)
            print(event, flush=True)

    async with BleakClient(device, timeout=15.0) as client:
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
            print("No notifying characteristics found.")
            return
        print("Notify characteristics:", ", ".join(char.uuid for char in notify_chars))
        for char in notify_chars:
            await client.start_notify(char.uuid, on_notify)
        await asyncio.sleep(seconds)
        for char in notify_chars:
            await client.stop_notify(char.uuid)
    print(f"Received {count} parsed WIT events from {device.address}")


async def main_async(args: argparse.Namespace) -> int:
    matches = await scan(args)
    if args.scan_only:
        return 0
    if not matches:
        print("No WIT-like BLE devices matched. Use --address with a listed device to force a stream attempt.")
        return 1
    for device in matches[: args.max_devices]:
        await stream_device(device, args.stream_seconds)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan and stream WIT BLE sensor data.")
    parser.add_argument("--scan-seconds", type=float, default=10.0)
    parser.add_argument("--stream-seconds", type=float, default=10.0)
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--max-devices", type=int, default=3)
    parser.add_argument("--name-token", action="append", default=["WIT", "WTVB", "WT"])
    parser.add_argument("--service-token", action="append", default=["ffe5"])
    parser.add_argument("--scan-service-uuid", action="append", default=[])
    parser.add_argument("--address", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
