#!/usr/bin/env bash
# Repo Steward installer — systemd user units for the tick timer and dashboard.
#
#   ./install.sh                 install + enable dashboard and hourly timer
#   ./install.sh --no-timer      install everything but leave the timer off
#
# Env overrides:
#   STEWARD_MODEL   pin a model for ticks (e.g. claude-opus-4-8); default: your
#                   Claude Code default model
#   STEWARD_PORT    dashboard port (default 8377)
#   STEWARD_CADENCE systemd OnCalendar for ticks (default "*-*-* *:17:00" = hourly at :17)
set -euo pipefail

STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PORT="${STEWARD_PORT:-8377}"
CADENCE="${STEWARD_CADENCE:-*-*-* *:17:00}"
ENABLE_TIMER=true
[[ "${1:-}" == "--no-timer" ]] && ENABLE_TIMER=false

CLAUDE_BIN="$(command -v claude || true)"
[[ -n "$CLAUDE_BIN" ]] || { echo "error: claude CLI not found in PATH (https://claude.com/claude-code)"; exit 1; }
command -v gh >/dev/null || { echo "error: gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh is not authenticated (run: gh auth login)"; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 not found"; exit 1; }

MODEL_FLAG=""
[[ -n "${STEWARD_MODEL:-}" ]] && MODEL_FLAG=" --model ${STEWARD_MODEL}"

mkdir -p "$STEWARD_HOME"/{state,logs} "$UNIT_DIR"
[[ -f "$STEWARD_HOME/config.yaml" ]] || {
  cp "$STEWARD_HOME/config.example.yaml" "$STEWARD_HOME/config.yaml"
  echo ">> created config.yaml from example — EDIT IT before starting the timer"
}

cat > "$UNIT_DIR/repo-steward.service" <<EOF
[Unit]
Description=Repo Steward tick — autonomous OSS issue/PR care
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$STEWARD_HOME
Environment=CLAUDE_BIN=$CLAUDE_BIN
${STEWARD_MODEL:+Environment=STEWARD_MODEL=$STEWARD_MODEL}
ExecStart=$STEWARD_HOME/tick.sh
TimeoutStartSec=5400
StandardError=append:$STEWARD_HOME/logs/tick.log
EOF

cat > "$UNIT_DIR/repo-steward.timer" <<EOF
[Unit]
Description=Repo Steward tick schedule

[Timer]
OnCalendar=$CADENCE
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/repo-steward-dash.service" <<EOF
[Unit]
Description=Repo Steward dashboard — local server
After=network.target

[Service]
Environment=STEWARD_PORT=$PORT
ExecStart=$(command -v python3) $STEWARD_HOME/server.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF

# Headless claude needs the steward home pre-trusted or it ignores
# .claude/settings.json permissions.
if command -v jq >/dev/null && [[ -f "$HOME/.claude.json" ]]; then
  jq --arg d "$STEWARD_HOME" '.projects[$d].hasTrustDialogAccepted = true' \
    "$HOME/.claude.json" > "$HOME/.claude.json.tmp" && mv "$HOME/.claude.json.tmp" "$HOME/.claude.json"
else
  echo ">> NOTE: run claude interactively in $STEWARD_HOME once and accept the trust dialog"
fi

systemctl --user daemon-reload
systemctl --user enable --now repo-steward-dash.service
if $ENABLE_TIMER; then
  systemctl --user enable --now repo-steward.timer
  echo ">> timer enabled: $CADENCE (±5 min jitter)"
else
  echo ">> timer NOT enabled; start ticks manually: systemctl --user start repo-steward.service"
fi

echo ">> dashboard: http://localhost:$PORT/"
echo ">> first tick: systemctl --user start repo-steward.service   (watch logs/tick.log)"
