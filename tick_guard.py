#!/usr/bin/env python3
"""Mechanical postconditions for an agent-driven steward tick.

The agent owns judgement and GitHub actions.  This helper owns the facts that
must not depend on the agent following prose perfectly: workflow history in a
ledger is monotonic, and a sync-only run is not successful while actionable
work remains.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path


WORKFLOW_FIELDS = (
    "status",
    "iterations",
    "last_action",
    "last_action_at",
    "verdict",
    "staged_actions",
    "notes",
    "approve_recommend_since",
    "head_oid",
    "review_records",
)
ACTION_KINDS = {"staged", "posted", "labeled", "fix_pr", "escalated", "merged"}
REVIEW_VERDICTS = {"approve-recommend", "iterate", "escalate"}
REVIEW_STATES = {"reviewed", "iterating", "ready-for-maintainer", "escalated"}


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


def has_workflow_evidence(item: dict) -> bool:
    return bool(
        item.get("status") not in (None, "backlog", "done", "dismissed")
        or item.get("last_action")
        or item.get("last_action_at")
        or item.get("verdict")
        or item.get("staged_actions")
    )


def looks_reset(item: dict) -> bool:
    return (
        item.get("status") == "backlog"
        and item.get("iterations", 0) == 0
        and item.get("last_action") is None
        and item.get("last_action_at") is None
        and item.get("verdict") is None
        and not item.get("staged_actions")
    )


def merge_notes(old: str | None, new: str | None) -> str:
    old, new = (old or "").strip(), (new or "").strip()
    if not old:
        return new
    if not new or new in old:
        return old
    if old in new:
        return new
    return f"{old} {new}"


def repair_ledgers(before_dir: Path, state_dir: Path) -> dict:
    repaired_items: list[str] = []
    preserved_fields = 0
    for current_path in sorted(state_dir.glob("*.json")):
        before_path = before_dir / current_path.name
        if not before_path.exists():
            continue
        before, current = read_json(before_path), read_json(current_path)
        changed = False
        for key, item in current.get("items", {}).items():
            old = before.get("items", {}).get(key)
            if not old:
                continue

            # This exact shape is produced when a sync rebuilds an open item
            # from GitHub and forgets to overlay its existing workflow state.
            if looks_reset(item) and has_workflow_evidence(old):
                fresh_notes = item.get("notes")
                for field in WORKFLOW_FIELDS:
                    if field in old:
                        item[field] = old[field]
                item["notes"] = merge_notes(old.get("notes"), fresh_notes)
                repaired_items.append(f"{current_path.stem}:{key}")
                changed = True
                continue

            # Even legitimate transitions must not erase the durable record of
            # what happened previously.  Fresh non-null values always win.
            for field in ("last_action", "last_action_at", "approve_recommend_since",
                          "head_oid", "review_records"):
                if item.get(field) is None and old.get(field) is not None:
                    item[field] = old[field]
                    preserved_fields += 1
                    changed = True
        if changed:
            write_json(current_path, current)
    return {"repaired_items": repaired_items, "preserved_fields": preserved_fields}


def review_action_verdict(action: dict) -> str | None:
    kind = action.get("kind")
    if kind == "pr_review_approve":
        return "approve-recommend"
    if kind == "pr_review_request_changes":
        return "iterate"
    return None


def review_record_errors(item: dict) -> list[str]:
    """Validate the immutable evidence behind the current PR judgment."""
    records = item.get("review_records")
    record = records[-1] if isinstance(records, list) and records else None
    if not isinstance(record, dict):
        return ["missing review_records entry"]
    errors = []
    if record.get("v") != 1:
        errors.append("review_record.v must be 1")
    for field in ("id", "head_oid", "verdict", "body", "recorded_at", "review_basis"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            errors.append(f"review_record.{field} is required")
    if record.get("verdict") not in REVIEW_VERDICTS:
        errors.append("review_record.verdict is invalid")
    if item.get("head_oid") and record.get("head_oid") != item.get("head_oid"):
        errors.append("review_record.head_oid does not match the current PR head")
    if item.get("verdict") and record.get("verdict") != item.get("verdict"):
        errors.append("review_record.verdict does not match the ledger verdict")
    for field in ("claims", "risk_areas", "test_evidence"):
        value = record.get(field)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip()
                                               for v in value):
            errors.append(f"review_record.{field} must be a list of non-empty strings")
    if isinstance(record.get("claims"), list) and not record["claims"]:
        errors.append("review_record.claims must record at least one checked claim")
    if isinstance(record.get("test_evidence"), list) and not record["test_evidence"]:
        errors.append("review_record.test_evidence must state what was or was not run")

    matching_actions = [
        action for action in item.get("staged_actions") or []
        if isinstance(action, dict) and review_action_verdict(action) == record.get("verdict")
    ]
    if matching_actions:
        action = matching_actions[-1]
        if action.get("body") != record.get("body"):
            errors.append("staged review body differs from canonical review_record.body")
        if action.get("review_record_id") != record.get("id"):
            errors.append("staged review is not linked to the canonical review record")
    return errors


def _read_activity(path: Path) -> list[dict]:
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def review_history_preserved(old_records, records) -> bool:
    if not isinstance(old_records, list):
        return True
    if not isinstance(records, list) or len(records) < len(old_records):
        return False
    for old, current in zip(old_records, records):
        if not isinstance(old, dict) or not isinstance(current, dict):
            if old != current:
                return False
            continue
        old_core = {k: v for k, v in old.items() if k != "posted_at"}
        current_core = {k: v for k, v in current.items() if k != "posted_at"}
        if old_core != current_core:
            return False
        old_posted, current_posted = old.get("posted_at"), current.get("posted_at")
        if old_posted != current_posted and not (
                old_posted is None and isinstance(current_posted, str) and current_posted.strip()):
            return False
    return True


def check_review_integrity(before_dir: Path, state_dir: Path, activity_path: Path) -> dict:
    """Reject new review judgments without evaluable local evidence.

    Unchanged historical judgments are reported as legacy gaps, not failures.
    This makes rollout safe while ensuring every judgment made from this tick
    onward is reviewable by the evaluation loop.
    """
    events = _read_activity(activity_path)
    acted_refs = {
        (event.get("repo"), event.get("ref"))
        for event in events
        if event.get("kind") in {"staged", "posted", "escalated"}
        and str(event.get("ref", "")).startswith("pr-")
    }
    posted_refs = {
        (event.get("repo"), event.get("ref"))
        for event in events
        if event.get("kind") == "posted" and str(event.get("ref", "")).startswith("pr-")
    }
    violations = []
    legacy_missing = []
    checked = []
    for current_path in sorted(state_dir.glob("*.json")):
        current = read_json(current_path)
        before_path = before_dir / current_path.name
        before = read_json(before_path) if before_path.exists() else {"items": {}}
        for ref, item in current.get("items", {}).items():
            if item.get("type") != "pr" and not ref.startswith("pr-"):
                continue
            old = before.get("items", {}).get(ref)
            has_judgment = item.get("verdict") in REVIEW_VERDICTS or item.get("status") in REVIEW_STATES
            if not has_judgment and not item.get("review_records"):
                continue
            changed = old is None or any(
                old.get(field) != item.get(field)
                for field in ("status", "verdict", "head_oid", "review_records")
            )
            acted = (current_path.stem, ref) in acted_refs
            errors = review_record_errors(item)
            records = item.get("review_records")
            record = records[-1] if isinstance(records, list) and records else {}
            old_records = old.get("review_records") if isinstance(old, dict) else None
            if not review_history_preserved(old_records, records):
                errors.append("review_records is append-only; a prior record changed or disappeared")
            if (current_path.stem, ref) in posted_refs and not record.get("posted_at"):
                errors.append("posted review must set review_record.posted_at")
            key = f"{current_path.stem}:{ref}"
            if changed or acted:
                checked.append(key)
                if errors:
                    violations.append({"item": key, "errors": errors})
            elif errors and "missing review_records entry" in errors:
                legacy_missing.append(key)
    return {
        "ok": not violations,
        "checked": checked,
        "violations": violations,
        "legacy_missing": legacy_missing,
    }


def recover_from_audit(state_dir: Path, audit_path: Path) -> dict:
    """Recover blank open entries after damage has already reached disk."""
    latest: dict[tuple[str, str], dict] = {}
    if audit_path.exists():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "steward_action" or event.get("kind") not in ACTION_KINDS:
                continue
            repo, ref = event.get("repo"), event.get("ref")
            if repo and ref:
                previous = latest.get((repo, ref))
                if previous is None or event.get("ts", "") >= previous.get("ts", ""):
                    latest[(repo, ref)] = event

    recovered: list[str] = []
    for path in sorted(state_dir.glob("*.json")):
        state, changed = read_json(path), False
        for key, item in state.get("items", {}).items():
            event = latest.get((path.stem, key))
            if not event or not looks_reset(item):
                continue
            kind, summary = event["kind"], event.get("summary", "")
            if kind == "fix_pr":
                status = "fix-in-flight"
            elif kind == "escalated":
                status = "escalated"
            elif kind == "labeled":
                status = "triaged"
            elif item.get("type") == "pr":
                if re.search(r"\bapprov(?:e|ed|ing|al)|approve-recommend", summary, re.I):
                    status = "ready-for-maintainer"
                    item["verdict"] = "approve-recommend"
                else:
                    status = "reviewed"
            else:
                status = "posted"
            item.update(
                status=status,
                last_action=summary,
                last_action_at=event.get("ts"),
            )
            recovered.append(f"{path.stem}:{key}")
            changed = True
        if changed:
            write_json(path, state)
    return {"recovered_items": recovered}


def activity_floor_days(config_path: Path) -> int:
    match = re.search(
        r"^[ \t]*activity_floor_days:[ \t]*(\d+)",
        config_path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return int(match.group(1)) if match else 0


def work_budget_floor(config_path: Path) -> int:
    text = config_path.read_text(encoding="utf-8")
    values = []
    for name in ("substantive_items_per_tick", "light_items_per_tick"):
        match = re.search(rf"^[ \t]*{name}:[ \t]*(\d+)", text, re.MULTILINE)
        if match and int(match.group(1)) > 0:
            values.append(int(match.group(1)))
    return min(values) if values else 1


def conversation_budget_floor(config_path: Path) -> int:
    match = re.search(
        r"^[ \t]*min_conversation_actions_per_tick:[ \t]*(\d+)",
        config_path.read_text(encoding="utf-8"), re.MULTILINE,
    )
    return int(match.group(1)) if match else 0


def in_scope(item: dict, cutoff: str | None) -> bool:
    if "steward-keep" in (item.get("labels") or []):
        return True
    if cutoff is None:
        return True
    activity = item.get("last_activity_at") or item.get("github_updated_at") or item.get("created_at")
    return bool(activity and activity >= cutoff)


def is_actionable(item: dict, cutoff: str | None) -> bool:
    if not in_scope(item, cutoff) or item.get("status") in {
        "done", "dismissed", "ready-for-maintainer", "escalated"
    }:
        return False
    if item.get("status") == "backlog" and not item.get("last_action"):
        return True
    last_activity = item.get("last_activity_at") or item.get("github_updated_at")
    last_action = item.get("last_action_at")
    return bool(last_activity and last_action and last_activity > last_action)


def check_tick(state_dir: Path, config_path: Path, activity_path: Path, now: str) -> dict:
    days = activity_floor_days(config_path)
    cutoff = None
    if days:
        current = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        cutoff = (current - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    by_repo: dict[str, int] = {}
    conversation_by_repo: dict[str, int] = {}
    for path in sorted(state_dir.glob("*.json")):
        items = read_json(path).get("items", {}).values()
        candidates = [item for item in items if is_actionable(item, cutoff)]
        count = len(candidates)
        if count:
            by_repo[path.stem] = count
        conversation_count = sum(item.get("type") in {"issue", "discussion"}
                                 for item in candidates)
        if conversation_count:
            conversation_by_repo[path.stem] = conversation_count

    actions = 0
    conversation_actions = 0
    if activity_path.exists():
        for line in activity_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") in ACTION_KINDS:
                actions += 1
                if str(event.get("ref", "")).startswith(("issue-", "disc-")):
                    conversation_actions += 1
    actionable_total = sum(by_repo.values())
    # An action should transition its item out of the candidate state. Adding
    # remaining + completed therefore estimates the queue available this run,
    # and avoids demanding 20 actions when only (say) three items existed.
    required_actions = min(work_budget_floor(config_path), actionable_total + actions)
    actionable_conversations = sum(conversation_by_repo.values())
    required_conversation_actions = min(
        conversation_budget_floor(config_path),
        actionable_conversations + conversation_actions,
    )
    budget_short = bool(by_repo and actions < required_actions)
    conversation_short = bool(
        conversation_by_repo and conversation_actions < required_conversation_actions
    )
    return {
        "actionable_total": actionable_total,
        "actionable_by_repo": by_repo,
        "queue_actions": actions,
        "required_actions": required_actions,
        "actionable_conversations": actionable_conversations,
        "conversation_by_repo": conversation_by_repo,
        "conversation_actions": conversation_actions,
        "required_conversation_actions": required_conversation_actions,
        "sync_only_failure": bool(by_repo and actions == 0),
        "under_budget_failure": budget_short or conversation_short,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    repair = sub.add_parser("repair")
    repair.add_argument("--before", type=Path, required=True)
    repair.add_argument("--state", type=Path, required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("--state", type=Path, required=True)
    recover.add_argument("--audit", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("--state", type=Path, required=True)
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--activity", type=Path, required=True)
    check.add_argument("--now", required=True)
    review = sub.add_parser("review-check")
    review.add_argument("--before", type=Path, required=True)
    review.add_argument("--state", type=Path, required=True)
    review.add_argument("--activity", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "repair":
        result = repair_ledgers(args.before, args.state)
    elif args.command == "recover":
        result = recover_from_audit(args.state, args.audit)
    elif args.command == "review-check":
        result = check_review_integrity(args.before, args.state, args.activity)
    else:
        result = check_tick(args.state, args.config, args.activity, args.now)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
