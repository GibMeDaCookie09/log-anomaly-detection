"""FastAPI service.

Run:  uvicorn loganomaly.api:app --reload
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .observability import make_access_log_middleware
from .pipeline import Pipeline

# Resolved against the package location, not the caller's working directory.
# A relative path here silently yields an unfitted pipeline whenever the service
# is started from anywhere other than the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
TRAIN_DATA = Path(
    os.environ.get("LOGANOMALY_TRAIN_DATA", _REPO_ROOT / "data" / "BGL_2k.log_structured.csv")
)

# Injected at build time by the release workflow. Reported by /health so a running
# container can be traced back to the exact commit that produced its image, and so
# a rollback can be confirmed as having actually taken effect.
BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")
BUILD_SHA = os.environ.get("BUILD_SHA", "unknown")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Fit once at startup so requests never pay the fitting cost.

    State lives on app.state rather than a module global: a global is shared by
    every app instance in the process, so one instance shutting down tears down
    another instance's pipeline.
    """
    pipe = Pipeline(window_size=50)
    if TRAIN_DATA.exists():
        df = pd.read_csv(TRAIN_DATA)
        pipe.fit(df["Content"].astype(str).tolist(), df["Label"].astype(str).tolist())
    app.state.pipeline = pipe
    yield
    app.state.pipeline = None


router = APIRouter()


class ScoreRequest(BaseModel):
    lines: list[str] = Field(..., min_length=1, description="Raw log lines")
    top_k: int = Field(5, ge=1, le=50)
    explain: bool = True


def _fitted_pipeline(request: Request) -> Pipeline | None:
    pipe: Pipeline | None = getattr(request.app.state, "pipeline", None)
    return pipe if pipe is not None and pipe.stats["fitted"] else None


@router.get("/health")
def health(request: Request) -> dict:
    """Deploy gate: 503 until the service can actually serve /score.

    Reporting 200 for an unfitted pipeline would let a broken deploy sail past the
    post-deploy health check and stay live while every /score call 503s. A check
    that cannot fail is not a check.
    """
    build = {"version": BUILD_VERSION, "sha": BUILD_SHA}
    pipe = _fitted_pipeline(request)
    if pipe is None:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "unavailable",
                "reason": "pipeline not fitted",
                "train_data": str(TRAIN_DATA),
                "train_data_present": TRAIN_DATA.exists(),
                "build": build,
            },
        )
    return {"status": "ok", "build": build, "pipeline": pipe.stats}


@router.post("/score")
def score(req: ScoreRequest, request: Request) -> dict:
    pipe = _fitted_pipeline(request)
    if pipe is None:
        raise HTTPException(503, "pipeline not fitted - is the training data present?")
    alerts = pipe.score(req.lines, top_k=req.top_k, explain=req.explain)
    return {
        "n_lines": len(req.lines),
        "n_alerts": len(alerts),
        "alerts": [a.as_dict() for a in alerts],
    }


def create_app() -> FastAPI:
    """Build an independent app instance.

    A factory rather than a single module-level app so tests can stand up an
    isolated instance without one instance's shutdown tearing down another's
    fitted pipeline.
    """
    application = FastAPI(
        title="Log Anomaly Detection",
        description="Template parsing -> window aggregation -> anomaly scoring -> LLM explanation",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Registered before the router so it wraps every request, including the
    # 503s from /health - which are the ones worth counting.
    application.middleware("http")(make_access_log_middleware(BUILD_SHA))
    application.include_router(router)
    return application


# The ASGI entrypoint uvicorn imports: `uvicorn loganomaly.api:app`.
app = create_app()
