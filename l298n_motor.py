#!/usr/bin/env python3
"""Control the L298N motor driver from the Raspberry Pi 5.

Confirmed working wiring:
  gray  -> GPIO12 -> ENA / enable / speed
  brown -> GPIO17 -> IN1
  black -> GPIO18 -> IN2
  blue  -> GND

This script uses /dev/gpiochip0 instead of /sys/class/gpio. On this Pi 5,
the sysfs GPIO path accepted writes but did not reliably drive GPIO17 high.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import random
import time


GPIOCHIP = "/dev/gpiochip0"
ENABLE_LINE = 12
IN1_LINE = 17
IN2_LINE = 18

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


class Motor:
    def __init__(self) -> None:
        request = bytearray(364)
        for idx, line in enumerate([ENABLE_LINE, IN1_LINE, IN2_LINE]):
            request[idx * 4 : idx * 4 + 4] = line.to_bytes(4, "little")
        request[256:260] = GPIOHANDLE_REQUEST_OUTPUT.to_bytes(4, "little")
        request[260] = 0
        request[261] = 0
        request[262] = 0
        label = b"l298n-motor"
        request[324 : 324 + len(label)] = label
        request[356:360] = (3).to_bytes(4, "little")

        chip = os.open(GPIOCHIP, os.O_RDONLY)
        try:
            fcntl.ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, request, True)
            self.fd = int.from_bytes(request[360:364], "little", signed=True)
        finally:
            os.close(chip)

        self.values = bytearray(64)

    def set(self, enable: int, in1: int, in2: int) -> None:
        self.values[0] = 1 if enable else 0
        self.values[1] = 1 if in1 else 0
        self.values[2] = 1 if in2 else 0
        fcntl.ioctl(self.fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, self.values, True)

    def direction(self, mode: str, enable: int = 0) -> None:
        if mode == "forward":
            self.set(enable, 1, 0)
        elif mode == "reverse":
            self.set(enable, 0, 1)
        else:
            raise ValueError(f"unknown direction: {mode}")

    def off(self) -> None:
        self.set(0, 0, 0)

    def close(self) -> None:
        os.close(self.fd)


class DryMotor:
    def set(self, enable: int, in1: int, in2: int) -> None:
        print(f"ENA={enable} IN1={in1} IN2={in2}")

    def direction(self, mode: str, enable: int = 0) -> None:
        if mode == "forward":
            self.set(enable, 1, 0)
        elif mode == "reverse":
            self.set(enable, 0, 1)
        else:
            raise ValueError(f"unknown direction: {mode}")

    def off(self) -> None:
        self.set(0, 0, 0)

    def close(self) -> None:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control the L298N motor.")
    parser.add_argument("--mode", choices=["forward", "reverse", "earthquake"], default="forward")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--duty", type=float, default=1.0, help="0.0 to 1.0")
    parser.add_argument("--cycle", type=float, default=0.2, help="Pulse cycle seconds.")
    parser.add_argument("--step", type=float, default=5.0, help="Earthquake update interval.")
    parser.add_argument("--min-duty", type=float, default=0.05)
    parser.add_argument("--max-duty", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def validate(args: argparse.Namespace) -> None:
    if args.duration <= 0:
        raise ValueError("--duration must be greater than 0")
    if args.cycle <= 0:
        raise ValueError("--cycle must be greater than 0")
    if args.step <= 0:
        raise ValueError("--step must be greater than 0")
    for name in ("duty", "min_duty", "max_duty"):
        value = getattr(args, name)
        if value < 0 or value > 1:
            raise ValueError(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.min_duty > args.max_duty:
        raise ValueError("--min-duty must be <= --max-duty")


def pulse(motor: Motor | DryMotor, direction: str, duty: float, duration: float, cycle: float) -> None:
    duty = max(0.0, min(1.0, duty))
    on_time = cycle * duty
    off_time = cycle - on_time
    end = time.monotonic() + duration

    if duty >= 1.0:
        motor.direction(direction, enable=1)
        time.sleep(duration)
        return
    if duty <= 0.0:
        motor.direction(direction, enable=0)
        time.sleep(duration)
        return

    while time.monotonic() < end:
        motor.direction(direction, enable=1)
        time.sleep(min(on_time, max(0.0, end - time.monotonic())))
        motor.direction(direction, enable=0)
        time.sleep(min(off_time, max(0.0, end - time.monotonic())))


def run(args: argparse.Namespace) -> None:
    validate(args)
    random.seed(args.seed)
    motor = DryMotor() if args.dry_run else Motor()

    try:
        motor.off()
        time.sleep(0.2)

        if args.mode == "earthquake":
            elapsed = 0.0
            while elapsed < args.duration:
                chunk = min(args.step, args.duration - elapsed)
                duty = random.uniform(args.min_duty, args.max_duty)
                print(
                    f"earthquake t={elapsed:.1f}s duty={duty * 100:.0f}% "
                    f"cycle={args.cycle:.3f}s for {chunk:.1f}s",
                    flush=True,
                )
                pulse(motor, "forward", duty, chunk, args.cycle)
                elapsed += chunk
        else:
            print(
                f"{args.mode} duty={args.duty * 100:.0f}% "
                f"cycle={args.cycle:.3f}s duration={args.duration:.1f}s",
                flush=True,
            )
            pulse(motor, args.mode, args.duty, args.duration, args.cycle)
    finally:
        motor.off()
        motor.close()
        print("OFF", flush=True)


def main() -> int:
    try:
        run(parse_args())
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
