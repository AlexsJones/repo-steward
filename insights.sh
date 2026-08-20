#!/usr/bin/env bash
# Runs one read-only, out-of-band repository insight sweep.
set -uo pipefail
STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STEWARD_HOME"
mkdir -p logs

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ENGINE="${STEWARD_ENGINE:-claude}"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-claude}}"
MODEL="${STEWARD_MODEL:-}"
TIMEOUT_SEC="${STEWARD_INSIGHTS_TIMEOUT_SEC:-600}"
export PROMPT="Read $STEWARD_HOME/INSIGHTS.md and execute one repository insight sweep exactly as described. The only output artifact you may write is $STEWARD_HOME/insights.candidate.json."

if ! PREPARED="$(python3 insights.py prepare --root "$STEWARD_HOME" 2>>logs/insights.log)"; then
  echo "=== insights $TS preparation failed ===" >> logs/insights.log
  exit 1
fi
printf '{}\n' > insights.candidate.json

case "$ENGINE" in
  claude)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" -p ${MODEL:+--model "$MODEL"} --output-format json "$PROMPT")"; RC=$?
    if jq -e . >/dev/null 2>&1 <<<"$OUT"; then
      { echo "=== insights $TS engine=claude prepared=$PREPARED (rc=$RC) ==="; jq -r '.result // "(no result text)"' <<<"$OUT"; } >> logs/insights.log
      jq -c --arg ts "$TS" --argjson rc "$RC" '{ts:$ts,rc:$rc,engine:"claude-insights",cost_usd:(.total_cost_usd//null),duration_ms:(.duration_ms//null),num_turns:(.num_turns//null),input_tokens:(.usage.input_tokens//null),output_tokens:(.usage.output_tokens//null),cache_read_tokens:(.usage.cache_read_input_tokens//null),cache_creation_tokens:(.usage.cache_creation_input_tokens//null)}' <<<"$OUT" >> usage.jsonl
    else
      { echo "=== insights $TS engine=claude (rc=$RC, non-json output) ==="; echo "$OUT"; } >> logs/insights.log
    fi
    ;;
  codex)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" exec --sandbox danger-full-access ${MODEL:+--model "$MODEL"} "$PROMPT" 2>&1)"; RC=$?
    { echo "=== insights $TS engine=codex prepared=$PREPARED (rc=$RC) ==="; echo "$OUT"; } >> logs/insights.log
    ;;
  gemini)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" ${MODEL:+-m "$MODEL"} -p "$PROMPT" 2>&1)"; RC=$?
    { echo "=== insights $TS engine=gemini prepared=$PREPARED (rc=$RC) ==="; echo "$OUT"; } >> logs/insights.log
    ;;
  opencode)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" run ${MODEL:+--model "$MODEL"} --format json "$PROMPT" 2>&1)"; RC=$?
    { echo "=== insights $TS engine=opencode prepared=$PREPARED (rc=$RC) ==="; echo "$OUT" | jq -r 'select(.type=="text") | .part.text // empty' 2>/dev/null; } >> logs/insights.log
    ;;
  custom)
    [[ -n "${STEWARD_ENGINE_CMD:-}" ]] || { echo "engine=custom requires STEWARD_ENGINE_CMD" >> logs/insights.log; exit 1; }
    OUT="$(timeout "$TIMEOUT_SEC" bash -c "$STEWARD_ENGINE_CMD" 2>&1)"; RC=$?
    { echo "=== insights $TS engine=custom prepared=$PREPARED (rc=$RC) ==="; echo "$OUT"; } >> logs/insights.log
    ;;
  *) echo "unknown STEWARD_ENGINE '$ENGINE'" >> logs/insights.log; exit 1 ;;
esac

if [[ $RC -ne 0 ]]; then
  exit "$RC"
fi
if ! PUBLISHED="$(python3 insights.py publish --root "$STEWARD_HOME" 2>>logs/insights.log)"; then
  echo "=== insights $TS candidate rejected ===" >> logs/insights.log
  exit 2
fi
echo "=== insights $TS published: $PUBLISHED ===" >> logs/insights.log
python3 - "$TS" "$ENGINE" "$PUBLISHED" <<'PY'
import json, sys
from audit import append
result = json.loads(sys.argv[3])
append("insights_done", "system", "insights", ts=sys.argv[1], ok=True,
       summary=f"insight sweep published {result['themes']} theme(s) and {result['ideas']} idea(s)",
       data={**result, "engine": sys.argv[2]})
PY
