#!/usr/bin/env bash
# Builds the themes the maintainer queued from the Insights page, one engine
# session per theme, oldest first, until the queue is empty. Started by the
# dashboard's Build button (server.py /api/build); safe to run by hand.
set -uo pipefail
STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STEWARD_HOME"
mkdir -p logs builds work/builds
# Single-flight: one build at a time, however it was started.
exec 9>".build.lock"
flock -n 9 || { echo "=== build $(date -u +%Y-%m-%dT%H:%M:%SZ) skipped: already running ===" >> logs/build.log; exit 75; }
printf '%s\n' "$$" >&9

ENGINE="${STEWARD_ENGINE:-claude}"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-claude}}"
MODEL="${STEWARD_MODEL:-}"
TIMEOUT_SEC="${STEWARD_BUILD_TIMEOUT_SEC:-3600}"
LAST_RC=0

python3 builds.py reap >/dev/null

while :; do
  CLAIM="$(python3 builds.py claim)" || { echo "=== build claim failed ===" >> logs/build.log; exit 1; }
  [[ -z "$CLAIM" ]] && break
  THEME_ID="${CLAIM%%$'\t'*}"
  SLUG="${CLAIM#*$'\t'}"
  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  export PROMPT="Read $STEWARD_HOME/BUILD.md and build the theme described in $STEWARD_HOME/builds/$SLUG.json exactly as it says. Write the result to $STEWARD_HOME/builds/$SLUG.result.json."

  case "$ENGINE" in
    claude)
      OUT="$(timeout "$TIMEOUT_SEC" "$BIN" -p ${MODEL:+--model "$MODEL"} --output-format json "$PROMPT")"; RC=$?
      if jq -e . >/dev/null 2>&1 <<<"$OUT"; then
        { echo "=== build $TS $THEME_ID engine=claude (rc=$RC) ==="; jq -r '.result // "(no result text)"' <<<"$OUT"; } >> logs/build.log
        jq -c --arg ts "$TS" --argjson rc "$RC" '{ts:$ts,rc:$rc,engine:"claude-build",cost_usd:(.total_cost_usd//null),duration_ms:(.duration_ms//null),num_turns:(.num_turns//null),input_tokens:(.usage.input_tokens//null),output_tokens:(.usage.output_tokens//null),cache_read_tokens:(.usage.cache_read_input_tokens//null),cache_creation_tokens:(.usage.cache_creation_input_tokens//null)}' <<<"$OUT" >> usage.jsonl
      else
        { echo "=== build $TS $THEME_ID engine=claude (rc=$RC, non-json output) ==="; echo "$OUT"; } >> logs/build.log
      fi
      ;;
    codex)
      OUT="$(timeout "$TIMEOUT_SEC" "$BIN" exec --sandbox danger-full-access ${MODEL:+--model "$MODEL"} "$PROMPT" 2>&1)"; RC=$?
      { echo "=== build $TS $THEME_ID engine=codex (rc=$RC) ==="; echo "$OUT"; } >> logs/build.log
      ;;
    gemini)
      OUT="$(timeout "$TIMEOUT_SEC" "$BIN" ${MODEL:+-m "$MODEL"} -p "$PROMPT" 2>&1)"; RC=$?
      { echo "=== build $TS $THEME_ID engine=gemini (rc=$RC) ==="; echo "$OUT"; } >> logs/build.log
      ;;
    opencode)
      OUT="$(timeout "$TIMEOUT_SEC" "$BIN" run ${MODEL:+--model "$MODEL"} --format json "$PROMPT" 2>&1)"; RC=$?
      { echo "=== build $TS $THEME_ID engine=opencode (rc=$RC) ==="; echo "$OUT" | jq -r 'select(.type=="text") | .part.text // empty' 2>/dev/null; } >> logs/build.log
      ;;
    custom)
      if [[ -z "${STEWARD_ENGINE_CMD:-}" ]]; then
        echo "engine=custom requires STEWARD_ENGINE_CMD" >> logs/build.log; RC=1
      else
        OUT="$(timeout "$TIMEOUT_SEC" bash -c "$STEWARD_ENGINE_CMD" 2>&1)"; RC=$?
        { echo "=== build $TS $THEME_ID engine=custom (rc=$RC) ==="; echo "$OUT"; } >> logs/build.log
      fi
      ;;
    *) echo "unknown STEWARD_ENGINE '$ENGINE'" >> logs/build.log; RC=1 ;;
  esac

  RESULT="$(python3 builds.py finish "$THEME_ID" "$RC" 2>>logs/build.log)"
  echo "=== build $TS $THEME_ID finished: $(jq -c '{status,pr_url}' <<<"$RESULT" 2>/dev/null || echo "$RESULT") ===" >> logs/build.log
  LAST_RC=$RC
done
exit "$LAST_RC"
