#!/usr/bin/env bash
# Repo Steward installer — systemd user units for the tick timer and dashboard.
#
#   ./install.sh                 install + enable dashboard and hourly timer
#   ./install.sh --no-timer      install everything but leave the timer off
#
# Env overrides:
#   STEWARD_ENGINE     agent CLI running the tick: claude (default) | codex |
#                      gemini | opencode | custom
#   STEWARD_ENGINE_CMD full command for engine=custom ($PROMPT is exported)
#   STEWARD_MODEL      pin a model for ticks (e.g. claude-opus-4-8); default:
#                      the engine's own default
#   STEWARD_PORT       dashboard port (default 8377)
#   STEWARD_CADENCE    systemd OnCalendar for ticks (default "*-*-* *:17:00" = hourly at :17)
set -euo pipefail

STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PORT="${STEWARD_PORT:-8377}"
CADENCE="${STEWARD_CADENCE:-*-*-* *:17:00}"
ENABLE_TIMER=true
[[ "${1:-}" == "--no-timer" ]] && ENABLE_TIMER=false

ENGINE="${STEWARD_ENGINE:-claude}"
if [[ "$ENGINE" == "custom" ]]; then
  [[ -n "${STEWARD_ENGINE_CMD:-}" ]] || { echo "error: engine=custom requires STEWARD_ENGINE_CMD"; exit 1; }
  ENGINE_BIN=""
else
  ENGINE_BIN="$(command -v "$ENGINE" || true)"
  [[ -n "$ENGINE_BIN" ]] || { echo "error: '$ENGINE' CLI not found in PATH"; exit 1; }
fi
GH_BIN="$(command -v gh || true)"
[[ -n "$GH_BIN" ]] || { echo "error: gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh is not authenticated (run: gh auth login)"; exit 1; }
PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "error: python3 not found"; exit 1; }
JQ_BIN="$(command -v jq || true)"
[[ -n "$JQ_BIN" ]] || { echo "error: jq not found"; exit 1; }

# The systemd user manager starts with a minimal PATH (typically
# /usr/local/bin:/usr/bin) and sources no shell rc, so a tool found here may be
# invisible to the tick — `command -v` in this shell proves nothing about what
# the service resolves. tick.sh/decide.sh call `gh` and `jq` by bare name, so
# carry the directories of the tools we actually resolved into the unit's PATH.
STEWARD_PATH=""
for _d in "$ENGINE_BIN" "$GH_BIN" "$JQ_BIN" "$PYTHON_BIN" /usr/local/bin /usr/bin; do
  [[ -n "$_d" ]] || continue
  [[ -d "$_d" ]] || _d="$(dirname "$_d")"
  case ":$STEWARD_PATH:" in *":$_d:"*) ;; *) STEWARD_PATH="${STEWARD_PATH:+$STEWARD_PATH:}$_d" ;; esac
done

# Same trap for auth: a token exported from an interactive rc file does not
# reach the service. Persist it to a 0600 EnvironmentFile rather than an inline
# Environment= line, which `systemctl show` exposes to any local user.
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/repo-steward/env"
_tok="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -n "$_tok" ]]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  ( umask 077; printf 'GITHUB_TOKEN=%s\n' "$_tok" > "$ENV_FILE" )
  chmod 600 "$ENV_FILE"
  echo ">> wrote $ENV_FILE (0600) — the tick's gh credential"
elif [[ ! -f "${GH_CONFIG_DIR:-$HOME/.config/gh}/hosts.yml" ]]; then
  echo "error: gh is authenticated here, but via neither GH_TOKEN/GITHUB_TOKEN nor"
  echo "       a hosts.yml the service can read. The tick would start unauthenticated."
  exit 1
else
  ENV_FILE=""   # hosts.yml under $HOME — systemd sets HOME, so gh finds it.
fi

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
Environment=STEWARD_ENGINE=$ENGINE
Environment=PATH=$STEWARD_PATH
${ENGINE_BIN:+Environment=STEWARD_ENGINE_BIN=$ENGINE_BIN}
${STEWARD_ENGINE_CMD:+Environment=STEWARD_ENGINE_CMD=$STEWARD_ENGINE_CMD}
${STEWARD_MODEL:+Environment=STEWARD_MODEL=$STEWARD_MODEL}
${ENV_FILE:+EnvironmentFile=$ENV_FILE}
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
# server.py shells out to gh for every maintainer-approved post, so it needs the
# same PATH and credential as the tick — without them subprocess raises
# FileNotFoundError and the dashboard button fails with no cause shown.
Environment=PATH=$STEWARD_PATH
${ENV_FILE:+EnvironmentFile=$ENV_FILE}
ExecStart=$PYTHON_BIN $STEWARD_HOME/server.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF

# Headless claude needs the steward home pre-trusted or it ignores
# .claude/settings.json permissions (the merge/close/force-push deny layer).
if [[ "$ENGINE" == "claude" ]]; then
  if [[ -f "$HOME/.claude.json" ]]; then
    jq --arg d "$STEWARD_HOME" '.projects[$d].hasTrustDialogAccepted = true' \
      "$HOME/.claude.json" > "$HOME/.claude.json.tmp" && mv "$HOME/.claude.json.tmp" "$HOME/.claude.json"
  else
    echo ">> NOTE: run claude interactively in $STEWARD_HOME once and accept the trust dialog"
  fi
else
  echo ">> NOTE: engine '$ENGINE' does not enforce the merge/close/force-push deny layer"
  echo "   (.claude/settings.json is Claude Code-specific) — the playbook guardrails still"
  echo "   apply, but review your engine's own sandbox/approval settings."
fi

cat > "$UNIT_DIR/repo-steward-uptime.service" <<EOF
[Unit]
Description=Repo Steward uptime probe (token-free)

[Service]
Type=oneshot
WorkingDirectory=$STEWARD_HOME
ExecStart=$(command -v python3) $STEWARD_HOME/uptime_check.py
EOF

cat > "$UNIT_DIR/repo-steward-uptime.timer" <<EOF
[Unit]
Description=Repo Steward uptime probe schedule

[Timer]
OnCalendar=*:0/5
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload

# Prove gh works where the tick will actually run it. The preflight above only
# tested this shell; a timer enabled on the strength of that can fail every
# firing with `gh: 127` while `gh --version` keeps working when you check by
# hand. Verify in a transient unit under the same manager, or refuse to arm.
if systemd-run --user --wait --collect --quiet \
     --property="Environment=PATH=$STEWARD_PATH" \
     ${ENV_FILE:+--property="EnvironmentFile=$ENV_FILE"} \
     /bin/bash -c 'command -v gh >/dev/null && gh auth status >/dev/null 2>&1'; then
  echo ">> verified: gh resolves and authenticates under systemd --user"
else
  echo "error: gh works in this shell but NOT under the systemd user manager."
  echo "       The tick would fail every firing. PATH given to the unit:"
  echo "       $STEWARD_PATH"
  exit 1
fi

systemctl --user enable repo-steward-dash.service
# restart, not `enable --now`: on a re-install the dashboard is already running
# and would keep serving with the previous unit's environment.
systemctl --user restart repo-steward-dash.service
# Only run the uptime probe if the user configured sites.
if grep -qE "^sites:" "$STEWARD_HOME/config.yaml" 2>/dev/null; then
  systemctl --user enable --now repo-steward-uptime.timer
  echo ">> uptime probe enabled (every 5 min)"
fi
if $ENABLE_TIMER; then
  systemctl --user enable --now repo-steward.timer
  echo ">> timer enabled: $CADENCE (±5 min jitter)"
else
  echo ">> timer NOT enabled; start ticks manually: systemctl --user start repo-steward.service"
fi

echo ">> dashboard: http://localhost:$PORT/"
echo ">> first tick: systemctl --user start repo-steward.service   (watch logs/tick.log)"
