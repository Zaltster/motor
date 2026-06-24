#!/usr/bin/env python3
"""Create HTML/SVG frequency spectra plots for labeled motion recordings."""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from train_motion_model import DEFAULT_SENSOR_ORDER, LABELS, discover_recordings, load_recording, resample_values


COLORS = {
    "no_motion": "#155eef",
    "ambient_motion": "#b45309",
    "earthquake": "#b42318",
}


DISPLAY_LABELS = {
    "no_motion": "No motion",
    "ambient_motion": "Ambient motion",
    "earthquake": "Earthquake",
}


def compute_spectrum(points: list[tuple[float, float]], start: float, end: float, hz: float) -> tuple[np.ndarray, np.ndarray]:
    _grid, values = resample_values(points, start, end, hz)
    if len(values) < 4:
        return np.array([]), np.array([])
    centered = values - float(np.mean(values))
    window = np.hanning(len(centered))
    spectrum = np.abs(np.fft.rfft(centered * window)) ** 2
    freqs = np.fft.rfftfreq(len(centered), d=1.0 / hz)
    if len(spectrum):
        spectrum[0] = 0.0
    total = float(np.sum(spectrum))
    if total > 1e-12:
        spectrum = spectrum / total
    return freqs, spectrum


def interp_power(freqs: np.ndarray, power: np.ndarray, target_freqs: np.ndarray) -> np.ndarray:
    if len(freqs) == 0:
        return np.zeros(len(target_freqs), dtype=float)
    return np.interp(target_freqs, freqs, power, left=0.0, right=0.0)


def recording_points(events: list[dict[str, Any]], sensor_order: list[str]) -> dict[str, list[tuple[float, float]]]:
    by_sensor: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for event in events:
        if event["sensorId"] in sensor_order:
            by_sensor[event["sensorId"]].append((float(event["elapsed"]), float(event["value"])))
    return by_sensor


def polyline(points: list[tuple[float, float]], color: str, width: float = 2.5) -> str:
    if not points:
        return ""
    encoded = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polyline points="{encoded}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"/>'


def spectra_svg(
    title: str,
    series: dict[str, np.ndarray],
    freqs: np.ndarray,
    max_freq: float,
    width: int = 900,
    height: int = 340,
) -> str:
    left = 58
    right = 18
    top = 42
    bottom = 42
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_power = max((float(np.max(values)) for values in series.values() if len(values)), default=1e-9)
    max_power = max(max_power, 1e-9)

    def x_for(freq: float) -> float:
        return left + (freq / max_freq) * plot_w

    def y_for(power: float) -> float:
        return top + (1.0 - min(max(power, 0.0), max_power) / max_power) * plot_h

    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="26" font-size="18" font-weight="700" fill="#101828">{html.escape(title)}</text>',
    ]
    for tick in range(0, int(max_freq) + 1, 2):
        x = x_for(float(tick))
        pieces.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + plot_h}" stroke="#f2f4f7"/>')
        pieces.append(f'<text x="{x:.1f}" y="{height - 14}" text-anchor="middle" font-size="12" fill="#667085">{tick}</text>')
    for index in range(5):
        frac = index / 4
        y = top + plot_h * frac
        pieces.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e4e7ec"/>')
    pieces.append(f'<text x="{width / 2:.1f}" y="{height - 2}" text-anchor="middle" font-size="12" fill="#667085">Frequency (Hz)</text>')

    legend_x = width - 300
    legend_y = 20
    for index, label in enumerate(LABELS):
        y = legend_y + index * 18
        pieces.append(f'<line x1="{legend_x}" x2="{legend_x + 24}" y1="{y}" y2="{y}" stroke="{COLORS[label]}" stroke-width="3"/>')
        pieces.append(f'<text x="{legend_x + 32}" y="{y + 4}" font-size="12" fill="#475467">{DISPLAY_LABELS[label]}</text>')

    mask = freqs <= max_freq
    visible_freqs = freqs[mask]
    for label in LABELS:
        values = series.get(label)
        if values is None or not len(values):
            continue
        visible_values = values[mask]
        points = [(x_for(float(freq)), y_for(float(power))) for freq, power in zip(visible_freqs, visible_values, strict=True)]
        pieces.append(polyline(points, COLORS[label]))
    pieces.append("</svg>")
    return "\n".join(pieces)


def top_frequencies(freqs: np.ndarray, power: np.ndarray, max_freq: float, count: int = 5) -> list[tuple[float, float]]:
    mask = (freqs > 0) & (freqs <= max_freq)
    visible_freqs = freqs[mask]
    visible_power = power[mask]
    if len(visible_power) == 0:
        return []
    indexes = np.argsort(visible_power)[::-1][:count]
    return [(float(visible_freqs[index]), float(visible_power[index])) for index in indexes]


def write_report(
    output_path: Path,
    target_freqs: np.ndarray,
    class_series: dict[str, np.ndarray],
    sensor_series: dict[str, dict[str, np.ndarray]],
    counts: dict[str, int],
    max_freq: float,
) -> None:
    top_rows = []
    for label in LABELS:
        tops = top_frequencies(target_freqs, class_series[label], max_freq)
        top_rows.append(
            "<tr>"
            f"<td>{DISPLAY_LABELS[label]}</td>"
            f"<td>{counts[label]}</td>"
            f"<td>{', '.join(f'{freq:.2f} Hz' for freq, _power in tops)}</td>"
            "</tr>"
        )
    sensor_sections = []
    for sensor_id, series in sensor_series.items():
        sensor_sections.append(f"<section>{spectra_svg(f'Average spectrum by class - {sensor_id}', series, target_freqs, max_freq)}</section>")
    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Motion Frequency Spectra</title>
  <style>
    body {{ margin: 28px; color: #101828; background: #f5f7fb; font-family: Inter, system-ui, sans-serif; }}
    section {{ margin: 18px 0; padding: 18px; border: 1px solid #d0d5dd; border-radius: 8px; background: #fff; }}
    svg {{ width: 100%; height: auto; display: block; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ padding: 8px 10px; border: 1px solid #eaecf0; text-align: left; }}
    p {{ color: #475467; }}
  </style>
</head>
<body>
  <h1>Motion Frequency Spectra</h1>
  <p>Each trace is the average normalized FFT power spectrum for labeled recordings, resampled to a fixed rate before FFT. This shows rhythm shape, not absolute vibration strength.</p>
  <section>{spectra_svg('Average spectrum by class - all sensors combined', class_series, target_freqs, max_freq)}</section>
  <section>
    <h2>Dominant Frequencies</h2>
    <table>
      <thead><tr><th>Label</th><th>Recordings</th><th>Top frequency peaks</th></tr></thead>
      <tbody>{''.join(top_rows)}</tbody>
    </table>
  </section>
  {''.join(sensor_sections)}
</body>
</html>
""",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot class frequency spectra from motion recordings.")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--output-dir", default="reports/motion_model_rhythm_v2")
    parser.add_argument("--hz", type=float, default=20.0)
    parser.add_argument("--max-freq", type=float, default=10.0)
    parser.add_argument("--bins", type=int, default=220)
    parser.add_argument("--sensor-order", action="append")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sensor_order = args.sensor_order or DEFAULT_SENSOR_ORDER
    target_freqs = np.linspace(0.0, args.max_freq, args.bins)
    class_accumulator: dict[str, list[np.ndarray]] = defaultdict(list)
    sensor_accumulator: dict[str, dict[str, list[np.ndarray]]] = {
        sensor_id: defaultdict(list) for sensor_id in sensor_order
    }
    counts: dict[str, int] = defaultdict(int)

    for path in discover_recordings(Path(args.recordings_dir)):
        label, _session_id, events = load_recording(path)
        if label not in LABELS or not events:
            continue
        start = min(float(event["elapsed"]) for event in events)
        end = max(float(event["elapsed"]) for event in events)
        if end - start < 1.0:
            continue
        by_sensor = recording_points(events, sensor_order)
        combined_points = sorted(point for points in by_sensor.values() for point in points)
        freqs, power = compute_spectrum(combined_points, start, end, args.hz)
        class_accumulator[label].append(interp_power(freqs, power, target_freqs))
        counts[label] += 1
        for sensor_id in sensor_order:
            freqs, power = compute_spectrum(by_sensor.get(sensor_id, []), start, end, args.hz)
            sensor_accumulator[sensor_id][label].append(interp_power(freqs, power, target_freqs))

    class_series = {
        label: np.mean(class_accumulator[label], axis=0) if class_accumulator[label] else np.zeros(len(target_freqs))
        for label in LABELS
    }
    sensor_series = {
        sensor_id: {
            label: np.mean(sensor_accumulator[sensor_id][label], axis=0)
            if sensor_accumulator[sensor_id][label]
            else np.zeros(len(target_freqs))
            for label in LABELS
        }
        for sensor_id in sensor_order
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "frequency_spectra.html"
    write_report(html_path, target_freqs, class_series, sensor_series, counts, args.max_freq)
    json_path = output_dir / "frequency_spectra.json"
    json_path.write_text(
        json.dumps(
            {
                "frequencies": target_freqs.tolist(),
                "counts": dict(counts),
                "classSeries": {label: values.tolist() for label, values in class_series.items()},
                "sensorSeries": {
                    sensor_id: {label: values.tolist() for label, values in values_by_label.items()}
                    for sensor_id, values_by_label in sensor_series.items()
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Saved spectra report: {html_path}")
    print(f"Saved spectra data: {json_path}")
    for label in LABELS:
        tops = top_frequencies(target_freqs, class_series[label], args.max_freq)
        print(f"{DISPLAY_LABELS[label]} top peaks: {', '.join(f'{freq:.2f}Hz' for freq, _power in tops)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
