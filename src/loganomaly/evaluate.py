"""Evaluation against benchmark labels.

Anomaly detection is a ranking problem, not a classification problem: the
scorer produces an ordering and you choose an operating point. So report
average precision and ROC AUC (threshold-free) alongside precision/recall at
a specific budget.

precision_at_k is the metric that matters operationally: if an analyst reviews
the top 100 alerts a day, precision@100 is what they experience.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Metrics:
    detector: str
    embedder: str
    n_samples: int
    n_anomalies: int
    roc_auc: float
    average_precision: float
    precision_at_k: float
    recall_at_k: float
    f1_at_k: float
    k: int

    def as_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return (
            f"{self.detector:<18} {self.embedder:<22} "
            f"AUC={self.roc_auc:.3f}  AP={self.average_precision:.3f}  "
            f"P@{self.k}={self.precision_at_k:.3f}  R@{self.k}={self.recall_at_k:.3f}  "
            f"F1={self.f1_at_k:.3f}"
        )


def evaluate(
    scores: np.ndarray,
    is_anomaly: np.ndarray,
    detector: str = "?",
    embedder: str = "?",
    k: int | None = None,
) -> Metrics:
    """k defaults to the true anomaly count - the standard convention, and the
    fairest comparison between detectors since all get the same alert budget."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    y = np.asarray(is_anomaly).astype(int)
    n_anom = int(y.sum())
    k = k or max(1, n_anom)

    order = np.argsort(-scores)
    top_k = order[:k]
    tp = int(y[top_k].sum())

    precision = tp / k
    recall = tp / n_anom if n_anom else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return Metrics(
        detector=detector,
        embedder=embedder,
        n_samples=len(y),
        n_anomalies=n_anom,
        roc_auc=float(roc_auc_score(y, scores)) if 0 < n_anom < len(y) else float("nan"),
        average_precision=float(average_precision_score(y, scores)) if n_anom else float("nan"),
        precision_at_k=precision,
        recall_at_k=recall,
        f1_at_k=f1,
        k=k,
    )
