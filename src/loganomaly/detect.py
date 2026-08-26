"""Anomaly scoring over template embeddings.

Two unsupervised scorers plus one frequency heuristic. All return a score where
higher means more anomalous, so they are interchangeable in the pipeline.

Unsupervised on purpose: in production you will not have labels. The labels in
the benchmark data are for *evaluation only* - never for fitting. If you fit on
labels you have built a classifier and should say so.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Detector(ABC):
    name: str = "abstract"

    @abstractmethod
    def fit(self, X: np.ndarray) -> Detector: ...

    @abstractmethod
    def score(self, X: np.ndarray) -> np.ndarray:
        """Higher score = more anomalous."""

    def fit_score(self, X: np.ndarray) -> np.ndarray:
        return self.fit(X).score(X)


class KnnDistanceDetector(Detector):
    """Mean distance to k nearest neighbours.

    Intuition: normal events sit in dense regions because they recur. A rare
    event is far from everything. Simple, no training, and a strong baseline
    that neural methods often fail to beat on log data.
    """

    name = "knn-distance"

    def __init__(self, k: int = 5):
        self.k = k
        self._X = None
        self._nn = None

    def fit(self, X: np.ndarray) -> KnnDistanceDetector:
        from sklearn.neighbors import NearestNeighbors

        k = min(self.k + 1, len(X))
        self._nn = NearestNeighbors(n_neighbors=k).fit(X)
        self._X = X
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        if self._nn is None:
            raise RuntimeError("call fit() first")
        dist, _ = self._nn.kneighbors(X)
        # column 0 is the point itself when scoring the training set
        return dist[:, 1:].mean(axis=1) if dist.shape[1] > 1 else dist.mean(axis=1)


class IsolationForestDetector(Detector):
    """Tree-based isolation. Handles higher dimensions better than kNN."""

    name = "isolation-forest"

    def __init__(self, n_estimators: int = 200, contamination: float | str = "auto"):
        self.n_estimators = n_estimators
        self.contamination = contamination
        self._model = None

    def fit(self, X: np.ndarray) -> IsolationForestDetector:
        from sklearn.ensemble import IsolationForest

        self._model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=42,
        ).fit(X)
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("call fit() first")
        # score_samples: lower = more anomalous, so negate
        return -np.asarray(self._model.score_samples(X), dtype=np.float32)


class TemplateRarityDetector(Detector):
    """Pure frequency heuristic - no embeddings at all.

    This is the 'did the fancy pipeline actually help?' control. Score is the
    inverse log-frequency of the template. If this matches your embedding
    pipeline on F1, your embeddings are not earning their complexity, and the
    honest move is to report that.
    """

    name = "template-rarity"

    def __init__(self):
        self._counts: dict[int, int] = {}
        self._total = 0

    def fit_ids(self, cluster_ids: list[int]) -> TemplateRarityDetector:
        from collections import Counter

        self._counts = dict(Counter(cluster_ids))
        self._total = len(cluster_ids)
        return self

    def score_ids(self, cluster_ids: list[int]) -> np.ndarray:
        return np.array(
            [-np.log((self._counts.get(cid, 0) + 1) / (self._total + 1)) for cid in cluster_ids],
            dtype=np.float32,
        )

    def fit(self, X: np.ndarray) -> TemplateRarityDetector:
        raise NotImplementedError("use fit_ids() - this detector works on cluster ids")

    def score(self, X: np.ndarray) -> np.ndarray:
        raise NotImplementedError("use score_ids()")


def get_detector(kind: str = "knn", **kwargs) -> Detector:
    table = {
        "knn": KnnDistanceDetector,
        "iforest": IsolationForestDetector,
        "rarity": TemplateRarityDetector,
    }
    if kind not in table:
        raise ValueError(f"unknown detector {kind!r}; choose from {sorted(table)}")
    return table[kind](**kwargs)
