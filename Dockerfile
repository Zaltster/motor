FROM node:22-bookworm-slim AS frontend

WORKDIR /src

COPY package.json package-lock.json tsconfig.json vite.config.ts ./
COPY frontend ./frontend
RUN npm ci && npm run build

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    SENSOR_MODE=auto \
    WIT_SERIAL_PORTS=/dev/ttyUSB0,/dev/ttyUSB1,/dev/ttyUSB2 \
    WIT_BAUD=115200 \
    WIT_BLE_SERVICE_UUIDS=0000ffe5-0000-1000-8000-00805f9a34fb

RUN pip install --no-cache-dir "bleak>=0.22,<1"

COPY l298n_motor.py rumble_ui.py ./
COPY --from=frontend /src/static ./static

EXPOSE 8000

ENTRYPOINT ["python3", "/app/rumble_ui.py", "--host", "0.0.0.0", "--port", "8000"]
