#!/usr/bin/env bash
# Runs one steward tick and records token/cost usage to usage.jsonl.
# Invoked by repo-steward.service; CLAUDE_BIN and STEWARD_MODEL come from the unit.
set -uo pipefail
STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STEWARD_HOME"
mkdir -p logs

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BIN="${CLAUDE_BIN:-claude}"
MODEL_ARGS=()
[[ -n "${STEWARD_MODEL:-}" ]] && MODEL_ARGS=(--model "$STEWARD_MODEL")

OUT="$("$BIN" -p "${MODEL_ARGS[@]}" --output-format json \
  "Read $STEWARD_HOME/STEWARD.md and execute one steward tick, following it exactly.")"
RC=$?

# Human-readable tick summary -> tick.log; usage envelope -> usage.jsonl.
if jq -e . >/dev/null 2>&1 <<<"$OUT"; then
  { echo "=== tick $TS (rc=$RC) ==="; jq -r '.result // "(no result text)"' <<<"$OUT"; } >> logs/tick.log
  jq -c --arg ts "$TS" --argjson rc "$RC" '{
    ts: $ts, rc: $rc,
    cost_usd: (.total_cost_usd // null),
    duration_ms: (.duration_ms // null),
    num_turns: (.num_turns // null),
    input_tokens: (.usage.input_tokens // null),
    output_tokens: (.usage.output_tokens // null),
    cache_read_tokens: (.usage.cache_read_input_tokens // null),
    cache_creation_tokens: (.usage.cache_creation_input_tokens // null)
  }' <<<"$OUT" >> usage.jsonl
else
  { echo "=== tick $TS (rc=$RC, non-json output) ==="; echo "$OUT"; } >> logs/tick.log
  echo "{\"ts\":\"$TS\",\"rc\":$RC,\"error\":\"non-json output\"}" >> usage.jsonl
fi
exit $RC
