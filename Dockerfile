FROM --platform=linux/arm64 python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    SENSOR_MODE=serial \
    WIT_SERIAL_PORTS=/dev/ttyUSB0,/dev/ttyUSB1,/dev/ttyUSB2 \
    WIT_BAUD=115200

COPY l298n_motor.py rumble_ui.py ./
COPY static ./static

EXPOSE 8000

ENTRYPOINT ["python3", "/app/rumble_ui.py", "--host", "0.0.0.0", "--port", "8000"]
