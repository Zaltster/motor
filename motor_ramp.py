#!/usr/bin/env python3
"""Run an L298N up/down power profile and write CSV + SVG graph.

Wiring:
  gray  -> GPIO12 -> ENA
  brown -> GPIO17 -> IN1
  black -> GPIO18 -> IN2
  blue  -> GND
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import os
import time
from dataclasses import dataclass


GPIOCHIP = "/dev/gpiochip0"
ENA = 12
IN1 = 17
IN2 = 18

IOC_NRSHIFT = 0
IOC_TYPESHIFT = 8
IOC_SIZESHIFT = 16
IOC_DIRSHIFT = 30
IOC_WRITE = 1
IOC_READ = 2


def iowr(type_: int, nr: int, size: int) -> int:
    return (
        ((IOC_READ | IOC_WRITE) << IOC_DIRSHIFT)
        | (type_ << IOC_TYPESHIFT)
        | (nr << IOC_NRSHIFT)
        | (size << IOC_SIZESHIFT)
    )


GPIO_GET_LINEHANDLE_IOCTL = iowr(0xB4, 0x03, 364)
GPIOHANDLE_SET_LINE_VALUES_IOCTL = iowr(0xB4, 0x09, 64)
GPIOHANDLE_REQUEST_OUTPUT = 1 << 1


@dataclass(frozen=True)
class Sample:
    second: float
    duty: float


class MotorLines:
    def __init__(self) -> None:
        request = bytearray(364)
        for idx, line in enumerate([ENA, IN1, IN2]):
            request[idx * 4 : idx * 4 + 4] = line.to_bytes(4, "little")
        request[256:260] = GPIOHANDLE_REQUEST_OUTPUT.to_bytes(4, "little")
        request[260] = 0
        request[261] = 1
        request[262] = 0
        label = b"motor-ramp"
        request[324 : 324 + len(label)] = label
        request[356:360] = (3).to_bytes(4, "little")

        chip = os.open(GPIOCHIP, os.O_RDONLY)
        try:
            fcntl.ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, request, True)
            self.fd = int.from_bytes(request[360:364], "little", signed=True)
        finally:
            os.close(chip)

        self.values = bytearray(64)

    def set(self, ena: int, in1: int = 1, in2: int = 0) -> None:
        self.values[0] = 1 if ena else 0
        self.values[1] = 1 if in1 else 0
        self.values[2] = 1 if in2 else 0
        fcntl.ioctl(self.fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, self.values, True)

    def off(self) -> None:
        self.set(0, 0, 0)

    def close(self) -> None:
        os.close(self.fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ramp L298N motor power up and down.")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--step", type=float, default=1.0)
    parser.add_argument("--min-duty", type=float, default=0.0)
    parser.add_argument("--max-duty", type=float, default=1.0)
    parser.add_argument("--pwm-frequency", type=float, default=20.0)
    parser.add_argument("--csv", default="/data/motor/motor_ramp.csv")
    parser.add_argument("--svg", default="/data/motor/motor_ramp.svg")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def validate(args: argparse.Namespace) -> None:
    if args.duration <= 0:
        raise ValueError("--duration must be greater than 0")
    if args.step <= 0:
        raise ValueError("--step must be greater than 0")
    if args.pwm_frequency <= 0:
        raise ValueError("--pwm-frequency must be greater than 0")
    if not 0 <= args.min_duty <= 1:
        raise ValueError("--min-duty must be between 0 and 1")
    if not 0 <= args.max_duty <= 1:
        raise ValueError("--max-duty must be between 0 and 1")
    if args.min_duty > args.max_duty:
        raise ValueError("--min-duty must be <= --max-duty")


def duty_at(second: float, duration: float, min_duty: float, max_duty: float) -> float:
    half = duration / 2.0
    if second <= half:
        ratio = second / half
    else:
        ratio = max(0.0, (duration - second) / half)
    return min_duty + (max_duty - min_duty) * ratio


def pwm_for(lines: MotorLines | None, duty: float, seconds: float, frequency: float) -> None:
    if lines is None:
        time.sleep(seconds)
        return

    duty = max(0.0, min(1.0, duty))
    period = 1.0 / frequency
    end = time.monotonic() + seconds

    if duty >= 1.0:
        lines.set(1)
        time.sleep(seconds)
        return
    if duty <= 0.0:
        lines.set(0)
        time.sleep(seconds)
        return

    high = period * duty
    low = period - high
    while time.monotonic() < end:
        lines.set(1)
        time.sleep(min(high, max(0.0, end - time.monotonic())))
        lines.set(0)
        time.sleep(min(low, max(0.0, end - time.monotonic())))


def write_csv(path: str, samples: list[Sample]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["second", "duty_percent"])
        for sample in samples:
            writer.writerow([f"{sample.second:.3f}", f"{sample.duty * 100:.1f}"])


def write_svg(path: str, samples: list[Sample]) -> None:
    width = 900
    height = 420
    pad_left = 70
    pad_right = 30
    pad_top = 30
    pad_bottom = 60
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    max_t = max(sample.second for sample in samples) if samples else 1.0

    def x(second: float) -> float:
        return pad_left + (second / max_t) * plot_w

    def y(duty: float) -> float:
        return pad_top + (1.0 - duty) * plot_h

    points = " ".join(f"{x(s.second):.1f},{y(s.duty):.1f}" for s in samples)
    circles = "\n".join(
        f'<circle cx="{x(s.second):.1f}" cy="{y(s.duty):.1f}" r="4" fill="#0f766e" />'
        for s in samples
    )

    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="420" viewBox="0 0 900 420">',
        '<rect width="900" height="420" fill="#ffffff"/>',
        '<text x="450" y="22" text-anchor="middle" font-family="Arial" font-size="18">Motor Command Ramp</text>',
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{height-pad_bottom}" stroke="#111827"/>',
        f'<line x1="{pad_left}" y1="{height-pad_bottom}" x2="{width-pad_right}" y2="{height-pad_bottom}" stroke="#111827"/>',
    ]
    for pct in [0, 25, 50, 75, 100]:
        yy = y(pct / 100)
        lines.append(f'<line x1="{pad_left}" y1="{yy:.1f}" x2="{width-pad_right}" y2="{yy:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="55" y="{yy+5:.1f}" text-anchor="end" font-family="Arial" font-size="12">{pct}%</text>')
    for second in range(0, int(max_t) + 1, 5):
        xx = x(second)
        lines.append(f'<line x1="{xx:.1f}" y1="{height-pad_bottom}" x2="{xx:.1f}" y2="{height-pad_bottom+6}" stroke="#111827"/>')
        lines.append(f'<text x="{xx:.1f}" y="{height-pad_bottom+24}" text-anchor="middle" font-family="Arial" font-size="12">{second}s</text>')
    lines.extend(
        [
            f'<polyline points="{points}" fill="none" stroke="#0f766e" stroke-width="4" stroke-linejoin="round"/>',
            circles,
            '<text x="450" y="402" text-anchor="middle" font-family="Arial" font-size="13">Time</text>',
            '<text x="20" y="210" text-anchor="middle" font-family="Arial" font-size="13" transform="rotate(-90 20 210)">Commanded duty</text>',
            "</svg>",
        ]
    )
    with open(path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines))


def run(args: argparse.Namespace) -> None:
    validate(args)
    lines = None if args.dry_run else MotorLines()
    samples: list[Sample] = []

    try:
        elapsed = 0.0
        while elapsed < args.duration:
            duty = duty_at(elapsed, args.duration, args.min_duty, args.max_duty)
            samples.append(Sample(elapsed, duty))
            print(f"t={elapsed:5.1f}s duty={duty * 100:5.1f}%", flush=True)
            chunk = min(args.step, args.duration - elapsed)
            pwm_for(lines, duty, chunk, args.pwm_frequency)
            elapsed += chunk
        samples.append(Sample(args.duration, duty_at(args.duration, args.duration, args.min_duty, args.max_duty)))
    finally:
        if lines is not None:
            lines.off()
            lines.close()
        print("OFF", flush=True)

    write_csv(args.csv, samples)
    write_svg(args.svg, samples)
    print(f"wrote {args.csv}")
    print(f"wrote {args.svg}")


def main() -> int:
    try:
        run(parse_args())
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
