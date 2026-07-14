#!/usr/bin/env python3
"""The decision log: audit.jsonl — an append-only, serialisable record of
every decision and action across the steward system. approvals.jsonl,
decisions.jsonl and the ledgers each hold one channel's raw writes (and
ledger fields get overwritten); this file is the unified history that never
loses an event.

Schema v1 — one JSON object per line:
  v        1 (schema version)
  ts       UTC ISO-8601, when the thing happened
  actor    maintainer | steward | system
  via      dashboard | tick | decide
  event    what happened (see below)
  repo     short repo name; absent when not repo-scoped
  ref      ledger key (pr-123 / issue-45 / disc-7); absent when not item-scoped
  summary  one human-readable line
  ok       true/false when the event records an execution attempt
  detail   raw executor output (truncated) when there is one
  data     small event-specific dict; data.backfilled marks migrated history

Events:
  approve            maintainer approved a staged item (dashboard click);
                     data.merged says whether it merged too
  dismiss            maintainer dismissed a staged item (nothing posted)
  decision_recorded  maintainer typed a free-text decision;
                     data.decision_ts identifies it (= its decisions.jsonl ts)
  decision_executed  the decision executor carried a decision out
  terminal           explicit maintainer merge/close executed via /api/terminal
  config_change      mode / schedule / limits / watch changed from the dashboard
  tick_requested     maintainer started a tick from the dashboard
  tick_done          a tick finished (ts = the tick's start, matching usage.jsonl)
  decide_done        a decision-executor run finished
  steward_action     one discrete thing the steward did or observed in a run —
                     written to activity.jsonl during the run, folded in by
                     tick.sh / decide.sh; data.kind: staged | posted | labeled |
                     fix_pr | escalated | observed

The log is append-only: nothing rewrites or deletes lines. `python3 audit.py
backfill` converts history that predates the log (approvals.jsonl,
decisions.jsonl, usage.jsonl) into the same format; it is idempotent, so
re-running never duplicates events.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "audit.jsonl"


def now_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append(event, actor, via, repo="", ref="", summary="", ok=None,
           detail="", data=None, ts=None):
    e = {"v": 1, "ts": ts or now_ts(), "actor": actor, "via": via, "event": event}
    if repo:
        e["repo"] = repo
    if ref:
        e["ref"] = ref
    if summary:
        e["summary"] = summary[:300]
    if ok is not None:
        e["ok"] = bool(ok)
    if detail:
        e["detail"] = detail[:300]
    if data:
        e["data"] = data
    with open(LOG, "a") as f:
        f.write(json.dumps(e) + "\n")
    return e


def read_events(limit=200, repo=None, event=None):
    """The last `limit` events, oldest first, optionally filtered."""
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if repo and o.get("repo") != repo:
            continue
        if event and o.get("event") != event:
            continue
        out.append(o)
    return out[-limit:]


def _jsonl(name):
    p = ROOT / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def backfill():
    """Convert pre-decision-log history into audit events. Idempotent: an
    event is skipped when one with the same (ts, event, repo, ref) already
    exists — and decision events also by their originating decision_ts, since
    live decision_executed events carry the real execution time while
    backfilled ones can only reuse the entry's ts."""
    seen, seen_dec = set(), set()
    for o in read_events(limit=10**9):
        seen.add((o.get("ts"), o.get("event"), o.get("repo", ""), o.get("ref", "")))
        dts = (o.get("data") or {}).get("decision_ts")
        if dts:
            seen_dec.add((o.get("event"), dts))

    events = []
    for o in _jsonl("approvals.jsonl"):
        ts, repo, action = o.get("ts"), o.get("repo", ""), o.get("action", "")
        if action.startswith("decision-"):
            verb = action.split("-", 1)[1]
            reason = (o.get("reason") or "").strip()
            events.append(dict(
                event="terminal", actor="maintainer", via="decide", ts=ts,
                repo=repo.split("/")[1] if "/" in repo else repo,
                ref=o.get("item", ""), ok=o.get("ok"), detail=o.get("detail", ""),
                summary=f"maintainer decision: {verb}" + (f" — {reason}" if reason else ""),
                data={"action": verb}))
            continue
        for key, oc in (o.get("outcomes") or {}).items():
            if action == "dismiss":
                events.append(dict(
                    event="dismiss", actor="maintainer", via="dashboard", ts=ts,
                    repo=repo, ref=key, ok=oc.get("ok"),
                    summary="dismissed via dashboard (nothing posted)"))
            else:
                merged = bool(oc.get("merged"))
                events.append(dict(
                    event="approve", actor="maintainer", via="dashboard", ts=ts,
                    repo=repo, ref=key, ok=oc.get("ok"), detail=oc.get("detail", ""),
                    summary="approved via dashboard" + (" & merged" if merged else ""),
                    data={"merged": merged}))

    for o in _jsonl("decisions.jsonl"):
        ts = o.get("ts")
        events.append(dict(
            event="decision_recorded", actor="maintainer", via="dashboard", ts=ts,
            repo=o.get("repo", ""),
            summary=o.get("title") or o.get("decision", "")[:120],
            data={"decision_ts": ts, "decision": o.get("decision", "")[:500],
                  "refs": o.get("refs", [])}))
        status = o.get("status")
        if status and status != "pending":
            events.append(dict(
                event="decision_executed", actor="steward", via="decide", ts=ts,
                repo=o.get("repo", ""), ok=status == "executed",
                summary=o.get("outcome") or f"decision {status}",
                data={"decision_ts": ts, "status": status}))

    for o in _jsonl("usage.jsonl"):
        decide = str(o.get("engine", "")).endswith("-decide")
        bits = [f"rc={o.get('rc')}"]
        if o.get("duration_ms"):
            bits.append(f"{round(o['duration_ms'] / 60000)}m")
        if o.get("cost_usd") is not None:
            bits.append(f"${o['cost_usd']:.2f}")
        events.append(dict(
            event="decide_done" if decide else "tick_done", actor="system",
            via="decide" if decide else "tick", ts=o.get("ts"),
            ok=o.get("rc") == 0,
            summary=("decision run" if decide else "tick") + f" finished ({', '.join(bits)})",
            data={k: o[k] for k in ("rc", "engine", "cost_usd", "duration_ms", "num_turns")
                  if o.get(k) is not None}))

    added = 0
    events.sort(key=lambda e: e.get("ts") or "")
    for e in events:
        key = (e.get("ts"), e["event"], e.get("repo", ""), e.get("ref", ""))
        dts = (e.get("data") or {}).get("decision_ts")
        if key in seen or (dts and (e["event"], dts) in seen_dec):
            continue
        seen.add(key)
        if dts:
            seen_dec.add((e["event"], dts))
        e.setdefault("data", {})["backfilled"] = True
        append(**e)
        added += 1
    return added


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        print(f"backfilled {backfill()} events into {LOG.name}")
    else:
        sys.exit(__doc__.strip())
