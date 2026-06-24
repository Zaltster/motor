#!/usr/bin/env python3
"""Evaluate Random Forest motion models across several window sizes."""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import LeaveOneGroupOut

from train_motion_model import (
    DEFAULT_SENSOR_ORDER,
    FEATURE_VERSION,
    LABELS,
    build_window_features,
    discover_recordings,
    load_recording,
    make_feature_names,
)


def fit_model(args: argparse.Namespace) -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
        class_weight="balanced",
        random_state=args.seed,
    )


def load_dataset(recordings_dir: Path, sensor_order: list[str], window: float, stride: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    features: list[list[float]] = []
    labels: list[str] = []
    groups: list[str] = []
    recordings = discover_recordings(recordings_dir)
    for path in recordings:
        _label, _session_id, events = load_recording(path)
        window_features, window_labels, window_groups = build_window_features(events, sensor_order, window, stride)
        features.extend(window_features)
        labels.extend(window_labels)
        groups.extend(window_groups)
    return np.array(features, dtype=float), np.array(labels), np.array(groups), [str(path) for path in recordings]


def evaluate_window(args: argparse.Namespace, window: float) -> dict[str, Any]:
    sensor_order = args.sensor_order or DEFAULT_SENSOR_ORDER
    x, y, groups, recordings = load_dataset(Path(args.recordings_dir), sensor_order, window, args.stride)
    if len(y) == 0:
        raise RuntimeError(f"no windows generated for {window:g}s")

    logo = LeaveOneGroupOut()
    y_true: list[str] = []
    y_pred: list[str] = []
    confidences: list[float] = []
    correct_confidences: list[float] = []
    wrong_confidences: list[float] = []

    for train_index, test_index in logo.split(x, y, groups):
        model = fit_model(args)
        model.fit(x[train_index], y[train_index])
        predicted = model.predict(x[test_index])
        probabilities = model.predict_proba(x[test_index])
        classes = list(model.classes_)
        for truth, pred, probs in zip(y[test_index], predicted, probabilities, strict=True):
            confidence = float(probs[classes.index(pred)])
            y_true.append(str(truth))
            y_pred.append(str(pred))
            confidences.append(confidence)
            if truth == pred:
                correct_confidences.append(confidence)
            else:
                wrong_confidences.append(confidence)

    report = classification_report(y_true, y_pred, labels=LABELS, output_dict=True, zero_division=0)
    matrix = confusion_matrix(y_true, y_pred, labels=LABELS)
    label_counts = Counter(y.tolist())
    final_model = fit_model(args)
    final_model.fit(x, y)

    output_dir = Path(args.output_dir) / f"window_{window:g}s"
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "motion_random_forest.joblib"
    manifest_path = output_dir / "motion_random_forest.manifest.json"
    joblib.dump(final_model, model_path)
    manifest = {
        "modelType": "RandomForestClassifier",
        "featureVersion": FEATURE_VERSION,
        "labels": LABELS,
        "sensorOrder": sensor_order,
        "featureNames": make_feature_names(sensor_order),
        "windowSeconds": window,
        "strideSeconds": args.stride,
        "recordings": recordings,
        "parameters": {
            "nEstimators": args.n_estimators,
            "maxDepth": args.max_depth,
            "minSamplesLeaf": args.min_samples_leaf,
            "seed": args.seed,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return {
        "windowSeconds": window,
        "strideSeconds": args.stride,
        "recordings": len(recordings),
        "windows": int(len(y)),
        "labelWindows": dict(label_counts),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macroF1": float(report["macro avg"]["f1-score"]),
        "weightedF1": float(report["weighted avg"]["f1-score"]),
        "meanConfidence": float(np.mean(confidences)) if confidences else 0.0,
        "meanCorrectConfidence": float(np.mean(correct_confidences)) if correct_confidences else 0.0,
        "meanWrongConfidence": float(np.mean(wrong_confidences)) if wrong_confidences else 0.0,
        "classificationReport": report,
        "confusionMatrix": matrix.tolist(),
        "labels": LABELS,
        "modelPath": str(model_path),
        "manifestPath": str(manifest_path),
    }


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def bar_svg(results: list[dict[str, Any]], metric: str, title: str, color: str) -> str:
    width = 820
    height = 280
    margin_left = 70
    margin_bottom = 48
    plot_width = width - margin_left - 24
    plot_height = height - 58 - margin_bottom
    bar_gap = 26
    bar_width = (plot_width - bar_gap * (len(results) - 1)) / len(results)
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{margin_left}" y="28" font-size="18" font-weight="700" fill="#101828">{html.escape(title)}</text>',
    ]
    for i in range(6):
        value = i / 5
        y = 48 + plot_height * (1 - value)
        pieces.append(f'<line x1="{margin_left}" x2="{width - 24}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e4e7ec"/>')
        pieces.append(f'<text x="{margin_left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="12" fill="#667085">{pct(value)}</text>')
    for index, result in enumerate(results):
        value = float(result[metric])
        x = margin_left + index * (bar_width + bar_gap)
        y = 48 + plot_height * (1 - value)
        pieces.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{plot_height * value:.1f}" rx="4" fill="{color}"/>')
        pieces.append(f'<text x="{x + bar_width / 2:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="13" font-weight="700" fill="#101828">{pct(value)}</text>')
        pieces.append(f'<text x="{x + bar_width / 2:.1f}" y="{height - 18}" text-anchor="middle" font-size="13" fill="#475467">{result["windowSeconds"]:g}s</text>')
    pieces.append("</svg>")
    return "\n".join(pieces)


def matrix_table(result: dict[str, Any]) -> str:
    rows = []
    labels = result["labels"]
    for label, values in zip(labels, result["confusionMatrix"], strict=True):
        cells = "".join(f"<td>{value}</td>" for value in values)
        rows.append(f"<tr><th>{html.escape(label)}</th>{cells}</tr>")
    headers = "".join(f"<th>{html.escape(label)}</th>" for label in labels)
    return f"""
    <table>
      <caption>{result["windowSeconds"]:g}s confusion matrix, rows true, columns predicted</caption>
      <thead><tr><th></th>{headers}</tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def write_report(results: list[dict[str, Any]], output_path: Path) -> None:
    summary_rows = []
    for result in results:
        summary_rows.append(
            "<tr>"
            f"<td>{result['windowSeconds']:g}s</td>"
            f"<td>{result['windows']}</td>"
            f"<td>{pct(result['accuracy'])}</td>"
            f"<td>{pct(result['macroF1'])}</td>"
            f"<td>{pct(result['meanConfidence'])}</td>"
            f"<td>{pct(result['meanCorrectConfidence'])}</td>"
            f"<td>{pct(result['meanWrongConfidence'])}</td>"
            "</tr>"
        )
    matrices = "\n".join(matrix_table(result) for result in results)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Motion Model Window Evaluation</title>
  <style>
    body {{ margin: 28px; color: #101828; background: #f5f7fb; font-family: Inter, system-ui, sans-serif; }}
    h1, h2 {{ letter-spacing: 0; }}
    section {{ margin: 18px 0; padding: 18px; border: 1px solid #d0d5dd; border-radius: 8px; background: #fff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ padding: 8px 10px; border: 1px solid #eaecf0; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    caption {{ margin-bottom: 8px; color: #475467; font-weight: 700; text-align: left; }}
    svg {{ width: 100%; height: auto; display: block; }}
  </style>
</head>
<body>
  <h1>Motion Model Window Evaluation</h1>
  <section>
    <h2>Summary</h2>
    <table>
      <thead><tr><th>Window</th><th>Windows</th><th>Accuracy</th><th>Macro F1</th><th>Avg Confidence</th><th>Correct Confidence</th><th>Wrong Confidence</th></tr></thead>
      <tbody>{''.join(summary_rows)}</tbody>
    </table>
  </section>
  <section>{bar_svg(results, "accuracy", "Accuracy By Window", "#155eef")}</section>
  <section>{bar_svg(results, "macroF1", "Macro F1 By Window", "#0f766e")}</section>
  <section>{bar_svg(results, "meanCorrectConfidence", "Mean Confidence When Correct", "#7c3aed")}</section>
  <section>{bar_svg(results, "meanWrongConfidence", "Mean Confidence When Wrong", "#b42318")}</section>
  <section>
    <h2>Confusion Matrices</h2>
    {matrices}
  </section>
</body>
</html>
"""
    output_path.write_text(html_text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate motion model windows.")
    parser.add_argument("--recordings-dir", default="data/recordings")
    parser.add_argument("--output-dir", default="models/window_eval")
    parser.add_argument("--report-dir", default="reports/motion_model")
    parser.add_argument("--windows", type=float, nargs="+", default=[1.0, 2.0, 5.0, 10.0])
    parser.add_argument("--stride", type=float, default=0.5)
    parser.add_argument("--n-estimators", type=int, default=150)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--min-samples-leaf", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sensor-order", action="append")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    results = [evaluate_window(args, window) for window in args.windows]
    (report_dir / "window_eval_results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    write_report(results, report_dir / "window_eval_report.html")
    for result in results:
        print(
            f"{result['windowSeconds']:>4g}s  "
            f"accuracy={pct(result['accuracy'])}  "
            f"macro_f1={pct(result['macroF1'])}  "
            f"correct_conf={pct(result['meanCorrectConfidence'])}  "
            f"wrong_conf={pct(result['meanWrongConfidence'])}  "
            f"windows={result['windows']}"
        )
    print(f"Saved report: {report_dir / 'window_eval_report.html'}")
    print(f"Saved results: {report_dir / 'window_eval_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
