# Hardware Notes

## Pi

- Hostname: `wendyos-wendy-vibration-pi5-32gb.local`
- Model: Raspberry Pi 5 Model B
- OS: WendyOS
- Access: root SSH

## Motor Driver

The connected motor driver is an L298N. It does not enumerate as a USB, I2C,
SPI, or serial device. Linux cannot "see" the L298N as a named device because
it is controlled by plain GPIO wires.

## Wiring

| Wire | Pi BCM GPIO | Header note | L298N pin |
| --- | ---: | --- | --- |
| Gray | `GPIO12` | physical pin 32 | `ENA` |
| Brown | `GPIO17` | physical pin 11 | `IN1` |
| Black | `GPIO18` | physical pin 12 | `IN2` |
| Blue | `GND` | any Pi ground | `GND` |

The motor power is external. The Pi GPIO pins only provide 3.3V logic signals.

## L298N Direction Logic

Forward:

```text
IN1 = HIGH
IN2 = LOW
ENA = HIGH or pulsed
```

Reverse:

```text
IN1 = LOW
IN2 = HIGH
ENA = HIGH or pulsed
```

Off/coast:

```text
ENA = LOW
IN1 = LOW
IN2 = LOW
```

## Speed Control

Speed is controlled by pulsing `ENA`, not by changing the Pi's voltage. The Pi
does not output 12V. The L298N applies the external motor supply when enabled.

For speed control to work, the L298N `ENA` jumper must be removed and the gray
wire must be attached to the actual `ENA` pin.
