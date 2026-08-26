"""Structured access logging.

The log line format here is a contract, not a convenience. CloudWatch metric
filters in infra/cloudwatch.tf parse these fields by name to produce the request
rate, error rate and latency metrics that the alarms and the Grafana dashboard
read. Rename a field and the filters stop matching - silently, because a filter
that matches nothing is indistinguishable from a service receiving no traffic.

One line per request, JSON, on stdout. Docker's awslogs driver forwards stdout
to CloudWatch, so there is no agent and no log file to rotate.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

# Bumped when the field names change, so a metric filter written against an old
# shape can be spotted rather than guessed at.
ACCESS_LOG_SCHEMA = 1


def emit(record: dict) -> None:
    """One JSON object per line, flushed immediately.

    Unflushed stdout is the classic way to lose exactly the logs you need: the
    container dies, the buffer goes with it, and the last few seconds before a
    crash - the interesting part - never reach CloudWatch.
    """
    json.dump(record, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def make_access_log_middleware(
    build_sha: str,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        status = 500  # if call_next raises, the client saw a 500; log it as one
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            emit(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "schema": ACCESS_LOG_SCHEMA,
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "sha": build_sha,
                }
            )

    return access_log
