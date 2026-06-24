#!/usr/bin/env python3
"""Plot RandomForest training/validation metrics as trees are added."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from sklearn.model_selection import GroupShuffleSplit

from evaluate_motion_windows import load_dataset
from train_motion_model import DEFAULT_SENSOR_ORDER


METRICS = [
    ("accuracy", "Accuracy", "#155eef"),
    ("precision", "Macro precision", "#7c3aed"),
    ("recall", "Macro recall", "#b45309"),
    ("f1", "Macro F1", "#0f766e"),
]


def score_model(model: RandomForestClassifier, x: np.ndarray, y: np.ndarray) -> dict[str, float]:
    pred = model.predict(x)
    precision, recall, f1, _support = precision_recall_fscore_support(y, pred, average="macro", zero_division=0)
    return {
        "accuracy": float(accuracy_score(y, pred)),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
    }


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def line_svg(points: list[dict[str, Any]], metric: str, title: str, color: str) -> str:
    width = 900
    height = 320
    left = 64
    right = 24
    top = 42
    bottom = 48
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_epoch = max(point["trees"] for point in points)

    def x_for(trees: int) -> float:
        return left + (trees / max_epoch) * plot_w

    def y_for(value: float) -> float:
        return top + (1.0 - value) * plot_h

    def polyline(key: str, stroke: str, dash: str = "") -> str:
        coords = " ".join(f"{x_for(point['trees']):.1f},{y_for(point[key][metric]):.1f}" for point in points)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        return f'<polyline points="{coords}" fill="none" stroke="{stroke}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"{dash_attr}/>'

    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="26" font-size="18" font-weight="700" fill="#101828">{html.escape(title)}</text>',
    ]
    for i in range(6):
        value = i / 5
        y = y_for(value)
        pieces.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e4e7ec"/>')
        pieces.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="#667085">{pct(value)}</text>')
    for trees in [point["trees"] for point in points]:
        x = x_for(trees)
        pieces.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top + plot_h}" y2="{top + plot_h + 5}" stroke="#98a2b3"/>')
        pieces.append(f'<text x="{x:.1f}" y="{height - 16}" text-anchor="middle" font-size="11" fill="#667085">{trees}</text>')
    pieces.append(polyline("train", "#98a2b3", "5 4"))
    pieces.append(polyline("validation", color))
    pieces.append(f'<text x="{width - 170}" y="24" font-size="12" fill="{color}">validation</text>')
    pieces.append(f'<text x="{width - 88}" y="24" font-size="12" fill="#667085">train dashed</text>')
    pieces.append(f'<text x="{width / 2:.1f}" y="{height - 2}" text-anchor="middle" font-size="12" fill="#667085">Trees in forest, epoch proxy</text>')
    pieces.append("</svg>")
    return "\n".join(pieces)


def write_report(points: list[dict[str, Any]], output_path: Path) -> None:
    rows = []
    for point in points:
        rows.append(
            "<tr>"
            f"<td>{point['trees']}</td>"
            f"<td>{pct(point['validation']['accuracy'])}</td>"
            f"<td>{pct(point['validation']['precision'])}</td>"
            f"<td>{pct(point['validation']['recall'])}</td>"
            f"<td>{pct(point['validation']['f1'])}</td>"
            f"<td>{pct(point['train']['f1'])}</td>"
            "</tr>"
        )
    charts = "\n".join(f"<section>{line_svg(points, key, title, color)}</section>" for key, title, color in METRICS)
    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>RandomForest Training Progress</title>
  <style>
    body {{ margin: 28px; color: #101828; background: #f5f7fb; font-family: Inter, system-ui, sans-serif; }}
    section {{ margin: 18px 0; padding: 18px; border: 1px solid #d0d5dd; border-radius: 8px; background: #fff; }}
    svg {{ width: 100%; height: auto; display: block; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ padding: 8px 10px; border: 1px solid #eaecf0; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    p {{ color: #475467; }}
  </style>
</head>
<body>
  <h1>RandomForest Training Progress</h1>
  <p>RandomForest has no neural-network epochs. These graphs use tree count as an epoch proxy and validate on held-out recording sessions.</p>
  <section>
    <h2>Validation Summary</h2>
    <table>
      <thead><tr><th>Trees</th><th>Accuracy</th><th>Macro precision</th><th>Macro recall</th><th>Macro F1</th><th>Train Macro F1</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </section>
  {charts}
</body>
</html>
""",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot RandomForest training progress.")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--output-dir", default="reports/motion_model_rhythm_v2")
    parser.add_argument("--window", type=float, default=5.0)
    parser.add_argument("--stride", type=float, default=0.5)
    parser.add_argument("--trees", type=int, nargs="+", default=[5, 10, 20, 40, 80, 120, 160, 200])
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--min-samples-leaf", type=int, default=2)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sensor-order", action="append")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sensor_order = args.sensor_order or DEFAULT_SENSOR_ORDER
    x, y, groups, _recordings = load_dataset(Path(args.recordings_dir), sensor_order, args.window, args.stride)
    splitter = GroupShuffleSplit(n_splits=1, test_size=args.test_size, random_state=args.seed)
    train_index, test_index = next(splitter.split(x, y, groups))
    points = []
    for trees in args.trees:
        model = RandomForestClassifier(
            n_estimators=trees,
            max_depth=args.max_depth,
            min_samples_leaf=args.min_samples_leaf,
            class_weight="balanced",
            random_state=args.seed,
        )
        model.fit(x[train_index], y[train_index])
        point = {
            "trees": trees,
            "train": score_model(model, x[train_index], y[train_index]),
            "validation": score_model(model, x[test_index], y[test_index]),
        }
        points.append(point)
        print(
            f"{trees:>4} trees  "
            f"val_accuracy={pct(point['validation']['accuracy'])}  "
            f"val_precision={pct(point['validation']['precision'])}  "
            f"val_recall={pct(point['validation']['recall'])}  "
            f"val_f1={pct(point['validation']['f1'])}"
        )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "training_progress.json"
    html_path = output_dir / "training_progress.html"
    json_path.write_text(json.dumps(points, indent=2) + "\n", encoding="utf-8")
    write_report(points, html_path)
    print(f"Saved training progress report: {html_path}")
    print(f"Saved training progress data: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
