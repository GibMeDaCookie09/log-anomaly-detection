#!/usr/bin/env bash
#
# Roll the service to a new image, verify it, and put the old one back if it
# does not come up.
#
#   deploy.sh <image-ref> [expected-sha]
#
# Lives in the repo rather than baked into the instance's user_data, so changing
# the deploy logic is a commit rather than an instance replacement. The CD
# workflow copies it over on every run.
#
# Exit codes are distinct on purpose - the caller needs to tell "we're fine, the
# change didn't ship" from "nobody is serving traffic":
#   0  new image deployed and healthy
#   1  deploy failed, previous image restored and healthy  (service is up)
#   2  deploy failed AND rollback failed                   (service is DOWN)
#   3  bad usage / environment
set -euo pipefail

IMAGE="${1:-}"
EXPECTED_SHA="${2:-}"

if [ -z "$IMAGE" ]; then
  echo "usage: deploy.sh <image-ref> [expected-sha]" >&2
  exit 3
fi

# Overridable so the rollback logic can be exercised by the test harness
# without a Docker daemon. Defaults are the real on-host paths.
ENV_FILE="${ENV_FILE:-/etc/loganomaly/env}"
# shellcheck source=/dev/null
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

CONTAINER_NAME="${CONTAINER_NAME:-loganomaly}"
API_PORT="${API_PORT:-8000}"
AWS_REGION="${AWS_REGION:-}"
LOG_GROUP="${LOG_GROUP:-}"
S3_BUCKET="${S3_BUCKET:-}"
INSTANCE_ID="${INSTANCE_ID:-unknown}"

# The brief's number. Generous enough for the startup fit on a t3.micro, short
# enough that a broken deploy is not live for minutes.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-30}"
# Rollback gets longer: at this point the priority is restoring service, and
# giving up early to report a faster failure helps nobody.
ROLLBACK_TIMEOUT="${ROLLBACK_TIMEOUT:-90}"

STATE_DIR="${STATE_DIR:-/opt/loganomaly}"
LAST_GOOD_FILE="$STATE_DIR/last_good_image"
HEALTH_URL="http://localhost:${API_PORT}/health"

mkdir -p "$STATE_DIR"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

health_body() { curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null; }

# Waits for /health to return 2xx. /health is a real gate - it 503s until the
# pipeline is fitted - so this is waiting for "can serve traffic", not merely
# "process is alive".
wait_healthy() {
  local budget="$1" deadline
  deadline=$(( $(date +%s) + budget ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if health_body >/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

start_container() {
  local image="$1"
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "${API_PORT}:8000" \
    --log-driver=awslogs \
    --log-opt awslogs-region="$AWS_REGION" \
    --log-opt awslogs-group="$LOG_GROUP" \
    --log-opt "awslogs-stream=app/${INSTANCE_ID}" \
    "$image" >/dev/null
}

# Records the deploy outcome to S3, where Stage 5's cron feeds it back through
# the detector. A failed deploy is exactly the kind of event that should show up
# as an anomaly in the service's own alert stream.
record_event() {
  local outcome="$1" detail="${2:-}"
  [ -n "$S3_BUCKET" ] || return 0
  command -v aws >/dev/null 2>&1 || return 0

  local ts key
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  key="logs/deploy/$(date -u +%Y/%m/%d)/${INSTANCE_ID}-$(date -u +%H%M%S).json"

  printf '{"ts":"%s","event":"deploy","outcome":"%s","image":"%s","previous":"%s","instance":"%s","detail":"%s"}\n' \
    "$ts" "$outcome" "$IMAGE" "${LAST_GOOD:-}" "$INSTANCE_ID" "$detail" \
    | aws s3 cp - "s3://${S3_BUCKET}/${key}" --region "$AWS_REGION" >/dev/null 2>&1 || \
      log "warning: could not write deploy record to S3"
}

dump_failure_context() {
  log "--- container status ---"
  docker ps -a --filter "name=${CONTAINER_NAME}" --format '{{.Names}} {{.Status}} {{.Image}}' || true
  log "--- last 50 log lines ---"
  docker logs --tail 50 "$CONTAINER_NAME" 2>&1 || true
  log "--- health response ---"
  curl -sS --max-time 3 "$HEALTH_URL" || true
  echo
}

# ------------------------------------------------------------------------------

CURRENT_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null || true)"
LAST_GOOD="$(cat "$LAST_GOOD_FILE" 2>/dev/null || true)"
# The file is authoritative once it exists; fall back to whatever is running.
[ -n "$LAST_GOOD" ] || LAST_GOOD="$CURRENT_IMAGE"

log "deploying    $IMAGE"
log "running now  ${CURRENT_IMAGE:-<none>}"
log "rollback to  ${LAST_GOOD:-<none available>}"

# Pull before stopping anything. A registry outage or a bad image reference
# should fail the deploy without ever taking the running service down.
if ! docker pull "$IMAGE"; then
  log "FAILED: could not pull $IMAGE - service left untouched"
  record_event "pull-failed"
  exit 1
fi

start_container "$IMAGE"

if wait_healthy "$HEALTH_TIMEOUT"; then
  # Healthy is necessary but not sufficient: if the container had failed to
  # replace the old one, health would pass against the *previous* version and
  # the deploy would look successful while shipping nothing.
  if [ -n "$EXPECTED_SHA" ]; then
    REPORTED_SHA="$(health_body | python3 -c 'import json,sys; print(json.load(sys.stdin)["build"]["sha"])' 2>/dev/null || echo "")"
    if [ "$REPORTED_SHA" != "$EXPECTED_SHA" ]; then
      log "FAILED: healthy but reports sha '$REPORTED_SHA', expected '$EXPECTED_SHA'"
      dump_failure_context
      HEALTHY=false
    else
      log "verified: serving $EXPECTED_SHA"
      HEALTHY=true
    fi
  else
    HEALTHY=true
  fi
else
  log "FAILED: $IMAGE did not become healthy within ${HEALTH_TIMEOUT}s"
  dump_failure_context
  HEALTHY=false
fi

if [ "$HEALTHY" = true ]; then
  echo "$IMAGE" > "$LAST_GOOD_FILE"
  log "SUCCESS: $IMAGE is live"
  record_event "success"

  # Keep the rollback target and the running image; drop everything older so the
  # 20 GB root volume does not fill up after a few dozen deploys.
  docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
  exit 0
fi

# ---- rollback ----------------------------------------------------------------

if [ -z "$LAST_GOOD" ] || [ "$LAST_GOOD" = "$IMAGE" ]; then
  log "CRITICAL: no distinct previous image to roll back to. Service is down."
  record_event "failed-no-rollback"
  exit 2
fi

log "rolling back to $LAST_GOOD"
if ! docker image inspect "$LAST_GOOD" >/dev/null 2>&1; then
  # Pruned or never present locally. Worth one attempt before giving up.
  docker pull "$LAST_GOOD" || true
fi

start_container "$LAST_GOOD"

if wait_healthy "$ROLLBACK_TIMEOUT"; then
  log "ROLLED BACK: $LAST_GOOD restored and healthy. Deploy of $IMAGE failed."
  record_event "rolled-back"
  exit 1
fi

log "CRITICAL: rollback to $LAST_GOOD also failed. Service is DOWN."
dump_failure_context
record_event "rollback-failed"
exit 2
