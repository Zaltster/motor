# Troubleshooting

## The motor runs at max for every duty

Likely causes:

- L298N `ENA` jumper is installed, so `ENA` is permanently enabled.
- Gray wire is not on the actual `ENA` pin.
- Motor is connected to the other L298N channel, which would require `ENB`,
  `IN3`, and `IN4`.

## The motor does not move

Check:

- External motor supply is connected to L298N motor power input.
- L298N ground, Pi ground, and motor supply ground are common.
- Motor is connected to the matching output channel.
- Brown wire is on `IN1`.
- Black wire is on `IN2`.
- Gray wire is on `ENA`, or the `ENA` jumper is installed for full enable.

## Linux does not show the L298N as a device

That is expected. L298N is not a USB/I2C/SPI device. It is just an H-bridge
controlled by logic wires, so the Pi can only drive pins; it cannot enumerate
or identify the L298N.

## Sysfs GPIO looked writable but did not work

This happened during debugging. `/sys/class/gpio/gpio586` and related paths
reported as writable, but setting GPIO17 high read back as `0`.

The working path is `/dev/gpiochip0` using the GPIO character-device API.
Use `l298n_motor.py` or `motor_ramp.py`, not the old sysfs scripts, for real
motor runs.

## Verifying With A Multimeter

Put the black probe on L298N GND.

During forward command:

- Brown / `IN1`: about 3.3V
- Black / `IN2`: about 0V
- Gray / `ENA`: pulsing or high depending on duty
- Motor supply input: external supply voltage, such as 12V

Across the motor output terminals, voltage should appear when enabled. Polarity
should flip when running reverse.

## Software Verification Limits

Without a sensor, software cannot prove the motor physically moved. It can only
prove commands were sent to GPIO. Add an encoder, current sensor, limit switch,
or camera if automatic motion verification is needed.
