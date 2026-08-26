#!/usr/bin/env bash
#
# Runs the detector over the service's own logs. Invoked by self-monitor.timer.
#
# The container is started from the same image that is currently serving traffic,
# so the detector doing the monitoring is literally the deployed build - not a
# separate copy that could drift from it.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/loganomaly/env}"
# shellcheck source=/dev/null
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

STATE_DIR="${STATE_DIR:-/opt/loganomaly}"
AWS_REGION="${AWS_REGION:-}"
LOG_GROUP="${LOG_GROUP:-}"
S3_BUCKET="${S3_BUCKET:-}"
# Must match the namespace the IAM policy conditions PutMetricData on, or the
# publish is denied - and only on the first timer run, long after deploy.
METRIC_NAMESPACE="${METRIC_NAMESPACE:-loganomaly/api}"

LOOKBACK_MINUTES="${LOOKBACK_MINUTES:-120}"
WINDOW_SIZE="${WINDOW_SIZE:-20}"

IMAGE="$(cat "$STATE_DIR/last_good_image" 2>/dev/null || true)"
if [ -z "$IMAGE" ]; then
  echo "no deployed image recorded yet - nothing to monitor"
  exit 0
fi

# The instance enforces an IMDS hop limit of 1, so a container cannot reach the
# metadata endpoint and assume the instance role itself. That is deliberate: it
# means an SSRF in the API cannot steal credentials. The host resolves them and
# passes them in as short-lived environment variables instead.
# export-credentials landed in AWS CLI 2.9. Fail with the reason rather than
# an opaque "unknown command" from inside a subshell.
if ! aws configure export-credentials --format env > /tmp/loganomaly-creds 2>/dev/null; then
  echo "aws configure export-credentials failed - AWS CLI v2.9+ is required" >&2
  aws --version >&2 || true
  exit 1
fi
# shellcheck source=/dev/null
. /tmp/loganomaly-creds
rm -f /tmp/loganomaly-creds

docker run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION="$AWS_REGION" \
  -e AWS_DEFAULT_REGION="$AWS_REGION" \
  "$IMAGE" \
  python /app/scripts/self_monitor.py \
  --log-group "$LOG_GROUP" \
  --bucket "$S3_BUCKET" \
  --region "$AWS_REGION" \
  --namespace "$METRIC_NAMESPACE" \
  --minutes "$LOOKBACK_MINUTES" \
  --window-size "$WINDOW_SIZE"
