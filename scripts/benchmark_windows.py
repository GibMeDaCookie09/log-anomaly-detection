#!/usr/bin/env python3
"""Window-level benchmark - compare against the line-level numbers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loganomaly.detect import IsolationForestDetector, KnnDistanceDetector  # noqa: E402
from loganomaly.evaluate import evaluate  # noqa: E402
from loganomaly.parse import LogParser  # noqa: E402
from loganomaly.window import count_vectors, make_windows  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/BGL_2k.log_structured.csv")
    ap.add_argument("--sizes", nargs="+", type=int, default=[10, 20, 50])
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    df = pd.read_csv(args.data)
    parser = LogParser()
    parsed = parser.parse(df["Content"].astype(str), df["Label"].astype(str))
    n_templates = parser.n_templates + 1

    print(f"\ndataset   : {args.data}")
    print(f"lines     : {len(parsed)}   templates: {parser.n_templates}")
    print("\n" + "-" * 100)

    results = []
    for size in args.sizes:
        windows = make_windows(parsed, size=size)
        X = count_vectors(windows, n_templates=n_templates)
        y = np.array([w.is_anomaly for w in windows])
        if y.sum() == 0 or y.sum() == len(y):
            print(f"window={size:<4} skipped (degenerate labels)")
            continue

        print(
            f"window={size:<4} windows={len(windows):<5} anomalous={int(y.sum())} ({y.mean():.1%})"
        )
        for det in (KnnDistanceDetector(k=5), IsolationForestDetector()):
            m = evaluate(det.fit_score(X), y, det.name, f"count-vec w={size}")
            print(f"          {m}")
            results.append(m.as_dict())
    print("-" * 100 + "\n")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
