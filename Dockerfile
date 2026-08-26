FROM python:3.12-slim

WORKDIR /app

# Dependencies first so this layer caches across code changes. scikit-learn and
# scipy are the expensive installs; they should not be rebuilt because a
# docstring changed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
COPY scripts/ ./scripts/

# Declared after the COPY layers so a new commit SHA invalidates only the cheap
# tail of the build, not the dependency layer.
ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown

ENV BUILD_VERSION=${BUILD_VERSION} \
    BUILD_SHA=${BUILD_SHA} \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="log-anomaly-detection" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.source="https://github.com/GibMeDaCookie09/log-anomaly-detection"

# Least privilege: nothing at runtime needs root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# start-period covers the startup fit (parses 2k lines and trains the detector);
# until that finishes /health is a genuine 503 and the container is not healthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

# --no-access-log: the app emits its own structured JSON access log, and
# uvicorn's unstructured one would double every request in CloudWatch while
# matching none of the metric filters.
CMD ["uvicorn", "loganomaly.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
