"""Canonicalising structured records into detector input.

Drain3 masks any token that varies between otherwise-identical lines. For an
unstructured log that is exactly right - it is how `core.1234 written` and
`core.5678 written` become one event type. For a *structured* JSON access log it
is destructive: the status code varies, so drain3 masks it, and

    {"event":"request","path":"/health","status":200,...}
    {"event":"request","path":"/health","status":503,...}

collapse into a single template. The detector then sees a service where nothing
ever changes, no matter how hard it is failing. Measured on a sample of 960 lines
containing a 40-request burst of 503s, raw JSON yields 6 templates and zero
detections; canonicalised, the burst is flagged.

The fix is to render structured records into an event string whose
discriminative fields are *words*. Words survive masking; numbers do not.
"""

from __future__ import annotations

import json


def status_class(status: int) -> str:
    """Bucket a status code into a word.

    Deliberately coarse. The interesting distinction operationally is "the server
    broke" versus "the caller asked for something silly", and a template per
    distinct code would fragment the feature space for no gain.
    """
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    if status >= 300:
        return "redirect"
    return "ok"


def canonicalise(line: str) -> str:
    """Render one raw log line into detector input.

    Anything that is not one of our own structured records passes through
    untouched - tracebacks and uvicorn lifecycle messages are already the
    unstructured text drain3 is good at, and rewriting them would lose detail.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return stripped

    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if not isinstance(record, dict):
        return stripped

    event = record.get("event")

    if event == "request":
        try:
            klass = status_class(int(record.get("status", 0)))
        except (TypeError, ValueError):
            klass = "unknown"
        method = str(record.get("method", "?"))
        path = str(record.get("path", "?"))
        return f"request {method} {path} {klass}"

    if event == "deploy":
        # Written by deploy/deploy.sh. A rolled-back deploy is precisely the
        # event this whole pipeline exists to surface.
        outcome = str(record.get("outcome", "unknown")).replace("-", "_")
        return f"deploy outcome {outcome}"

    return stripped


def canonicalise_all(lines: list[str]) -> list[str]:
    return [canonicalise(line) for line in lines]
