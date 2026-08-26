"""Window aggregation.

Line-level detection underperforms on log data and it is worth understanding
why: a single line like "kernel: page allocation failure" is *ordinary* in
isolation. What makes it anomalous is the company it keeps - what preceded it
and how often it fired in a short span.

So aggregate lines into windows and detect on the window. The standard feature
is a template count vector: one dimension per template, the value being how
often that template appeared in the window. This is what DeepLog and LogAnomaly
operate on, and it is a large improvement over per-line scoring.

A window is labelled anomalous if ANY line in it is anomalous - the operational
convention, since an analyst investigating a window will find the fault.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .parse import ParsedLine


@dataclass
class Window:
    index: int
    line_ids: list[int]
    cluster_ids: list[int]
    templates: list[str]
    is_anomaly: bool
    labels: set[str]

    @property
    def size(self) -> int:
        return len(self.line_ids)

    def preview(self, n: int = 5) -> list[str]:
        return self.templates[:n]


def make_windows(
    parsed: list[ParsedLine],
    size: int = 20,
    stride: int | None = None,
) -> list[Window]:
    """Sliding windows over the line sequence.

    stride defaults to size (non-overlapping). Use stride < size for overlap,
    which increases recall at the cost of duplicate alerts.
    """
    stride = stride or size
    windows: list[Window] = []
    for w_idx, start in enumerate(range(0, max(1, len(parsed) - size + 1), stride)):
        chunk = parsed[start : start + size]
        if not chunk:
            continue
        labels = {p.label for p in chunk if p.label and p.label != "-"}
        windows.append(
            Window(
                index=w_idx,
                line_ids=[p.line_id for p in chunk],
                cluster_ids=[p.cluster_id for p in chunk],
                templates=[p.template for p in chunk],
                is_anomaly=any(p.is_anomaly for p in chunk),
                labels=labels,
            )
        )
    return windows


def count_vectors(windows: list[Window], n_templates: int | None = None) -> np.ndarray:
    """Template count matrix: rows are windows, columns are template ids.

    Rows are L2-normalised so that window length does not dominate the
    distance metric - otherwise a long window looks anomalous purely for
    being long.
    """
    all_ids = [cid for w in windows for cid in w.cluster_ids]
    max_id = n_templates or (max(all_ids) + 1 if all_ids else 1)

    X = np.zeros((len(windows), max_id), dtype=np.float32)
    for i, w in enumerate(windows):
        for cid in w.cluster_ids:
            if 0 <= cid < max_id:
                X[i, cid] += 1.0

    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return X / norms
