#!/usr/bin/env bash
#
# Exercises deploy.sh's failure paths without a Docker daemon.
#
# The rollback branch is the part of this project that most needs testing and is
# hardest to test: you would have to deliberately break production to see it run.
# So docker, curl, aws and python3 are replaced with stubs on PATH, and each
# scenario asserts the exit code the caller depends on.
#
#   bash deploy/test_deploy.sh
#
# The setup_* functions are invoked indirectly, by name, through run_case.
# shellcheck disable=SC2329
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY="$HERE/deploy.sh"
PASS=0
FAIL=0

# ---- stub harness ------------------------------------------------------------

make_stubs() {
  local dir="$1"
  mkdir -p "$dir"

  # PULL_FAILS, HEALTHY_IMAGES (space separated), RUNNING file
  cat > "$dir/docker" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  pull)
    [ "${PULL_FAILS:-0}" = "1" ] && exit 1
    echo "pulled $2"; exit 0 ;;
  inspect)
    if [ "$2" = "--format" ]; then
      # inspect the running container's image
      [ -s "$RUNNING_FILE" ] || exit 1
      cat "$RUNNING_FILE"; exit 0
    fi
    exit 0 ;;
  image)
    case "$2" in
      inspect) exit 0 ;;
      prune)   exit 0 ;;
    esac
    exit 0 ;;
  rm) : > "$RUNNING_FILE"; exit 0 ;;
  run)
    # last argument is the image reference
    for img in "$@"; do :; done
    echo "$img" > "$RUNNING_FILE"
    exit 0 ;;
  ps|logs) exit 0 ;;
esac
exit 0
STUB

  # Health depends on which image the stub docker recorded as running.
  cat > "$dir/curl" <<'STUB'
#!/usr/bin/env bash
running="$(cat "$RUNNING_FILE" 2>/dev/null || true)"
[ -n "$running" ] || exit 22
for good in $HEALTHY_IMAGES; do
  if [ "$running" = "$good" ]; then
    sha="${SHA_FOR_RUNNING:-$running}"
    printf '{"status":"ok","build":{"version":"test","sha":"%s"},"pipeline":{"fitted":true}}' "$sha"
    exit 0
  fi
done
exit 22
STUB

  cat > "$dir/aws" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

  # deploy.sh parses the health JSON with python3
  cat > "$dir/python3" <<'STUB'
#!/usr/bin/env bash
exec "$REAL_PYTHON" "$@"
STUB

  chmod +x "$dir"/*
}

run_case() {
  local name="$1" expected="$2"; shift 2
  local tmp; tmp="$(mktemp -d)"
  local stubs="$tmp/bin"
  make_stubs "$stubs"

  export RUNNING_FILE="$tmp/running"
  : > "$RUNNING_FILE"
  export STATE_DIR="$tmp/state"
  export ENV_FILE="$tmp/env"
  mkdir -p "$STATE_DIR"

  cat > "$ENV_FILE" <<ENVEOF
CONTAINER_NAME=loganomaly
API_PORT=8000
AWS_REGION=ap-south-1
LOG_GROUP=/loganomaly/app
S3_BUCKET=
INSTANCE_ID=i-test
ENVEOF

  # scenario-specific setup runs with the temp dirs exported
  "$@"

  local out rc
  out="$(PATH="$stubs:$PATH" HEALTH_TIMEOUT=2 ROLLBACK_TIMEOUT=2 \
        bash "$DEPLOY" "$NEW_IMAGE" "${EXPECT_SHA:-}" 2>&1)"
  rc=$?

  if [ "$rc" = "$expected" ]; then
    printf '  PASS  %-46s exit %s\n' "$name" "$rc"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s exit %s (expected %s)\n' "$name" "$rc" "$expected"
    while IFS= read -r line; do printf '          %s
' "$line"; done <<< "$out"
    FAIL=$((FAIL + 1))
  fi
  rm -rf "$tmp"
}

# ---- scenarios ---------------------------------------------------------------

export REAL_PYTHON
REAL_PYTHON="$(command -v python || command -v python3)"

echo "deploy.sh rollback behaviour"
echo

setup_happy() {
  export NEW_IMAGE="repo:new"
  export HEALTHY_IMAGES="repo:new repo:old"
  export PULL_FAILS=0
  export EXPECT_SHA=""
  echo "repo:old" > "$STATE_DIR/last_good_image"
  echo "repo:old" > "$RUNNING_FILE"
}
run_case "new image healthy -> success" 0 setup_happy

setup_rollback() {
  export NEW_IMAGE="repo:broken"
  export HEALTHY_IMAGES="repo:old"     # new image never answers
  export PULL_FAILS=0
  export EXPECT_SHA=""
  echo "repo:old" > "$STATE_DIR/last_good_image"
  echo "repo:old" > "$RUNNING_FILE"
}
run_case "new image unhealthy -> rolled back, up" 1 setup_rollback

setup_rollback_fails() {
  export NEW_IMAGE="repo:broken"
  export HEALTHY_IMAGES=""             # nothing is healthy
  export PULL_FAILS=0
  export EXPECT_SHA=""
  echo "repo:old" > "$STATE_DIR/last_good_image"
  echo "repo:old" > "$RUNNING_FILE"
}
run_case "rollback also fails -> service down" 2 setup_rollback_fails

setup_pull_fails() {
  export NEW_IMAGE="repo:missing"
  export HEALTHY_IMAGES="repo:old"
  export PULL_FAILS=1
  export EXPECT_SHA=""
  echo "repo:old" > "$STATE_DIR/last_good_image"
  echo "repo:old" > "$RUNNING_FILE"
}
run_case "pull fails -> untouched, deploy failed" 1 setup_pull_fails

setup_wrong_sha() {
  export NEW_IMAGE="repo:new"
  export HEALTHY_IMAGES="repo:new repo:old"
  export PULL_FAILS=0
  export SHA_FOR_RUNNING="deadbeef"    # healthy, but serving the wrong build
  export EXPECT_SHA="cafe1234"
  echo "repo:old" > "$STATE_DIR/last_good_image"
  echo "repo:old" > "$RUNNING_FILE"
}
run_case "healthy but wrong sha -> rolled back" 1 setup_wrong_sha

setup_first_deploy() {
  export NEW_IMAGE="repo:first"
  export HEALTHY_IMAGES="repo:first"
  export PULL_FAILS=0
  export EXPECT_SHA=""
  unset SHA_FOR_RUNNING
  : > "$RUNNING_FILE"                  # nothing running yet
}
run_case "first deploy, nothing running -> success" 0 setup_first_deploy

setup_first_deploy_broken() {
  export NEW_IMAGE="repo:first"
  export HEALTHY_IMAGES=""
  export PULL_FAILS=0
  export EXPECT_SHA=""
  : > "$RUNNING_FILE"                  # no previous image exists to restore
}
run_case "first deploy broken -> no rollback target" 2 setup_first_deploy_broken

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
