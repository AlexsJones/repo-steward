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
START_EPOCH="$(date +%s)"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-$HOME/.local/bin/claude}}"

# Fresh activity log for this run — the executor's structured event lines
# (STEWARD.md step 0), folded into audit.jsonl below. decide.sh never runs
# alongside a tick, so the file is ours for the duration.
: > activity.jsonl
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

# Fold this run's structured activity events into the append-only decision
# log, then the run record — ts matches this run's usage.jsonl line so a
# later backfill dedupes against it.
if [[ -s activity.jsonl ]]; then
  jq -cR 'fromjson? | select(type=="object")
          | {v:1, actor:"steward", via:"decide", event:"steward_action"} + .' \
    activity.jsonl >> audit.jsonl || true
fi
DUR=$(( $(date +%s) - START_EPOCH ))
printf '{"v":1,"ts":"%s","actor":"system","via":"decide","event":"decide_done","ok":%s,"summary":"decision run finished (rc=%s, %sm)","data":{"rc":%s,"engine":"claude-decide","duration_ms":%s}}\n' \
  "$TS" "$([[ $RC -eq 0 ]] && echo true || echo false)" "$RC" "$(( DUR / 60 ))" "$RC" "$(( DUR * 1000 ))" >> audit.jsonl

exit $RC
