#!/usr/bin/env bash
# Runs one read-only, out-of-band critical evaluation of steward judgments.
set -uo pipefail
STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STEWARD_HOME"
mkdir -p logs
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ENGINE="${STEWARD_ENGINE:-claude}"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-claude}}"
MODEL="${STEWARD_MODEL:-}"
TIMEOUT_SEC="${STEWARD_EVALUATION_TIMEOUT_SEC:-600}"
export PROMPT="Read $STEWARD_HOME/EVALUATION.md and run one critical self-evaluation exactly as described. The only output artifact you may write is $STEWARD_HOME/evaluation.candidate.json."

python3 signals.py collect --root "$STEWARD_HOME" >>logs/evaluation.log 2>&1 || exit 1
PREPARED="$(python3 evaluation.py prepare --root "$STEWARD_HOME" 2>>logs/evaluation.log)" || exit 1
printf '{}\n' > evaluation.candidate.json

case "$ENGINE" in
  claude)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" -p ${MODEL:+--model "$MODEL"} --output-format json "$PROMPT")"; RC=$?
    if jq -e . >/dev/null 2>&1 <<<"$OUT"; then
      { echo "=== evaluation $TS engine=claude prepared=$PREPARED (rc=$RC) ==="; jq -r '.result // "(no result text)"' <<<"$OUT"; } >> logs/evaluation.log
      jq -c --arg ts "$TS" --argjson rc "$RC" '{ts:$ts,rc:$rc,engine:"claude-evaluation",cost_usd:(.total_cost_usd//null),duration_ms:(.duration_ms//null),num_turns:(.num_turns//null),input_tokens:(.usage.input_tokens//null),output_tokens:(.usage.output_tokens//null),cache_read_tokens:(.usage.cache_read_input_tokens//null),cache_creation_tokens:(.usage.cache_creation_input_tokens//null)}' <<<"$OUT" >> usage.jsonl
    else
      { echo "=== evaluation $TS engine=claude (rc=$RC, non-json output) ==="; echo "$OUT"; } >> logs/evaluation.log
    fi ;;
  codex)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" exec --sandbox danger-full-access ${MODEL:+--model "$MODEL"} "$PROMPT" 2>&1)"; RC=$?
    { echo "=== evaluation $TS engine=codex prepared=$PREPARED (rc=$RC) ==="; echo "$OUT"; } >> logs/evaluation.log ;;
  gemini)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" ${MODEL:+-m "$MODEL"} -p "$PROMPT" 2>&1)"; RC=$?
    { echo "=== evaluation $TS engine=gemini prepared=$PREPARED (rc=$RC) ==="; echo "$OUT"; } >> logs/evaluation.log ;;
  opencode)
    OUT="$(timeout "$TIMEOUT_SEC" "$BIN" run ${MODEL:+--model "$MODEL"} --format json "$PROMPT" 2>&1)"; RC=$?
    { echo "=== evaluation $TS engine=opencode prepared=$PREPARED (rc=$RC) ==="; echo "$OUT" | jq -r 'select(.type=="text") | .part.text // empty' 2>/dev/null; } >> logs/evaluation.log ;;
  custom)
    [[ -n "${STEWARD_ENGINE_CMD:-}" ]] || exit 1
    OUT="$(timeout "$TIMEOUT_SEC" bash -c "$STEWARD_ENGINE_CMD" 2>&1)"; RC=$?
    { echo "=== evaluation $TS engine=custom prepared=$PREPARED (rc=$RC) ==="; echo "$OUT"; } >> logs/evaluation.log ;;
  *) echo "unknown STEWARD_ENGINE '$ENGINE'" >>logs/evaluation.log; exit 1 ;;
esac
[[ $RC -eq 0 ]] || exit "$RC"
PUBLISHED="$(python3 evaluation.py publish --root "$STEWARD_HOME" 2>>logs/evaluation.log)" || {
  echo "=== evaluation $TS candidate rejected ===" >>logs/evaluation.log; exit 2; }
echo "=== evaluation $TS published: $PUBLISHED ===" >>logs/evaluation.log
python3 - "$TS" "$ENGINE" "$PUBLISHED" <<'PY'
import json, sys
from audit import append
r=json.loads(sys.argv[3])
append("evaluation_done","system","evaluation",ts=sys.argv[1],ok=True,
       summary=f"self-evaluation published {r['findings']} finding(s) and {r['lessons']} lesson(s)",
       data={**r,"engine":sys.argv[2]})
PY
