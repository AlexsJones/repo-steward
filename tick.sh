#!/usr/bin/env bash
# Runs one steward tick and records token/cost usage to usage.jsonl.
# Invoked by repo-steward.service; environment comes from the unit:
#   STEWARD_ENGINE      claude (default) | codex | gemini | opencode | custom
#   STEWARD_ENGINE_BIN  resolved binary for the engine
#   STEWARD_ENGINE_CMD  full command template for engine=custom ($PROMPT is exported)
#   STEWARD_MODEL       optional model override
set -uo pipefail
STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STEWARD_HOME"
mkdir -p logs

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ENGINE="${STEWARD_ENGINE:-claude}"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-claude}}"
MODEL="${STEWARD_MODEL:-}"
export PROMPT="Read $STEWARD_HOME/STEWARD.md and execute one steward tick, following it exactly."

case "$ENGINE" in
  claude)
    OUT="$("$BIN" -p ${MODEL:+--model "$MODEL"} --output-format json "$PROMPT")"
    RC=$?
    if jq -e . >/dev/null 2>&1 <<<"$OUT"; then
      { echo "=== tick $TS engine=claude (rc=$RC) ==="; jq -r '.result // "(no result text)"' <<<"$OUT"; } >> logs/tick.log
      jq -c --arg ts "$TS" --argjson rc "$RC" '{
        ts: $ts, rc: $rc, engine: "claude",
        cost_usd: (.total_cost_usd // null),
        duration_ms: (.duration_ms // null),
        num_turns: (.num_turns // null),
        input_tokens: (.usage.input_tokens // null),
        output_tokens: (.usage.output_tokens // null),
        cache_read_tokens: (.usage.cache_read_input_tokens // null),
        cache_creation_tokens: (.usage.cache_creation_input_tokens // null)
      }' <<<"$OUT" >> usage.jsonl
    else
      { echo "=== tick $TS engine=claude (rc=$RC, non-json output) ==="; echo "$OUT"; } >> logs/tick.log
      echo "{\"ts\":\"$TS\",\"rc\":$RC,\"engine\":\"claude\",\"error\":\"non-json output\"}" >> usage.jsonl
    fi
    ;;
  codex)
    OUT="$("$BIN" exec ${MODEL:+--model "$MODEL"} "$PROMPT" 2>&1)"; RC=$?
    { echo "=== tick $TS engine=codex (rc=$RC) ==="; echo "$OUT"; } >> logs/tick.log
    echo "{\"ts\":\"$TS\",\"rc\":$RC,\"engine\":\"codex\",\"note\":\"usage not reported by this engine\"}" >> usage.jsonl
    ;;
  gemini)
    OUT="$("$BIN" ${MODEL:+-m "$MODEL"} -p "$PROMPT" 2>&1)"; RC=$?
    { echo "=== tick $TS engine=gemini (rc=$RC) ==="; echo "$OUT"; } >> logs/tick.log
    echo "{\"ts\":\"$TS\",\"rc\":$RC,\"engine\":\"gemini\",\"note\":\"usage not reported by this engine\"}" >> usage.jsonl
    ;;
  opencode)
    OUT="$("$BIN" run ${MODEL:+--model "$MODEL"} "$PROMPT" 2>&1)"; RC=$?
    { echo "=== tick $TS engine=opencode (rc=$RC) ==="; echo "$OUT"; } >> logs/tick.log
    echo "{\"ts\":\"$TS\",\"rc\":$RC,\"engine\":\"opencode\",\"note\":\"usage not reported by this engine\"}" >> usage.jsonl
    ;;
  custom)
    [[ -n "${STEWARD_ENGINE_CMD:-}" ]] || { echo "engine=custom requires STEWARD_ENGINE_CMD" >> logs/tick.log; exit 1; }
    OUT="$(bash -c "$STEWARD_ENGINE_CMD" 2>&1)"; RC=$?
    { echo "=== tick $TS engine=custom (rc=$RC) ==="; echo "$OUT"; } >> logs/tick.log
    echo "{\"ts\":\"$TS\",\"rc\":$RC,\"engine\":\"custom\"}" >> usage.jsonl
    ;;
  *)
    echo "unknown STEWARD_ENGINE '$ENGINE'" >> logs/tick.log; exit 1
    ;;
esac
exit $RC
