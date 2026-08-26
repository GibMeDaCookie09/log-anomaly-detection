#!/usr/bin/env bash
#
# Installs the self-monitoring timer. Run by the deploy workflow after the
# container rolls, so the timer and the script it runs are versioned with the
# code rather than baked into the instance at first boot.
#
# Idempotent: safe to run on every deploy.
set -euo pipefail

SRC="${1:-/tmp/loganomaly-deploy}"
STATE_DIR="${STATE_DIR:-/opt/loganomaly}"

install -m 0755 "$SRC/run_self_monitor.sh" "$STATE_DIR/run_self_monitor.sh"
sudo install -m 0644 "$SRC/self-monitor.service" /etc/systemd/system/self-monitor.service
sudo install -m 0644 "$SRC/self-monitor.timer" /etc/systemd/system/self-monitor.timer

sudo systemctl daemon-reload
sudo systemctl enable --now self-monitor.timer

echo "self-monitor.timer installed:"
systemctl list-timers self-monitor.timer --no-pager || true
