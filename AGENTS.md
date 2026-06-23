# Agent Notes For This Motor Workspace

Use this file as the first local context before touching the motor.

## Device Access

- Wendy device: `wendyos-wendy-vibration-pi5-32gb.local`
- Root SSH works:
  `ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local ...`
- The working scripts live on the Pi under `/data/motor/`.

## Confirmed Wiring

- Gray wire: `GPIO12` -> L298N `ENA`
- Brown wire: `GPIO17` -> L298N `IN1`
- Black wire: `GPIO18` -> L298N `IN2`
- Blue wire: Pi `GND` -> L298N `GND`
- External motor supply was tested at `12V`.

## Critical Implementation Detail

Use `/dev/gpiochip0` and GPIO character-device ioctls for motor control.

Do not rely on `/sys/class/gpio` for the working motor path. During debugging,
sysfs exports for GPIO17/GPIO18 appeared writable, but readback stayed `0` and
did not reliably drive the L298N inputs. Releasing sysfs exports and using
`/dev/gpiochip0` worked.

On this Pi 5, `/dev/gpiochip0` line offsets match BCM GPIO numbers for the
needed lines:

- `12` = ENA
- `17` = IN1
- `18` = IN2

## Commands

Copy updated scripts to the Pi:

```bash
scp -o StrictHostKeyChecking=accept-new l298n_motor.py root@wendyos-wendy-vibration-pi5-32gb.local:/data/motor/l298n_motor.py
scp -o StrictHostKeyChecking=accept-new motor_ramp.py root@wendyos-wendy-vibration-pi5-32gb.local:/data/motor/motor_ramp.py
```

Full power:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 1'
```

25% pulse modulation:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 0.25 --cycle 0.2'
```

Earthquake mode:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode earthquake --duration 20 --step 5 --min-duty 0.05 --max-duty 1 --cycle 0.2'
```

## Verification Limits

Software can verify that GPIO commands were issued. It cannot verify actual
motor motion without feedback hardware such as an encoder, current sensor, or
camera. For electrical verification, measure:

- L298N `IN1` to GND: should be about 3.3V when brown/GPIO17 is high
- L298N `IN2` to GND: should be 0V when black/GPIO18 is low
- L298N `ENA` to GND: should pulse according to duty when gray/GPIO12 is active
- L298N motor supply to GND: external supply voltage, tested at 12V
