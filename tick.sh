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
START_EPOCH="$(date +%s)"
ENGINE="${STEWARD_ENGINE:-claude}"
BIN="${STEWARD_ENGINE_BIN:-${CLAUDE_BIN:-claude}}"
MODEL="${STEWARD_MODEL:-}"
export PROMPT="Read $STEWARD_HOME/STEWARD.md and execute one steward tick, following it exactly."

# Fresh progress feed for this tick (the dashboard polls /api/progress).
printf '{"ts":"%s","phase":"start","msg":"tick started"}\n' "$TS" > progress.jsonl

# Drain maintainer decisions typed since the last chance to run them — they
# unblock work and may change what this tick should do. decide.sh maintains
# .decide.pid, which opens the server's /api/terminal gate for any explicit
# merge/close the maintainer asked for. Entries with a `note` are awaiting the
# maintainer's clarification, not a retry.
if [[ -s decisions.jsonl ]] && \
   jq -se 'map(select(.status=="pending" and (.note|not))) | length > 0' decisions.jsonl >/dev/null 2>&1; then
  printf '{"ts":"%s","phase":"repo","msg":"executing recorded maintainer decisions"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> progress.jsonl
  bash decide.sh || true
fi

# Fresh activity log for this tick — the steward's structured event lines
# (STEWARD.md "Activity log"), folded into audit.jsonl when the tick ends.
# Reset AFTER the decide drain: decide.sh folds its own slice.
: > activity.jsonl

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

# Record real per-chunk completion offsets (seconds from tick start, from file
# mtimes) so the dashboard can estimate remaining time by chunk, not wall clock.
# Chunks: one per repo ledger, plus the metrics and dashboard writes.
{
  printf '{"ts":"%s","total_sec":%s,"chunks":{' "$TS" "$(( $(date +%s) - START_EPOCH ))"
  first=1
  for f in state/*.json metrics.jsonl dashboard.html; do
    [[ -e "$f" ]] || continue
    m="$(stat -c %Y "$f")"
    (( m >= START_EPOCH )) || continue
    name="$(basename "$f")"; name="${name%.json}"; name="${name%.jsonl}"; name="${name%.html}"
    (( first )) || printf ','
    printf '"%s":%s' "$name" "$(( m - START_EPOCH ))"
    first=0
  done
  printf '}}\n'
} >> timings.jsonl

# Fold the tick's structured activity events into the append-only decision
# log, stamping their provenance (fromjson? drops malformed lines). Then the
# run record itself — ts is the tick's START time, deliberately matching this
# tick's usage.jsonl line so a later backfill dedupes against it.
if [[ -s activity.jsonl ]]; then
  jq -cR 'fromjson? | select(type=="object")
          | {v:1, actor:"steward", via:"tick", event:"steward_action"} + .' \
    activity.jsonl >> audit.jsonl || true
fi
DUR=$(( $(date +%s) - START_EPOCH ))
printf '{"v":1,"ts":"%s","actor":"system","via":"tick","event":"tick_done","ok":%s,"summary":"tick finished (rc=%s, %sm)","data":{"rc":%s,"engine":"%s","duration_ms":%s}}\n' \
  "$TS" "$([[ $RC -eq 0 ]] && echo true || echo false)" "$RC" "$(( DUR / 60 ))" "$RC" "$ENGINE" "$(( DUR * 1000 ))" >> audit.jsonl

printf '{"ts":"%s","phase":"done","rc":%s,"msg":"tick complete"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RC" >> progress.jsonl
exit $RC
