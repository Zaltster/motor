from __future__ import annotations

import glob
import os
import platform
import stat
from pathlib import Path


def mode(path: str) -> str:
    try:
        return stat.filemode(os.stat(path).st_mode)
    except OSError as exc:
        return f"unavailable ({exc})"


def access(path: str) -> str:
    flags = []
    if os.access(path, os.R_OK):
        flags.append("read")
    if os.access(path, os.W_OK):
        flags.append("write")
    return ",".join(flags) if flags else "no access"


def list_paths(label: str, pattern: str) -> list[str]:
    paths = sorted(glob.glob(pattern))
    print(f"{label}: {len(paths)}")
    for path in paths:
        print(f"  {path}  {mode(path)}  {access(path)}")
    return paths


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def summarize_gpio_chips() -> bool:
    chips = sorted(glob.glob("/sys/class/gpio/gpiochip*"))
    print(f"sysfs gpio chips: {len(chips)}")
    saw_rp1 = False
    for chip in chips:
        label = read_text(f"{chip}/label") or "unknown"
        base = read_text(f"{chip}/base") or "unknown"
        ngpio = read_text(f"{chip}/ngpio") or "unknown"
        if "rp1" in label:
            saw_rp1 = True
        print(f"  {chip} label={label} base={base} ngpio={ngpio}")
    return saw_rp1


def main() -> None:
    print("motor-probe: non-moving hardware access check")
    print(f"host={platform.node()} system={platform.system()} machine={platform.machine()}")
    print(f"uid={os.getuid()} gid={os.getgid()}")

    gpio_devices = list_paths("gpio character devices", "/dev/gpiochip*")
    spi_devices = list_paths("spi devices", "/dev/spidev*")
    i2c_devices = list_paths("i2c devices", "/dev/i2c-*")
    pwm_chips = list_paths("pwm sysfs chips", "/sys/class/pwm/pwmchip*")
    saw_rp1 = summarize_gpio_chips()

    gpio_writable = any(os.access(path, os.W_OK) for path in gpio_devices)
    spi_writable = any(os.access(path, os.W_OK) for path in spi_devices)
    pwm_writable = any(os.access(f"{path}/export", os.W_OK) for path in pwm_chips)

    print("verdict:")
    if saw_rp1 and gpio_writable:
        print("  YES: this app can see writable Raspberry Pi GPIO hardware.")
        print("  For the L298N rig, use GPIO12=ENA, GPIO17=IN1, GPIO18=IN2.")
    elif saw_rp1:
        print("  PARTIAL: Raspberry Pi GPIO hardware is visible, but no writable gpiochip was exposed.")
    else:
        print("  NO: Raspberry Pi GPIO hardware was not visible inside this app.")

    if spi_writable:
        print("  SPI is writable if the motor controller is SPI-based.")
    if pwm_writable:
        print("  Kernel PWM export is writable.")
    if not i2c_devices:
        print("  No I2C controller device was visible in this entitlement set.")

    print("  No motor command was sent by this probe.")
