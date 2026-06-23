#!/usr/bin/env python3
"""Random PWM motor drive pattern for a Raspberry Pi.

The Pi does not output 6V directly. It sends a PWM control signal to a motor
controller, and the motor controller should be powered from a 6V motor supply.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Pulse:
    second: float
    volts: float
    duty_cycle: float


class DryRunPWM:
    def __init__(self, pin: int) -> None:
        self.pin = pin
        self.value = 0.0

    def close(self) -> None:
        self.value = 0.0

    def hold(self, duration: float, should_stop) -> None:
        end = time.monotonic() + duration
        while not should_stop() and time.monotonic() < end:
            time.sleep(0.02)


class SysfsGPIOPWM:
    def __init__(self, bcm_pin: int, frequency: float, gpio_base: int | None) -> None:
        self.bcm_pin = bcm_pin
        self.frequency = frequency
        self.value = 0.0
        self.gpio_number = (find_rp1_gpio_base() if gpio_base is None else gpio_base) + bcm_pin
        self.path = Path(f"/sys/class/gpio/gpio{self.gpio_number}")
        self._export()
        self._write("direction", "low")

    def _export(self) -> None:
        if self.path.exists():
            return
        Path("/sys/class/gpio/export").write_text(str(self.gpio_number), encoding="utf-8")
        for _ in range(50):
            if self.path.exists():
                return
            time.sleep(0.01)
        raise RuntimeError(f"GPIO {self.gpio_number} did not appear after export")

    def _write(self, name: str, value: str) -> None:
        (self.path / name).write_text(value, encoding="utf-8")

    def hold(self, duration: float, should_stop) -> None:
        end = time.monotonic() + duration
        period = 1.0 / self.frequency
        while not should_stop() and time.monotonic() < end:
            duty = max(0.0, min(1.0, self.value))
            high_time = period * duty
            low_time = period - high_time

            if high_time > 0:
                self._write("value", "1")
                time.sleep(min(high_time, max(0.0, end - time.monotonic())))
            if low_time > 0 and not should_stop():
                self._write("value", "0")
                time.sleep(min(low_time, max(0.0, end - time.monotonic())))

        self._write("value", "0")

    def close(self) -> None:
        self.value = 0.0
        if self.path.exists():
            self._write("value", "0")


def find_rp1_gpio_base() -> int:
    for chip in Path("/sys/class/gpio").glob("gpiochip*"):
        label_path = chip / "label"
        base_path = chip / "base"
        try:
            label = label_path.read_text(encoding="utf-8").strip()
            if "pinctrl-rp1" in label:
                return int(base_path.read_text(encoding="utf-8").strip())
        except OSError:
            continue
    raise RuntimeError("could not find Raspberry Pi RP1 GPIO base")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive a random earthquake-like motor pattern from a Raspberry Pi."
    )
    parser.add_argument("--pin", type=int, default=18, help="GPIO PWM command pin.")
    parser.add_argument(
        "--backend",
        choices=["auto", "gpiozero", "sysfs-gpio"],
        default="auto",
        help="GPIO backend to use on the Raspberry Pi.",
    )
    parser.add_argument(
        "--gpio-base",
        type=int,
        default=None,
        help="Sysfs GPIO base. Defaults to the detected Raspberry Pi RP1 base.",
    )
    parser.add_argument(
        "--frequency",
        type=float,
        default=50.0,
        help="Software PWM frequency in Hz for the sysfs-gpio backend.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=20.0,
        help="Total run time in seconds.",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=5.0,
        help="Seconds between random voltage changes.",
    )
    parser.add_argument(
        "--max-voltage",
        type=float,
        default=6.0,
        help="Maximum commanded motor voltage.",
    )
    parser.add_argument(
        "--supply-voltage",
        type=float,
        default=6.0,
        help="Motor-controller supply voltage used to compute PWM duty cycle.",
    )
    parser.add_argument(
        "--min-voltage",
        type=float,
        default=0.0,
        help="Minimum commanded motor voltage.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for repeatable tests.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pattern without touching GPIO hardware.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.duration <= 0:
        raise ValueError("--duration must be greater than 0")
    if args.step <= 0:
        raise ValueError("--step must be greater than 0")
    if args.supply_voltage <= 0:
        raise ValueError("--supply-voltage must be greater than 0")
    if args.frequency <= 0:
        raise ValueError("--frequency must be greater than 0")
    if args.min_voltage < 0:
        raise ValueError("--min-voltage cannot be negative")
    if args.max_voltage < args.min_voltage:
        raise ValueError("--max-voltage must be >= --min-voltage")
    if args.max_voltage > args.supply_voltage:
        raise ValueError("--max-voltage cannot exceed --supply-voltage")


def build_pwm(args: argparse.Namespace):
    if args.dry_run:
        return DryRunPWM(args.pin)

    if args.backend == "sysfs-gpio":
        return SysfsGPIOPWM(args.pin, args.frequency, args.gpio_base)

    if args.backend == "auto" and os.path.exists("/sys/class/gpio/export"):
        try:
            return SysfsGPIOPWM(args.pin, args.frequency, args.gpio_base)
        except Exception:
            pass

    return build_gpiozero_pwm(args.pin)


def build_gpiozero_pwm(pin: int):
    try:
        from gpiozero import PWMOutputDevice
    except ImportError as exc:
        raise RuntimeError(
            "gpiozero is required for the gpiozero backend. Install it or run "
            "with: --backend sysfs-gpio"
        ) from exc

    return PWMOutputDevice(pin, frequency=1000, initial_value=0.0)


def sleep_for(duration: float, should_stop) -> None:
    end = time.monotonic() + duration
    while not should_stop() and time.monotonic() < end:
        time.sleep(0.02)


def hold_pwm(pwm, duration: float, should_stop) -> None:
    hold = getattr(pwm, "hold", None)
    if hold is not None:
        hold(duration, should_stop)
    else:
        sleep_for(duration, should_stop)


def random_pulses(args: argparse.Namespace) -> list[Pulse]:
    pulses = []
    elapsed = 0.0

    while elapsed < args.duration:
        volts = random.uniform(args.min_voltage, args.max_voltage)
        duty_cycle = volts / args.supply_voltage
        pulses.append(Pulse(second=elapsed, volts=volts, duty_cycle=duty_cycle))
        elapsed += args.step

    return pulses


def run(args: argparse.Namespace) -> None:
    validate_args(args)
    random.seed(args.seed)

    pwm = build_pwm(args)
    interrupted = False

    def stop(_signum, _frame) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(
        f"Starting: pin=GPIO{args.pin}, duration={args.duration:g}s, "
        f"step={args.step:g}s, range={args.min_voltage:g}-{args.max_voltage:g}V"
    )

    start = time.monotonic()
    try:
        for pulse in random_pulses(args):
            if interrupted:
                break

            pwm.value = pulse.duty_cycle
            print(
                f"t={pulse.second:5.1f}s  command={pulse.volts:4.2f}V  "
                f"duty={pulse.duty_cycle * 100:5.1f}%"
            )

            elapsed = time.monotonic() - start
            next_time = min(pulse.second + args.step, args.duration)
            hold_pwm(pwm, max(0.0, next_time - elapsed), lambda: interrupted)
    finally:
        pwm.value = 0.0
        pwm.close()
        print("Stopped: motor command set to 0V")


def main() -> int:
    try:
        run(parse_args())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
