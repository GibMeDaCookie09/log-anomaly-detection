"""API contract tests.

/health is the gate the deploy pipeline rolls back on, so its failure mode is
worth testing explicitly: it must go red when the service cannot serve traffic,
not merely when the process is dead.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from loganomaly import api


@pytest.fixture(scope="module")
def client():
    # TestClient's context manager is what runs the lifespan handler; without it
    # the pipeline is never fitted.
    with TestClient(api.create_app()) as c:
        yield c


def test_health_ok_when_fitted(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["pipeline"]["fitted"] is True
    assert set(body["build"]) == {"version", "sha"}


def test_health_503_when_training_data_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "TRAIN_DATA", tmp_path / "absent.csv")
    with TestClient(api.create_app()) as c:
        r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "pipeline not fitted"


def test_score_returns_ranked_alerts(client):
    lines = ["kernel: page allocation failure order:2", "user root logged in from 10.0.0.4"] * 40
    r = client.post("/score", json={"lines": lines, "top_k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["n_lines"] == len(lines)
    assert body["n_alerts"] <= 3
    scores = [a["score"] for a in body["alerts"]]
    assert scores == sorted(scores, reverse=True)


def test_score_rejects_empty_input(client):
    assert client.post("/score", json={"lines": []}).status_code == 422


def test_access_log_shape_is_the_metric_filter_contract(client, capsys):
    """infra/cloudwatch.tf parses these field names to build metrics.

    A filter that stops matching produces no metric, which looks exactly like a
    service receiving no traffic - so the shape is worth asserting here rather
    than discovering on a flat dashboard.
    """
    client.get("/health")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert lines, "no access log line was emitted"

    rec = json.loads(lines[-1])
    assert rec["event"] == "request"
    assert rec["method"] == "GET"
    assert rec["path"] == "/health"
    assert rec["status"] == 200
    assert rec["schema"] == 1
    assert isinstance(rec["duration_ms"], (int, float))
    assert set(rec) == {"ts", "schema", "event", "method", "path", "status", "duration_ms", "sha"}


def test_access_log_records_error_status(monkeypatch, tmp_path, capsys):
    """A 503 must be logged as a 503 - it is what the error-rate alarm counts."""
    monkeypatch.setattr(api, "TRAIN_DATA", tmp_path / "absent.csv")
    with TestClient(api.create_app()) as c:
        c.get("/health")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    statuses = [json.loads(ln)["status"] for ln in lines]
    assert 503 in statuses
