"""Tests for the self-monitoring loop.

The claim this project rests on is that a bad deploy shows up in the service's
own alert stream. That claim is only worth making if it is tested, so these
build a synthetic log stream - normal traffic followed by the burst a failed
deploy actually produces - and assert the detector finds it.
"""

from __future__ import annotations

import json
import random

import pytest

from loganomaly.ingest import canonicalise, canonicalise_all, status_class
from self_monitor import analyse, split_baseline_recent


def access_line(path: str, status: int, method: str = "GET") -> str:
    return json.dumps(
        {
            "ts": "2026-08-26T12:00:00Z",
            "schema": 1,
            "event": "request",
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": 3.5,
            "sha": "a1b2c3d",
        }
    )


@pytest.fixture
def bad_deploy_stream() -> tuple[list[str], int]:
    """Normal traffic, then a failed deploy. Returns (lines, index_burst_starts)."""
    rng = random.Random(7)
    lines: list[str] = []
    for _ in range(900):
        path, method = rng.choices(
            [("/health", "GET"), ("/score", "POST"), ("/docs", "GET")],
            weights=[80, 15, 5],
        )[0]
        lines.append(access_line(path, 200, method))

    burst_start = len(lines)
    lines += ["INFO:     Shutting down", "INFO:     Waiting for application shutdown."]
    for i in range(40):
        lines.append(access_line("/health", 503))
        if i % 7 == 0:
            lines += [
                "ERROR:    Traceback (most recent call last):",
                "ERROR:      FileNotFoundError: data/BGL_2k.log_structured.csv",
                "ERROR:    pipeline not fitted - is the training data present?",
            ]
    return lines, burst_start


# --- canonicalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [(200, "ok"), (204, "ok"), (301, "redirect"), (404, "client_error"), (503, "server_error")],
)
def test_status_class_buckets(code, expected):
    assert status_class(code) == expected


def test_canonicalise_preserves_the_status_distinction():
    """The whole reason ingest.py exists: drain3 masks a bare status code."""
    ok = canonicalise(access_line("/health", 200))
    bad = canonicalise(access_line("/health", 503))
    assert ok == "request GET /health ok"
    assert bad == "request GET /health server_error"
    assert ok != bad


def test_canonicalise_passes_through_unstructured_lines():
    line = "ERROR:    Traceback (most recent call last):"
    assert canonicalise(line) == line


def test_canonicalise_survives_malformed_json():
    assert canonicalise('{"event": "request"') == '{"event": "request"'
    assert canonicalise("{}") == "{}"


def test_canonicalise_renders_deploy_events():
    line = json.dumps({"event": "deploy", "outcome": "rolled-back", "image": "repo:x"})
    assert canonicalise(line) == "deploy outcome rolled_back"


def test_raw_json_collapses_status_but_canonical_does_not(bad_deploy_stream):
    """Regression guard for the bug that made the loop silently detect nothing.

    Fed raw, drain3 masks the status field and a 503 burst becomes
    indistinguishable from ordinary traffic.
    """
    from loganomaly.parse import LogParser

    lines, _ = bad_deploy_stream

    raw = LogParser(similarity_threshold=0.7)
    raw.parse(lines)
    raw_templates = [t for t in raw.templates().values() if "request" in t or "event" in t]

    canon = LogParser(similarity_threshold=0.7)
    canon.parse(canonicalise_all(lines))
    canon_templates = [t for t in canon.templates().values() if t.startswith("request")]

    assert len(raw_templates) == 1, "raw JSON should collapse to one request template"
    assert any("server_error" in t for t in canon_templates)
    assert not any("server_error" in t for t in raw_templates)


# --- the loop -----------------------------------------------------------------


def test_bad_deploy_burst_is_detected_and_ranked_first(bad_deploy_stream):
    lines, burst_start = bad_deploy_stream
    baseline, recent = split_baseline_recent(lines, recent_fraction=0.25)
    findings, stats = analyse(
        baseline, recent, window_size=20, percentile=95.0, explain=True, similarity=0.7
    )

    assert findings, "the failed deploy produced no findings at all"

    # Which recent line index does the burst begin at?
    burst_offset = burst_start - len(baseline)
    top = findings[0]
    assert max(top.line_ids) >= burst_offset, "top finding is not in the burst region"

    # Rarity is the signal that catches this; see the comment in analyse() for
    # why Isolation Forest alone cannot.
    assert "rarity" in top.signals
    assert top.rarity > top.rarity_threshold * 2

    assert stats["recent_rarity_max"] > stats["rarity_threshold"]


def test_detection_explains_itself(bad_deploy_stream):
    lines, _ = bad_deploy_stream
    baseline, recent = split_baseline_recent(lines, recent_fraction=0.25)
    findings, _ = analyse(
        baseline, recent, window_size=20, percentile=95.0, explain=True, similarity=0.7
    )
    top = findings[0]
    assert top.explanation is not None
    assert top.explanation["backend"] == "stub"
    assert top.explanation["severity"] in {"low", "medium", "high"}


def test_steady_traffic_is_not_flagged_as_a_burst():
    """No deploy, no burst: nothing should look novel."""
    lines = [access_line("/health", 200) for _ in range(600)]
    baseline, recent = split_baseline_recent(lines, recent_fraction=0.25)
    findings, stats = analyse(
        baseline, recent, window_size=20, percentile=95.0, explain=False, similarity=0.7
    )
    assert stats["recent_rarity_max"] <= stats["rarity_threshold"]
    assert not any("rarity" in f.signals for f in findings)


def test_degenerate_baseline_is_reported_not_hidden():
    """A uniform baseline gives Isolation Forest nothing to split on.

    Reporting a confident zero there would be worse than saying so.
    """
    lines = [access_line("/health", 200) for _ in range(400)]
    baseline, recent = split_baseline_recent(lines, recent_fraction=0.25)
    _, stats = analyse(
        baseline, recent, window_size=20, percentile=95.0, explain=False, similarity=0.7
    )
    assert stats["baseline_score_spread"] < 1e-9
    assert "degenerate_baseline" in stats


def test_insufficient_data_does_not_crash():
    findings, stats = analyse(
        ["one line"], ["another"], window_size=20, percentile=95.0, explain=False
    )
    assert findings == []
    assert "skipped" in stats
