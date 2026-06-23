# Motor Control Runbook

This directory contains the Raspberry Pi 5 + L298N motor-control scripts used
for the vibration/earthquake test rig.

## Current Hardware

- Device: `wendyos-wendy-vibration-pi5-32gb.local`
- Board: Raspberry Pi 5 running WendyOS
- Motor driver: L298N H-bridge
- Motor supply: external supply, tested successfully at `12V`
- Pi logic level: `3.3V`; the Pi does not power the motor

Confirmed signal wiring:

| Wire | Pi BCM GPIO | L298N pin | Purpose |
| --- | ---: | --- | --- |
| Gray | `GPIO12` | `ENA` | Enable / speed control |
| Brown | `GPIO17` | `IN1` | Direction input 1 |
| Black | `GPIO18` | `IN2` | Direction input 2 |
| Blue | `GND` | `GND` | Common ground |

The working GPIO access path is `/dev/gpiochip0`. Do not use the old
`/sys/class/gpio` path for motor control on this Pi; it accepted writes but did
not reliably drive GPIO17 high.

## Run Commands

Full power for 10 seconds:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 1'
```

25% pulse modulation for 10 seconds:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 0.25 --cycle 0.2'
```

5% pulse modulation for 10 seconds:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 0.05 --cycle 0.2'
```

Reverse for 5 seconds:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode reverse --duration 5 --duty 1'
```

Earthquake-style random modulation for 20 seconds:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode earthquake --duration 20 --step 5 --min-duty 0.05 --max-duty 1 --cycle 0.2'
```

## Pulse Modulation

The script varies effective motor power by toggling `ENA`.

With `--cycle 0.2`:

- `--duty 1.0`: always on
- `--duty 0.5`: 100 ms on, 100 ms off
- `--duty 0.25`: 50 ms on, 150 ms off
- `--duty 0.10`: 20 ms on, 180 ms off
- `--duty 0.05`: 10 ms on, 190 ms off

This is low-frequency pulse modulation, not precision 20 kHz hardware PWM. It
was chosen because it was visibly testable on the current L298N setup.

## Files

- `l298n_motor.py`: main working motor script using `/dev/gpiochip0`
- `motor_ramp.py`: up/down ramp runner that writes CSV and SVG command graphs
- `motor_ramp.csv`: latest ramp command log
- `motor_ramp.svg`: latest ramp command graph
- `earthquake_motor.py`: older generic PWM experiment; not the current L298N path
- `motor_probe/` and `cmd/motor-probe/`: hardware probes used during debugging

## Safety

All current scripts set `ENA`, `IN1`, and `IN2` low in a `finally` block. If a
run is interrupted, force the pins off with:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --duration 0.1 --duty 0'
```

If the motor runs at max regardless of duty, check that the L298N `ENA` jumper
is removed and that the gray wire is actually on `ENA`.
