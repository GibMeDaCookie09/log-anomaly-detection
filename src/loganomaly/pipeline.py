"""End-to-end pipeline: raw lines -> parsed -> windowed -> scored -> explained.

One object that holds fitted state so the API can score new batches without
re-fitting. Fit once at startup, score per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .detect import Detector, IsolationForestDetector
from .explain import Explainer, Explanation
from .parse import LogParser
from .window import Window, count_vectors, make_windows


@dataclass
class Alert:
    window_index: int
    score: float
    line_ids: list[int]
    templates: list[str]
    explanation: Explanation | None = None
    ground_truth_anomaly: bool | None = None

    def as_dict(self) -> dict:
        return {
            "window_index": self.window_index,
            "score": round(self.score, 4),
            "line_ids": self.line_ids,
            "templates": self.templates,
            "explanation": self.explanation.as_dict() if self.explanation else None,
            "ground_truth_anomaly": self.ground_truth_anomaly,
        }


@dataclass
class Pipeline:
    window_size: int = 50
    detector: Detector = field(default_factory=IsolationForestDetector)
    explainer: Explainer = field(default_factory=Explainer)

    parser: LogParser = field(default_factory=LogParser, init=False)
    _n_templates: int = field(default=0, init=False)
    _normal_templates: list[str] = field(default_factory=list, init=False)
    _fitted: bool = field(default=False, init=False)

    def fit(self, lines: list[str], labels: list[str] | None = None) -> Pipeline:
        parsed = self.parser.parse(lines, labels)
        self._n_templates = self.parser.n_templates + 1
        windows = make_windows(parsed, size=self.window_size)
        X = count_vectors(windows, n_templates=self._n_templates)
        self.detector.fit(X)

        # Baseline templates for prompt contrast. Prefer known-normal windows;
        # fall back to the lowest-scoring windows when unlabelled.
        normal = [t for w in windows if not w.is_anomaly for t in w.templates]
        if not normal:
            scores = self.detector.score(X)
            quietest = windows[int(np.argmin(scores))]
            normal = quietest.templates
        self._normal_templates = list(dict.fromkeys(normal))
        self._fitted = True
        return self

    def score(
        self,
        lines: list[str],
        labels: list[str] | None = None,
        top_k: int = 5,
        explain: bool = True,
    ) -> list[Alert]:
        if not self._fitted:
            raise RuntimeError("call fit() before score()")

        parsed = self.parser.parse(lines, labels)
        windows = make_windows(parsed, size=self.window_size)
        if not windows:
            return []

        X = count_vectors(windows, n_templates=self._n_templates)
        scores = self.detector.score(X)
        order = np.argsort(-scores)[:top_k]

        alerts: list[Alert] = []
        for i in order:
            w: Window = windows[int(i)]
            alerts.append(
                Alert(
                    window_index=w.index,
                    score=float(scores[int(i)]),
                    line_ids=w.line_ids,
                    templates=list(dict.fromkeys(w.templates))[:10],
                    explanation=(
                        self.explainer.explain(w, self._normal_templates, float(scores[int(i)]))
                        if explain
                        else None
                    ),
                    ground_truth_anomaly=w.is_anomaly if labels else None,
                )
            )
        return alerts

    @property
    def stats(self) -> dict:
        return {
            "fitted": self._fitted,
            "n_templates": self._n_templates,
            "window_size": self.window_size,
            "detector": self.detector.name,
            "explainer_backend": self.explainer.client.name,
            "explanation_cache": self.explainer.cache_size,
        }
