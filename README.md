# Wendy Studio Vibration Tower Earthquake Demo

This project runs a Raspberry Pi 5 + L298N vibration rig from a browser UI. It
can run one Wendy Studio earthquake demo sequence and graph the shaking reported
by three WIT Motion sensors on different floors.

The deployed WendyOS app serves a Vite + React + TypeScript dashboard, controls
the motor through GPIO, reads WIT serial or BLE data, classifies floor response,
and streams live updates to the browser with Server-Sent Events.

## Current Hardware

- Device: `wendyos-wendy-vibration-pi5-32gb.local`
- Board: Raspberry Pi 5 running WendyOS
- Motor driver: L298N H-bridge
- Motor supply: external supply, tested successfully at `12V`
- Pi logic level: `3.3V`; the Pi does not power the motor
- Target sensors: one BLE WIT Motion sensor on each floor
- Verified fallback sensors: three wired WIT Motion sensors through CH341 USB serial adapters

## Linear Project Alignment

This repository implements the Wendy Studio Vibration Tower Earthquake Demo:

- Three-floor tower/nightstand model with a vibration motor on the bottom layer.
- WendyOS drives the 12 V vibration motor through the Pi + L298N GPIO path.
- One demo run starts after a randomized delay and runs a 20 second ramping
  earthquake sequence.
- WendyOS streams live floor vibration data and sensor connection state.
- The frontend is Vite, React, TypeScript, and shadcn-style components.
- The dashboard shows live graphs for each floor, total shake, motor command
  timeline, and per-sensor connection state.
- The classifier exposes exactly three sensor-only states: `No motion`,
  `Non-Earthquake motion detected`, and `Earthquake detected`.
- The classifier does not use motor/demo state to decide whether an earthquake
  was detected.

The Wendy app is BLE-capable through the `bluetooth` entitlement and the
`bleak` Python package. The current bench setup still keeps serial/USB
entitlements because wired WIT adapters are still useful as the preferred
regular connection when present.

In `SENSOR_MODE=auto`, the backend streams serial and BLE at the same time. The
dashboard and classifier prefer serial sensor data while serial is actively
producing packets, and automatically display/use BLE data when serial is absent
or inactive.

Verified WIT BLE details:

| Purpose | UUID |
| --- | --- |
| Advertised service filter | `0000ffe5-0000-1000-8000-00805f9a34fb` |
| Notification characteristic | `0000ffe4-0000-1000-8000-00805f9a34fb` |

Mac-side BLE test:

```bash
.venv/bin/python tools/ble_wit_stream.py \
  --scan-seconds 30 \
  --stream-seconds 10 \
  --max-devices 3 \
  --scan-service-uuid 0000ffe5-0000-1000-8000-00805f9a34fb
```

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

The `Start` button sends one `/api/demo/start` request. The backend arms the
demo, waits a randomized delay, runs one 20 second ramping earthquake sequence,
drives the motor forward through the L298N, and publishes the commanded rumble
strength to the UI. `/api/rumble` remains as a compatibility alias.

The UI shows:

- demo state and commanded rumble strength over time
- Total Shake, calculated from the latest active floor sensor signals
- separate Top Floor, Middle Floor, and Bottom Floor sensor graphs
- per-floor connection state
- sensor-only classifier state: `No motion`,
  `Non-Earthquake motion detected`, or `Earthquake detected`

The app is configured in `Dockerfile` and `wendy.json`. Wendy grants host
networking, Bluetooth access for BLE sensors, GPIO access for pins `12`, `17`,
and `18`, USB access, and serial access to `ttyUSB0`, `ttyUSB1`, and `ttyUSB2`
for the current wired fallback setup.

For laptop/UI testing without touching hardware:

```bash
MOTOR_DRY_RUN=1 SIMULATE_SENSOR=1 python3 rumble_ui.py --host 127.0.0.1 --port 8000
```

For explicit BLE-only sensor mode:

```bash
SENSOR_MODE=ble WIT_BLE_SERVICE_UUIDS=0000ffe5-0000-1000-8000-00805f9a34fb python3 rumble_ui.py --host 0.0.0.0 --port 8000
```

For frontend development:

```bash
npm install
npm run dev
```

For production frontend assets served by `rumble_ui.py`:

```bash
npm run build
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
- `rumble_ui.py`: Wendy/browser app backend, HTTP API, SSE stream, demo state machine, classifier, motor runner, and WIT sensor reader
- `frontend/`: Vite React TypeScript dashboard source
- `static/`: generated production frontend assets served by the Python backend
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
   `POST /api/demo/start`, `POST /api/demo/stop`, exposes `/health`, and
   streams `/events`.
2. `DemoController` runs one randomized-delay, 20 second ramping earthquake
   sequence in a background thread. It uses the existing `l298n_motor.py` GPIO
   code and always shuts the motor off in a `finally` block.
3. `SensorReader` streams configured WIT serial ports and WIT BLE sensors. In
   auto mode, serial and BLE run concurrently; serial is preferred while active,
   with BLE as the automatic display/classifier fallback. It parses standard
   `0x55 0x51/0x52/0x53` WIT packets and the observed 32-byte `0x55 0x61`
   frames. The UI currently uses the wide-frame shake metric as a relative
   signal, not a calibrated engineering unit.
4. `SensorClassifier` uses rolling windows across floors. It does not read demo
   or motor state; it only labels the live sensor response as no motion,
   non-earthquake motion, or earthquake detected.

The frontend opens one `EventSource` to `/events`. Demo events update the
commanded-strength chart. Sensor and classification events update Total Shake,
per-floor charts, connection state, and classifier state.

## Safety

All current scripts set `ENA`, `IN1`, and `IN2` low in a `finally` block. If a
run is interrupted, force the pins off with:

```bash
ssh root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --duration 0.1 --duty 0'
```

If the motor runs at max regardless of duty, check that the L298N `ENA` jumper
is removed and that the gray wire is actually on `ENA`.
