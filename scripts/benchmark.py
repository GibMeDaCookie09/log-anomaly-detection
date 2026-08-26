#!/usr/bin/env python3
"""Run every embedder x detector combination and print a metrics table.

This is the script that produces the numbers for your README. Run it, paste the
table, and you have evidence instead of claims.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loganomaly.detect import (  # noqa: E402
    IsolationForestDetector,
    KnnDistanceDetector,
    TemplateRarityDetector,
)
from loganomaly.embed import get_embedder  # noqa: E402
from loganomaly.evaluate import evaluate  # noqa: E402
from loganomaly.parse import LogParser  # noqa: E402


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "Content" not in df.columns:
        raise SystemExit(f"{path} has no Content column; got {list(df.columns)}")
    if "Label" not in df.columns:
        raise SystemExit(f"{path} has no Label column - cannot evaluate without labels")
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/BGL_2k.log_structured.csv")
    ap.add_argument(
        "--embedders", nargs="+", default=["tfidf"], help="tfidf, sentence-transformers"
    )
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    df = load(Path(args.data))
    parser = LogParser()
    parsed = parser.parse(df["Content"].astype(str), df["Label"].astype(str))

    templates = [p.template for p in parsed]
    cluster_ids = [p.cluster_id for p in parsed]
    y = np.array([p.is_anomaly for p in parsed])

    print(f"\ndataset      : {args.data}")
    print(f"lines        : {len(parsed)}")
    print(f"templates    : {parser.n_templates}")
    print(f"anomalies    : {int(y.sum())} ({y.mean():.1%})")
    print("\n" + "-" * 96)

    results = []

    # Control: no embeddings at all. If this wins, report that it wins.
    rarity = TemplateRarityDetector().fit_ids(cluster_ids)
    m = evaluate(rarity.score_ids(cluster_ids), y, "template-rarity", "none (control)")
    print(m)
    results.append(m.as_dict())

    for kind in args.embedders:
        try:
            emb = get_embedder(kind)
            V = emb.fit_transform(templates)
        except Exception as exc:  # backend unavailable - keep going
            print(f"{kind:<18} SKIPPED ({type(exc).__name__}: {exc})")
            continue

        for det in (KnnDistanceDetector(k=5), IsolationForestDetector()):
            m = evaluate(det.fit_score(V), y, det.name, emb.name)
            print(m)
            results.append(m.as_dict())

    print("-" * 96)
    print("\nk = true anomaly count (equal alert budget for every detector).")
    print("If the control row matches the embedding rows, your embeddings are not")
    print("earning their complexity on this dataset. Report that honestly.\n")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
