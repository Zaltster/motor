# Motor Runbook

## One-Time Setup On The Pi

```bash
mkdir -p /data/motor
```

Copy scripts from this workspace to the Pi:

```bash
scp -o StrictHostKeyChecking=accept-new l298n_motor.py root@wendyos-wendy-vibration-pi5-32gb.local:/data/motor/l298n_motor.py
scp -o StrictHostKeyChecking=accept-new motor_ramp.py root@wendyos-wendy-vibration-pi5-32gb.local:/data/motor/motor_ramp.py
```

## Standard Runs

Full power for 10 seconds:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 1'
```

50% with a 0.2 second pulse cycle:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 0.5 --cycle 0.2'
```

25% with a 0.2 second pulse cycle:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 0.25 --cycle 0.2'
```

5% with a 0.2 second pulse cycle:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 10 --duty 0.05 --cycle 0.2'
```

Reverse:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode reverse --duration 5 --duty 1'
```

Earthquake-style random pulsing:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode earthquake --duration 20 --step 5 --min-duty 0.05 --max-duty 1 --cycle 0.2'
```

## Ramp With Graph

Run an up/down ramp and create CSV/SVG outputs on the Pi:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/motor_ramp.py --duration 20 --step 1 --min-duty 0 --max-duty 1 --csv /data/motor/motor_ramp.csv --svg /data/motor/motor_ramp.svg'
```

Copy the graph back:

```bash
scp -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local:/data/motor/motor_ramp.svg ./motor_ramp.svg
scp -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local:/data/motor/motor_ramp.csv ./motor_ramp.csv
```

## Stop / Off

The scripts turn the pins off in `finally`. To intentionally command off:

```bash
ssh -o StrictHostKeyChecking=accept-new root@wendyos-wendy-vibration-pi5-32gb.local \
  'python3 /data/motor/l298n_motor.py --mode forward --duration 0.1 --duty 0'
```
