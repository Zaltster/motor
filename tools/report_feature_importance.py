#!/usr/bin/env python3
"""Generate feature importance reports for a trained motion model."""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.inspection import permutation_importance

from evaluate_motion_windows import load_dataset
from train_motion_model import DEFAULT_SENSOR_ORDER


def feature_group(name: str) -> str:
    base = name.split("_", 1)[1] if name.startswith("s") and "_" in name and name[1:2].isdigit() else name
    if "freq" in base or "band" in base or "spectral" in base:
        return "frequency"
    if "corr" in base or "lag" in base or "coherence" in base:
        return "cross-floor coherence"
    if "trend" in base or "early" in base or "late" in base:
        return "trend"
    if "burst" in base or "peak" in base or "duration" in base or "frac_ge" in base:
        return "bursts/threshold duration"
    if base in {"count", "active_sensors_0_01", "active_sensors_0_05"}:
        return "sample/activity count"
    if "spread" in base or "ratio" in base:
        return "floor balance/spread"
    return "amplitude"


def sensor_label(name: str, sensor_order: list[str]) -> str:
    if name.startswith("s") and "_" in name and name[1:2].isdigit():
        index = int(name[1])
        if index < len(sensor_order):
            return sensor_order[index]
    return "all"


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def bar(value: float, max_value: float, color: str) -> str:
    width = 0.0 if max_value <= 0 else min(100.0, max(0.0, value / max_value * 100.0))
    return f'<div class="bar"><span style="width:{width:.1f}%;background:{color}"></span></div>'


def write_html(rows: list[dict[str, Any]], group_rows: list[dict[str, Any]], output_path: Path, model_path: str) -> None:
    max_importance = max((row["importance"] for row in rows), default=0.0)
    max_permutation = max((max(0.0, row["permutationMean"]) for row in rows), default=0.0)
    row_html = []
    for row in rows:
        row_html.append(
            "<tr>"
            f"<td>{row['rank']}</td>"
            f"<td><code>{html.escape(row['feature'])}</code></td>"
            f"<td>{html.escape(row['sensor'])}</td>"
            f"<td>{html.escape(row['group'])}</td>"
            f"<td>{pct(row['importance'])}{bar(row['importance'], max_importance, '#155eef')}</td>"
            f"<td>{pct(row['permutationMean'])}{bar(max(0.0, row['permutationMean']), max_permutation, '#0f766e')}</td>"
            f"<td>{pct(row['permutationStd'])}</td>"
            "</tr>"
        )
    group_html = []
    for row in group_rows:
        group_html.append(
            "<tr>"
            f"<td>{html.escape(row['group'])}</td>"
            f"<td>{pct(row['importance'])}</td>"
            f"<td>{pct(row['permutationMean'])}</td>"
            f"<td>{row['features']}</td>"
            "</tr>"
        )
    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Motion Model Feature Importance</title>
  <style>
    body {{ margin: 28px; color: #101828; background: #f5f7fb; font-family: Inter, system-ui, sans-serif; }}
    section {{ margin: 18px 0; padding: 18px; border: 1px solid #d0d5dd; border-radius: 8px; background: #fff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
    th, td {{ padding: 8px 10px; border: 1px solid #eaecf0; text-align: right; vertical-align: top; }}
    th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4), th:first-child, td:first-child {{ text-align: left; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .bar {{ margin-top: 4px; height: 6px; background: #eaecf0; border-radius: 999px; overflow: hidden; }}
    .bar span {{ display: block; height: 100%; }}
    p {{ color: #475467; }}
  </style>
</head>
<body>
  <h1>Motion Model Feature Importance</h1>
  <p>Model: <code>{html.escape(model_path)}</code></p>
  <section>
    <h2>Grouped Importance</h2>
    <p>RandomForest importance is the model's internal split importance. Permutation importance is in-sample accuracy drop after shuffling that feature, so it is useful for ranking but not a clean held-out guarantee.</p>
    <table>
      <thead><tr><th>Group</th><th>RF importance</th><th>Permutation accuracy drop</th><th>Feature count</th></tr></thead>
      <tbody>{''.join(group_html)}</tbody>
    </table>
  </section>
  <section>
    <h2>All Features</h2>
    <table>
      <thead><tr><th>Rank</th><th>Feature</th><th>Sensor</th><th>Group</th><th>RF importance</th><th>Permutation accuracy drop</th><th>Permutation std</th></tr></thead>
      <tbody>{''.join(row_html)}</tbody>
    </table>
  </section>
</body>
</html>
""",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report motion model feature importance.")
    parser.add_argument("--model", default="models/rf_rhythm_v2/window_5s/motion_random_forest.joblib")
    parser.add_argument("--manifest", default="models/rf_rhythm_v2/window_5s/motion_random_forest.manifest.json")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--output-dir", default="reports/motion_model_rhythm_v2")
    parser.add_argument("--permutation-repeats", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model = joblib.load(args.model)
    manifest = load_manifest(Path(args.manifest))
    sensor_order = manifest.get("sensorOrder") or DEFAULT_SENSOR_ORDER
    window = float(manifest.get("windowSeconds") or 5.0)
    stride = float(manifest.get("strideSeconds") or 0.5)
    feature_names = list(manifest["featureNames"])
    x, y, _groups, _recordings = load_dataset(Path(args.recordings_dir), sensor_order, window, stride)
    permutation = permutation_importance(
        model,
        x,
        y,
        n_repeats=args.permutation_repeats,
        random_state=args.seed,
        n_jobs=1,
    )
    rows = []
    for feature, importance, perm_mean, perm_std in zip(
        feature_names,
        model.feature_importances_,
        permutation.importances_mean,
        permutation.importances_std,
        strict=True,
    ):
        rows.append(
            {
                "feature": feature,
                "sensor": sensor_label(feature, sensor_order),
                "group": feature_group(feature),
                "importance": float(importance),
                "permutationMean": float(perm_mean),
                "permutationStd": float(perm_std),
            }
        )
    rows.sort(key=lambda row: row["importance"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index

    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"importance": 0.0, "permutationMean": 0.0, "features": 0})
    for row in rows:
        item = grouped[row["group"]]
        item["importance"] += row["importance"]
        item["permutationMean"] += max(0.0, row["permutationMean"])
        item["features"] += 1
    group_rows = [{"group": group, **values} for group, values in grouped.items()]
    group_rows.sort(key=lambda row: row["importance"], reverse=True)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "feature_importance.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=["rank", "feature", "sensor", "group", "importance", "permutationMean", "permutationStd"],
        )
        writer.writeheader()
        writer.writerows(rows)
    group_csv_path = output_dir / "feature_importance_groups.csv"
    with group_csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["group", "importance", "permutationMean", "features"])
        writer.writeheader()
        writer.writerows(group_rows)
    html_path = output_dir / "feature_importance.html"
    write_html(rows, group_rows, html_path, args.model)
    print(f"Saved feature HTML: {html_path}")
    print(f"Saved feature CSV: {csv_path}")
    print(f"Saved group CSV: {group_csv_path}")
    print("Top features:")
    for row in rows[:20]:
        print(f"{row['rank']:>2}. {row['feature']:<28} rf={pct(row['importance'])} perm_drop={pct(row['permutationMean'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
