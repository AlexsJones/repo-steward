#!/usr/bin/env python3
"""Build the append-only evidence stream used by repository insights.

The operational ledgers are mutable current state.  This module snapshots
their useful facts, plus audit events and metric samples, into signals.jsonl.
Every record has stable graph identities and explicit evidence; later insight
jobs may derive themes and ideas without changing this source record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
TEXT_LIMIT = 8000


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_entries(config_path: Path) -> list[dict]:
    """Return owner/name entries without taking a YAML dependency."""
    if not config_path.exists():
        return []
    entries = []
    for line in config_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*-\s*name:\s*['\"]?([^'\"\s#]+/[^'\"\s#]+)", line)
        if match:
            full = match.group(1)
            entries.append({"name": full, "short": full.rsplit("/", 1)[-1]})
    return entries


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def clean_text(value, limit: int = TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:limit]


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def signal_id(kind: str, source_key: str, payload: dict) -> str:
    digest = hashlib.sha256(
        f"{kind}\0{source_key}\0{canonical(payload)}".encode("utf-8")
    ).hexdigest()[:24]
    return f"sig_{digest}"


def repo_value(full: str) -> dict:
    return {"id": f"repo:{full}", "name": full, "short": full.rsplit("/", 1)[-1]}


def item_url(full: str, ref: str, item: dict) -> str:
    if item.get("url"):
        return clean_text(item["url"], 2000)
    kind, _, number = ref.partition("-")
    segment = {"issue": "issues", "pr": "pull", "disc": "discussions"}.get(kind, kind)
    return f"https://github.com/{full}/{segment}/{number}"


def recent_comment_text(item: dict) -> list[dict]:
    comments = item.get("recent_comments") or item.get("comments") or []
    if not isinstance(comments, list):
        return []
    out = []
    for comment in comments[-3:]:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or {}
        if isinstance(author, dict):
            author = author.get("login") or ""
        body = clean_text(comment.get("body") or comment.get("bodyText"), 4000)
        if body:
            out.append({
                "author": clean_text(author, 200),
                "updated_at": comment.get("updatedAt") or comment.get("updated_at"),
                "text": body,
            })
    return out


def normalized_analysis(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    out = {}
    for key in ("intent", "user_goal", "impact", "workaround", "change_goal",
                "review_basis", "test_evidence"):
        text = clean_text(value.get(key), 1000)
        if text:
            out[key] = text
    for key in ("topics", "components", "symptoms", "risk_areas"):
        values = value.get(key)
        if isinstance(values, list):
            cleaned = [clean_text(entry, 300) for entry in values[:12]]
            cleaned = [entry for entry in cleaned if entry]
            if cleaned:
                out[key] = cleaned
    confidence = value.get("confidence")
    if confidence in {"low", "medium", "high"}:
        out["confidence"] = confidence
    return out or None


def normalized_attributes(item: dict) -> dict:
    attributes = {}
    text_fields = (
        "title", "status", "category", "created_at", "github_updated_at",
        "last_activity_at", "verdict", "review_decision", "head_ref", "head_oid",
    )
    for key in text_fields:
        value = item.get(key)
        if value is not None:
            attributes[key] = clean_text(value, 1000)
    if "created_at" not in attributes and item.get("created"):
        attributes["created_at"] = clean_text(item["created"], 1000)
    author = item.get("author")
    if isinstance(author, dict):
        author = author.get("login")
    if author:
        attributes["author"] = clean_text(author, 200)
    labels = item.get("labels")
    if isinstance(labels, list):
        names = []
        for label in labels:
            if isinstance(label, dict):
                label = label.get("name")
            name = clean_text(label, 200)
            if name:
                names.append(name)
        attributes["labels"] = names
    for key in ("is_answered", "is_draft"):
        if isinstance(item.get(key), bool):
            attributes[key] = item[key]
    for key in ("iterations", "additions", "deletions"):
        if isinstance(item.get(key), int):
            attributes[key] = item[key]
    comment_count = item.get("comment_count", item.get("comments_count"))
    if isinstance(comment_count, int):
        attributes["comment_count"] = comment_count
    return attributes


def item_signal(full: str, ref: str, item: dict, observed_at: str) -> dict:
    item_kind = item.get("type") or ref.partition("-")[0]
    url = item_url(full, ref, item)
    body = clean_text(item.get("body") or item.get("bodyText"))
    evidence = [{"type": "github", "url": url, "label": clean_text(item.get("title"), 500)}]
    if body:
        evidence[0]["text"] = body
    comments = recent_comment_text(item)
    if comments:
        evidence[0]["recent_comments"] = comments

    attributes = normalized_attributes(item)
    # Optional agent-authored extraction made while the item was already being
    # read for operational work. It is explicitly analysis, never source fact.
    analysis = normalized_analysis(item.get("signal"))
    payload = {
        "subject": {"id": f"repo:{full}/{ref}", "kind": item_kind, "ref": ref},
        "source": {"system": "github", "url": url,
                   "updated_at": item.get("github_updated_at") or item.get("last_activity_at")},
        "attributes": attributes,
        "evidence": evidence,
    }
    if analysis:
        payload["analysis"] = analysis
    workflow = {}
    for key in ("last_action", "last_action_at", "notes", "approve_recommend_since"):
        value = item.get(key)
        if value:
            workflow[key] = clean_text(value, 2000)
    staged = []
    for action in (item.get("staged_actions") or [])[-3:]:
        if isinstance(action, dict):
            staged.append({
                "kind": clean_text(action.get("kind"), 100),
                "staged_at": action.get("staged_at"),
                "executed_at": action.get("executed_at"),
                "body": clean_text(action.get("body"), 4000),
            })
    if staged:
        workflow["staged_actions"] = staged
    records = item.get("review_records")
    if isinstance(records, list):
        workflow["review_records"] = [{
            key: record.get(key) for key in (
                "v", "id", "head_oid", "verdict", "body", "recorded_at",
                "posted_at", "claims", "risk_areas", "test_evidence",
                "review_basis", "integrity",
            ) if record.get(key) is not None
        } for record in records[-10:] if isinstance(record, dict)]
    if workflow:
        payload["workflow"] = workflow
    return {
        "v": SCHEMA_VERSION,
        "id": signal_id("item_observed", f"{full}:{ref}", payload),
        "ts": observed_at,
        "kind": "item_observed",
        "repo": repo_value(full),
        **payload,
        "relationships": [{"type": "belongs_to", "target": f"repo:{full}"}],
    }


def audit_signal(event: dict, full_by_short: dict[str, str], observed_at: str) -> dict | None:
    short = event.get("repo")
    if not short:
        return None
    full = full_by_short.get(short, short)
    ref = event.get("ref")
    subject_id = f"repo:{full}/{ref}" if ref else f"repo:{full}"
    payload = {
        "subject": {"id": subject_id, "kind": "item" if ref else "repository", "ref": ref},
        "source": {"system": "repo-steward-audit", "event": event.get("event"),
                   "occurred_at": event.get("ts")},
        "attributes": {
            key: event[key] for key in ("actor", "via", "event", "kind", "ok", "summary", "data")
            if event.get(key) is not None
        },
        "evidence": [],
    }
    source_key = canonical({
        "ts": event.get("ts"), "event": event.get("event"), "repo": short,
        "ref": ref, "summary": event.get("summary"), "data": event.get("data"),
    })
    relationships = [{"type": "belongs_to", "target": f"repo:{full}"}]
    if ref:
        relationships.append({"type": "describes", "target": subject_id})
    return {
        "v": SCHEMA_VERSION,
        "id": signal_id("steward_event", source_key, payload),
        "ts": observed_at,
        "kind": "steward_event",
        "repo": repo_value(full),
        **payload,
        "relationships": relationships,
    }


def metric_signal(metric: dict, full_by_short: dict[str, str], observed_at: str) -> dict | None:
    short = metric.get("repo")
    if not short:
        return None
    full = full_by_short.get(short, short)
    attributes = {key: value for key, value in metric.items() if key not in {"repo", "ts"}}
    payload = {
        "subject": {"id": f"repo:{full}", "kind": "repository"},
        "source": {"system": "repo-steward-metrics", "observed_at": metric.get("ts")},
        "attributes": attributes,
        "evidence": [],
    }
    return {
        "v": SCHEMA_VERSION,
        "id": signal_id("repository_metric", canonical(metric), payload),
        "ts": observed_at,
        "kind": "repository_metric",
        "repo": repo_value(full),
        **payload,
        "relationships": [{"type": "describes", "target": f"repo:{full}"}],
    }


def collect(root: Path = ROOT, observed_at: str | None = None) -> dict:
    observed_at = observed_at or now_ts()
    repos = repo_entries(root / "config.yaml")
    full_by_short = {entry["short"]: entry["name"] for entry in repos}
    output = root / "signals.jsonl"
    existing = {record.get("id") for record in read_jsonl(output) if record.get("id")}
    candidates = []

    for entry in repos:
        ledger_path = root / "state" / f"{entry['short']}.json"
        if not ledger_path.exists():
            continue
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for ref, item in sorted((ledger.get("items") or {}).items()):
            if isinstance(item, dict):
                candidates.append(item_signal(entry["name"], ref, item, observed_at))

    for event in read_jsonl(root / "audit.jsonl"):
        signal = audit_signal(event, full_by_short, observed_at)
        if signal:
            candidates.append(signal)
    for metric in read_jsonl(root / "metrics.jsonl"):
        signal = metric_signal(metric, full_by_short, observed_at)
        if signal:
            candidates.append(signal)

    added = []
    for signal in candidates:
        if signal["id"] not in existing:
            existing.add(signal["id"])
            added.append(signal)
    if added:
        with output.open("a", encoding="utf-8") as handle:
            for signal in added:
                handle.write(json.dumps(signal, ensure_ascii=False, separators=(",", ":")) + "\n")
    elif not output.exists():
        output.touch()
    counts = {}
    for signal in added:
        counts[signal["kind"]] = counts.get(signal["kind"], 0) + 1
    return {"added": len(added), "by_kind": counts, "path": str(output)}


def query(path: Path, *, limit: int = 500, repo: str | None = None,
          kind: str | None = None) -> list[dict]:
    records = read_jsonl(path)
    if repo:
        records = [record for record in records
                   if repo in {record.get("repo", {}).get("name"),
                               record.get("repo", {}).get("short"),
                               record.get("repo", {}).get("id")}]
    if kind:
        records = [record for record in records if record.get("kind") == kind]
    return records[-max(0, limit):]


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.command == "collect":
        print(json.dumps(collect(args.root), separators=(",", ":")))


if __name__ == "__main__":
    main()
