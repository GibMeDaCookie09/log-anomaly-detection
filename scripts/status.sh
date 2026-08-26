#!/usr/bin/env bash
#
# One view of the whole system: infrastructure, the running service, CI/CD, and
# what the detector currently thinks of its own logs.
#
#   bash scripts/status.sh
#
# Everything here is read-only. Sections degrade individually - if the AWS CLI
# or gh is missing, or the instance is destroyed, the rest still prints. A
# status tool that dies because one thing is down is useless exactly when you
# need it.
set -uo pipefail

REPO="${REPO:-GibMeDaCookie09/log-anomaly-detection}"
INFRA_DIR="${INFRA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra}"

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m%-12s\033[0m %s\n' "$1" "$2"; }
bad()  { printf '  \033[31m%-12s\033[0m %s\n' "$1" "$2"; }
info() { printf '  %-12s %s\n' "$1" "$2"; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---- infrastructure ----------------------------------------------------------

bold "INFRASTRUCTURE"
if ! have terraform; then
  info "terraform" "not on PATH"
elif [ ! -f "$INFRA_DIR/terraform.tfstate" ]; then
  info "state" "no local state - nothing provisioned"
else
  HOST=$(terraform -chdir="$INFRA_DIR" output -raw public_ip 2>/dev/null)
  BUCKET=$(terraform -chdir="$INFRA_DIR" output -raw s3_bucket 2>/dev/null)
  LOG_GROUP=$(terraform -chdir="$INFRA_DIR" output -raw log_group 2>/dev/null)
  REGION=$(terraform -chdir="$INFRA_DIR" output -raw aws_region 2>/dev/null)
  INSTANCE=$(terraform -chdir="$INFRA_DIR" output -raw instance_id 2>/dev/null)

  if [ -z "${HOST:-}" ]; then
    bad "state" "present but no outputs - destroyed?"
  else
    info "instance" "${INSTANCE}  (${REGION})"
    info "address" "$HOST"
    info "bucket" "$BUCKET"
    info "log group" "$LOG_GROUP"

    if have aws; then
      STATE=$(aws ec2 describe-instances --instance-ids "$INSTANCE" --region "$REGION" \
              --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)
      if [ "$STATE" = "running" ]; then
        ok "ec2 state" "$STATE"
      else
        bad "ec2 state" "${STATE:-unknown}"
      fi
    fi
  fi
fi

# ---- the service -------------------------------------------------------------

bold "SERVICE"
if [ -z "${HOST:-}" ]; then
  info "health" "no host"
else
  BODY=$(curl -fsS --max-time 8 "http://${HOST}:8000/health" 2>/dev/null)
  if [ -n "$BODY" ]; then
    ok "health" "200 OK"
    if have python3 || have python; then
      PY=$(command -v python3 || command -v python)
      echo "$BODY" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
b=d.get("build",{}); p=d.get("pipeline",{})
print(f"  {\"build\":<12} {b.get(\"version\",\"?\")} / {str(b.get(\"sha\",\"?\"))[:7]}")
print(f"  {\"pipeline\":<12} fitted={p.get(\"fitted\")}  templates={p.get(\"n_templates\")}  detector={p.get(\"detector\")}")
' 2>/dev/null
    fi
  else
    bad "health" "unreachable or 503 (not fitted / not deployed / SG blocks you)"
  fi
fi

# ---- alarms ------------------------------------------------------------------

bold "ALARMS"
if ! have aws || [ -z "${REGION:-}" ]; then
  info "aws" "unavailable"
else
  OUT=$(aws cloudwatch describe-alarms --region "$REGION" --alarm-name-prefix loganomaly \
        --query 'MetricAlarms[].[AlarmName,StateValue]' --output text 2>/dev/null)
  if [ -z "$OUT" ]; then
    info "alarms" "none found"
  else
    while IFS=$'\t' read -r name state; do
      case "$state" in
        OK)                ok  "$state" "$name" ;;
        ALARM)             bad "$state" "$name" ;;
        *)                 info "$state" "$name" ;;
      esac
    done <<< "$OUT"
  fi

  PENDING=$(aws sns list-subscriptions --region "$REGION" \
            --query "Subscriptions[?contains(TopicArn,'loganomaly')].SubscriptionArn" --output text 2>/dev/null)
  case "$PENDING" in
    *PendingConfirmation*) bad "sns" "subscription NOT confirmed - alarms reach nobody" ;;
    "")                    info "sns" "no subscription" ;;
    *)                     ok "sns" "subscription confirmed" ;;
  esac
fi

# ---- detector ----------------------------------------------------------------

bold "SELF-MONITORING"
if ! have aws || [ -z "${BUCKET:-}" ]; then
  info "aws" "unavailable"
else
  LAST=$(aws s3 ls "s3://${BUCKET}/results/" --recursive --region "$REGION" 2>/dev/null | sort | tail -1)
  if [ -z "$LAST" ]; then
    info "results" "none yet - timer runs every 15 min after first deploy"
  else
    info "last run" "$(echo "$LAST" | awk '{print $1, $2}')"
    KEY=$(echo "$LAST" | awk '{print $4}')
    aws s3 cp "s3://${BUCKET}/${KEY}" - --region "$REGION" 2>/dev/null | \
      grep -o '"anomalies_detected": *[0-9]*' | head -1 | \
      awk -F: '{gsub(/ /,"",$2); if ($2+0 > 0) printf "  \033[31m%-12s\033[0m %s flagged windows\n","anomalies",$2; else printf "  \033[32m%-12s\033[0m none\n","anomalies"}'
  fi

  DEPLOYS=$(aws s3 ls "s3://${BUCKET}/logs/deploy/" --recursive --region "$REGION" 2>/dev/null | wc -l)
  info "deploy log" "${DEPLOYS// /} records in s3://${BUCKET}/logs/deploy/"
fi

# ---- pipeline ----------------------------------------------------------------

bold "CI / CD"
if ! have gh; then
  info "gh" "not on PATH"
else
  gh run list --repo "$REPO" --limit 5 \
     --json workflowName,status,conclusion,headBranch,createdAt \
     --template '{{range .}}  {{printf "%-10s" .conclusion}} {{printf "%-9s" .workflowName}} {{.headBranch}}  {{timeago .createdAt}}
{{end}}' 2>/dev/null || info "runs" "could not fetch"
fi

bold "LINKS"
info "actions" "https://github.com/${REPO}/actions"
[ -n "${HOST:-}" ] && info "api docs" "http://${HOST}:8000/docs"
[ -n "${REGION:-}" ] && info "cloudwatch" "https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#alarmsV2:"
info "grafana" "ssh -N -L 3000:localhost:3000 ec2-user@${HOST:-<host>}  then http://localhost:3000"
echo
