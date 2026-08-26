"""Tests run with no network and no API key - that is deliberate."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loganomaly.detect import IsolationForestDetector, KnnDistanceDetector
from loganomaly.embed import get_embedder
from loganomaly.evaluate import evaluate
from loganomaly.parse import LogParser
from loganomaly.pipeline import Pipeline
from loganomaly.window import count_vectors, make_windows

SAMPLE = [
    "instruction cache parity error corrected",
    "generating core.1234",
    "CE sym 5, at 0x1fbb7bc0, mask 0x04",
    "ciod: failed to read message prefix on control stream",
    "kernel: page allocation failure order:2",
    "user root logged in from 10.0.0.4",
] * 20


def test_parser_collapses_variables():
    p = LogParser()
    parsed = p.parse(["core.1234 written", "core.5678 written"])
    assert parsed[0].cluster_id == parsed[1].cluster_id
    assert p.n_templates == 1


def test_parser_label_convention():
    p = LogParser()
    parsed = p.parse(["a failure", "b ok"], ["KERNSTOR", "-"])
    assert parsed[0].is_anomaly
    assert not parsed[1].is_anomaly


def test_embedder_shape_and_determinism():
    e = get_embedder("tfidf", n_components=8)
    a = e.fit_transform(SAMPLE[:6])
    b = e.transform(SAMPLE[:6])
    assert a.shape[0] == 6
    np.testing.assert_allclose(a, b, rtol=1e-5)


def test_unknown_backends_raise():
    with pytest.raises(ValueError):
        get_embedder("nope")


def test_windows_partition_lines():
    p = LogParser()
    parsed = p.parse(SAMPLE)
    w = make_windows(parsed, size=10)
    assert all(x.size == 10 for x in w)
    assert len({i for x in w for i in x.line_ids}) == len(w) * 10


def test_count_vectors_are_normalised():
    p = LogParser()
    parsed = p.parse(SAMPLE)
    X = count_vectors(make_windows(parsed, size=10), n_templates=p.n_templates + 1)
    np.testing.assert_allclose(np.linalg.norm(X, axis=1), 1.0, rtol=1e-5)


@pytest.mark.parametrize("det", [KnnDistanceDetector(k=3), IsolationForestDetector()])
def test_detectors_score_every_row(det):
    X = np.random.RandomState(0).rand(30, 6).astype(np.float32)
    s = det.fit_score(X)
    assert s.shape == (30,) and np.isfinite(s).all()


def test_detector_flags_planted_outlier():
    rs = np.random.RandomState(1)
    X = np.vstack([rs.normal(0, 0.1, (40, 4)), np.array([[9.0, 9.0, 9.0, 9.0]])]).astype(np.float32)
    s = KnnDistanceDetector(k=3).fit_score(X)
    assert int(np.argmax(s)) == 40


def test_metrics_bounds():
    y = np.array([0, 0, 1, 1])
    m = evaluate(np.array([0.1, 0.2, 0.9, 0.8]), y, "t", "t")
    assert m.roc_auc == 1.0
    assert 0.0 <= m.precision_at_k <= 1.0


def test_pipeline_end_to_end_offline():
    pipe = Pipeline(window_size=10)
    pipe.fit(SAMPLE, ["-"] * len(SAMPLE))
    alerts = pipe.score(SAMPLE, top_k=3)
    assert len(alerts) == 3
    assert alerts[0].score >= alerts[-1].score
    assert alerts[0].explanation is not None
    assert alerts[0].explanation.backend == "stub"


def test_explanation_cache_hits():
    pipe = Pipeline(window_size=10)
    pipe.fit(SAMPLE)
    pipe.score(SAMPLE, top_k=2)
    before = pipe.stats["explanation_cache"]
    pipe.score(SAMPLE, top_k=2)
    assert pipe.stats["explanation_cache"] == before


def test_score_before_fit_raises():
    with pytest.raises(RuntimeError):
        Pipeline().score(SAMPLE)
