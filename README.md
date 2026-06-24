# Earthquake Rumble Monitor

This project runs a Raspberry Pi 5 + L298N vibration rig from a browser UI. It
can trigger one earthquake-style rumble and graph the shaking reported by three
wired WIT Motion sensors on different floors.

The deployed WendyOS app serves a small HTTP UI, controls the motor through
GPIO, reads WIT serial data from USB adapters, and streams live updates to the
browser with Server-Sent Events.

## Current Hardware

- Device: `wendyos-wendy-vibration-pi5-32gb.local`
- Board: Raspberry Pi 5 running WendyOS
- Motor driver: L298N H-bridge
- Motor supply: external supply, tested successfully at `12V`
- Pi logic level: `3.3V`; the Pi does not power the motor
- Sensors: three wired WIT Motion sensors through CH341 USB serial adapters

Floor sensor mapping:

| Floor label | Serial device |
| --- | --- |
| Top Floor | `/dev/ttyUSB0` |
| Middle Floor | `/dev/ttyUSB1` |
| Bottom Floor | `/dev/ttyUSB2` |

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

The normal path is the Wendy browser app:

```bash
wendy run --yes --detach --build-type docker --device wendyos-wendy-vibration-pi5-32gb.local
```

Then open:

```text
http://wendyos-wendy-vibration-pi5-32gb.local:8000/
```

The `Start Earthquake` button sends one `/api/rumble` request. The backend
chooses randomized duty chunks, drives the motor forward through the L298N, and
publishes the commanded rumble strength to the UI.

The UI shows:

- commanded rumble strength over time
- Total Shake, calculated by adding all active floor sensor signals together
- separate Top Floor, Middle Floor, and Bottom Floor sensor graphs

The app is configured in `Dockerfile` and `wendy.json`. Wendy grants host
networking, GPIO access for pins `12`, `17`, and `18`, USB access, and serial
access to `ttyUSB0`, `ttyUSB1`, and `ttyUSB2`.

For laptop/UI testing without touching hardware:

```bash
MOTOR_DRY_RUN=1 SIMULATE_SENSOR=1 python3 rumble_ui.py --host 127.0.0.1 --port 8000
```

Useful Wendy checks:

```bash
wendy --json device apps list --device wendyos-wendy-vibration-pi5-32gb.local
wendy --json device logs --app rumble_ui --device wendyos-wendy-vibration-pi5-32gb.local
```

Legacy direct SSH motor commands still work if you copy `l298n_motor.py` to the
Pi manually.

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
- `rumble_ui.py`: Wendy/browser app backend, HTTP API, SSE stream, motor runner, and WIT sensor reader
- `static/`: browser UI for the earthquake button and live graphs
- `wendy.json`: WendyOS app config and hardware entitlements
- `Dockerfile`: deployable Python runtime for Wendy
- `motor_ramp.py`: up/down ramp runner that writes CSV and SVG command graphs
- `motor_ramp.csv`: latest ramp command log
- `motor_ramp.svg`: latest ramp command graph
- `earthquake_motor.py`: older generic PWM experiment; not the current L298N path
- `motor_probe/` and `cmd/motor-probe/`: hardware probes used during debugging

## How The App Works

`rumble_ui.py` starts three cooperating pieces:

1. `ThreadingHTTPServer` serves `static/index.html`, accepts
   `POST /api/rumble`, exposes `/health`, and streams `/events`.
2. `RumbleController` runs each earthquake in a background thread. It uses the
   existing `l298n_motor.py` GPIO code and always shuts the motor off in a
   `finally` block.
3. `SensorReader` opens the configured WIT serial ports at `115200` baud. It
   parses standard `0x55 0x51/0x52/0x53` WIT packets and the observed 32-byte
   `0x55 0x61` wired frames. The UI currently uses the wide-frame shake metric
   as a relative signal, not a calibrated engineering unit.

The frontend opens one `EventSource` to `/events`. Rumble events update the
commanded-strength chart. Sensor events update the Total Shake chart and each
floor chart. Total Shake is the sum of the latest active sensor signals.

## Safety

All current scripts set `ENA`, `IN1`, and `IN2` low in a `finally` block. If a
run is interrupted, force the pins off with:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --duration 0.1 --duty 0'
```

If the motor runs at max regardless of duty, check that the L298N `ENA` jumper
is removed and that the gray wire is actually on `ENA`.
