#!/usr/bin/env bash
# Executes pending maintainer decisions (decisions.jsonl) with one focused
# engine run. Spawned by server.py when the maintainer types a decision into
# the dashboard while no tick is running; a tick executes leftovers itself
# (STEWARD.md step 0). Records usage to usage.jsonl like tick.sh.
set -uo pipefail
STEWARD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STEWARD_HOME"
mkdir -p logs

# The pidfile is the server's evidence that a decision executor is running —
# it gates /api/terminal (explicit merge/close typed by the maintainer).
echo $$ > .decide.pid
trap 'rm -f .decide.pid' EXIT

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-$HOME/.local/bin/claude}}"
PROMPT="Read $STEWARD_HOME/STEWARD.md (the guardrails and step 0) and execute the pending maintainer decisions in $STEWARD_HOME/decisions.jsonl now, exactly as step 0 describes. Do not run a full tick; touch only what the decisions require."

OUT="$("$BIN" -p --output-format json "$PROMPT")"
RC=$?
if jq -e . >/dev/null 2>&1 <<<"$OUT"; then
  { echo "=== decide $TS (rc=$RC) ==="; jq -r '.result // "(no result text)"' <<<"$OUT"; } >> logs/decide.log
  jq -c --arg ts "$TS" --argjson rc "$RC" '{
    ts: $ts, rc: $rc, engine: "claude-decide",
    cost_usd: (.total_cost_usd // null),
    duration_ms: (.duration_ms // null),
    num_turns: (.num_turns // null),
    input_tokens: (.usage.input_tokens // null),
    output_tokens: (.usage.output_tokens // null),
    cache_read_tokens: (.usage.cache_read_input_tokens // null),
    cache_creation_tokens: (.usage.cache_creation_input_tokens // null)
  }' <<<"$OUT" >> usage.jsonl
else
  { echo "=== decide $TS (rc=$RC, non-json output) ==="; echo "$OUT"; } >> logs/decide.log
  echo "{\"ts\":\"$TS\",\"rc\":$RC,\"engine\":\"claude-decide\",\"error\":\"non-json output\"}" >> usage.jsonl
fi
exit $RC
