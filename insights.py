#!/usr/bin/env python3
"""Prepare and validate the evidence-backed repository insight graph."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import signals


ROOT = Path(__file__).resolve().parent
POSTURES = {"heating", "stable", "cooling", "insufficient-data"}
THEME_STATES = {"possible": 1, "recurring": 2, "persistent": 3}
IDEA_STATES = {"observed": 1, "emerging": 2, "proposed": 3}
CONFIDENCE = {"low", "medium", "high"}
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InvalidInsights(ValueError):
    pass


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


def compact_signal(record: dict) -> dict:
    """Keep insight context bounded while retaining IDs and source provenance."""
    out = {key: record.get(key) for key in ("id", "ts", "kind", "repo", "subject", "source")}
    out["attributes"] = record.get("attributes") or {}
    if record.get("analysis"):
        out["analysis"] = record["analysis"]
    evidence = []
    for item in (record.get("evidence") or [])[:2]:
        if not isinstance(item, dict):
            continue
        ev = {key: item.get(key) for key in ("type", "url", "label") if item.get(key)}
        if item.get("text"):
            ev["text"] = str(item["text"])[:2000]
        comments = []
        for comment in (item.get("recent_comments") or [])[-2:]:
            if isinstance(comment, dict):
                comments.append({**comment, "text": str(comment.get("text", ""))[:1000]})
        if comments:
            ev["recent_comments"] = comments
        evidence.append(ev)
    out["evidence"] = evidence
    return out


def prepare(root: Path = ROOT) -> dict:
    records = signals.read_jsonl(root / "signals.jsonl")
    current_repos = {entry["name"] for entry in signals.repo_entries(root / "config.yaml")}
    records = [record for record in records if record.get("repo", {}).get("name") in current_repos]

    latest_items = {}
    metrics_by_repo = defaultdict(list)
    events_by_repo = defaultdict(list)
    for record in records:
        repo = record.get("repo", {}).get("name")
        if record.get("kind") == "item_observed":
            subject = record.get("subject", {}).get("id")
            if subject:
                latest_items[subject] = record
        elif record.get("kind") == "repository_metric":
            metrics_by_repo[repo].append(record)
        elif record.get("kind") == "steward_event":
            events_by_repo[repo].append(record)

    selected = []
    items_by_repo = defaultdict(list)
    for record in latest_items.values():
        items_by_repo[record.get("repo", {}).get("name")].append(record)
    for repo in sorted(current_repos):
        item_rows = sorted(
            items_by_repo[repo],
            key=lambda row: (row.get("source", {}).get("updated_at") or row.get("ts") or ""),
            reverse=True,
        )[:20]
        selected.extend(item_rows)
        selected.extend(metrics_by_repo[repo][-6:])
        selected.extend(events_by_repo[repo][-10:])

    previous = {}
    previous_path = root / "insights.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    context = {
        "v": 1,
        "prepared_at": now_ts(),
        "repositories": sorted(current_repos),
        "selection": {
            "latest_items_per_repo": 20,
            "metrics_per_repo": 6,
            "events_per_repo": 10,
            "available_signals": len(records),
            "selected_signals": len(selected),
        },
        "previous_insights": previous,
        "signals": [compact_signal(record) for record in selected],
    }
    write_json(root / "insights-input.json", context)
    return context


def require_text(value, path: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInsights(f"{path} must be non-empty text")
    if len(value) > limit:
        raise InvalidInsights(f"{path} exceeds {limit} characters")
    return value.strip()


def validate(candidate: dict, signal_records: list[dict], configured_repos: set[str]) -> dict:
    if not isinstance(candidate, dict) or candidate.get("v") != 1:
        raise InvalidInsights("candidate must be an object with v=1")
    repositories = candidate.get("repositories")
    if not isinstance(repositories, list):
        raise InvalidInsights("repositories must be a list")

    known = {record.get("id"): record for record in signal_records if record.get("id")}
    seen_repo, seen_nodes = set(), set()
    output_repos = []
    for ri, repo in enumerate(repositories):
        prefix = f"repositories[{ri}]"
        name = require_text(repo.get("name"), f"{prefix}.name", 300)
        if name not in configured_repos:
            raise InvalidInsights(f"{prefix}.name is not a configured repository: {name}")
        if name in seen_repo:
            raise InvalidInsights(f"duplicate repository: {name}")
        seen_repo.add(name)
        posture = repo.get("posture")
        if posture not in POSTURES:
            raise InvalidInsights(f"{prefix}.posture must be one of {sorted(POSTURES)}")
        repo_id = f"repo:{name}"
        if len(repo.get("themes") or []) > 8:
            raise InvalidInsights(f"{prefix}.themes exceeds the canvas limit of 8")
        themes = []
        for ti, theme in enumerate(repo.get("themes") or []):
            tpath = f"{prefix}.themes[{ti}]"
            key = require_text(theme.get("key"), f"{tpath}.key", 100)
            if not KEY_RE.fullmatch(key):
                raise InvalidInsights(f"{tpath}.key must be a lowercase kebab-case key")
            node_id = f"theme:{name}:{key}"
            if node_id in seen_nodes:
                raise InvalidInsights(f"duplicate node id: {node_id}")
            seen_nodes.add(node_id)
            state = theme.get("state")
            if state not in THEME_STATES:
                raise InvalidInsights(f"{tpath}.state must be one of {sorted(THEME_STATES)}")
            confidence = theme.get("confidence")
            if confidence not in CONFIDENCE:
                raise InvalidInsights(f"{tpath}.confidence must be one of {sorted(CONFIDENCE)}")
            evidence_ids = theme.get("signal_ids")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                raise InvalidInsights(f"{tpath}.signal_ids must be a non-empty list")
            evidence = []
            subjects = set()
            for signal_id in dict.fromkeys(evidence_ids):
                record = known.get(signal_id)
                if not record:
                    raise InvalidInsights(f"{tpath} cites unknown signal {signal_id}")
                if record.get("repo", {}).get("name") != name:
                    raise InvalidInsights(f"{tpath} cites evidence from another repository")
                evidence.append(signal_id)
                subject = record.get("subject", {}).get("id")
                # Item observations and ref-scoped audit events both provide
                # independent item evidence. Repository metrics do not: all
                # of them share the repository node and cannot manufacture
                # recurrence through repeated samples.
                if subject and subject != repo_id:
                    subjects.add(subject)
            if len(subjects) < THEME_STATES[state]:
                raise InvalidInsights(
                    f"{tpath} state {state} requires {THEME_STATES[state]} distinct item(s); got {len(subjects)}")

            if len(theme.get("ideas") or []) > 3:
                raise InvalidInsights(f"{tpath}.ideas exceeds the canvas limit of 3")
            ideas = []
            for ii, idea in enumerate(theme.get("ideas") or []):
                ipath = f"{tpath}.ideas[{ii}]"
                idea_key = require_text(idea.get("key"), f"{ipath}.key", 100)
                if not KEY_RE.fullmatch(idea_key):
                    raise InvalidInsights(f"{ipath}.key must be a lowercase kebab-case key")
                idea_id = f"idea:{name}:{idea_key}"
                if idea_id in seen_nodes:
                    raise InvalidInsights(f"duplicate node id: {idea_id}")
                seen_nodes.add(idea_id)
                idea_state = idea.get("state")
                if idea_state not in IDEA_STATES:
                    raise InvalidInsights(f"{ipath}.state must be one of {sorted(IDEA_STATES)}")
                idea_signal_ids = idea.get("signal_ids")
                if not isinstance(idea_signal_ids, list) or not idea_signal_ids:
                    raise InvalidInsights(f"{ipath}.signal_ids must be a non-empty list")
                idea_subjects = set()
                clean_idea_ids = []
                for signal_id in dict.fromkeys(idea_signal_ids):
                    record = known.get(signal_id)
                    if not record:
                        raise InvalidInsights(f"{ipath} cites unknown signal {signal_id}")
                    if record.get("repo", {}).get("name") != name:
                        raise InvalidInsights(f"{ipath} cites evidence from another repository")
                    clean_idea_ids.append(signal_id)
                    subject = record.get("subject", {}).get("id")
                    if subject and subject != repo_id:
                        idea_subjects.add(subject)
                if len(idea_subjects) < IDEA_STATES[idea_state]:
                    raise InvalidInsights(
                        f"{ipath} state {idea_state} requires {IDEA_STATES[idea_state]} distinct item(s); got {len(idea_subjects)}")
                ideas.append({
                    "id": idea_id, "key": idea_key,
                    "title": require_text(idea.get("title"), f"{ipath}.title", 300),
                    "problem": require_text(idea.get("problem"), f"{ipath}.problem"),
                    "state": idea_state,
                    "rationale": require_text(idea.get("rationale"), f"{ipath}.rationale"),
                    "scope": require_text(idea.get("scope"), f"{ipath}.scope", 1000),
                    "risk": require_text(idea.get("risk"), f"{ipath}.risk", 1000),
                    "suggested_next_action": require_text(
                        idea.get("suggested_next_action"), f"{ipath}.suggested_next_action", 1000),
                    "signal_ids": clean_idea_ids,
                    "relationships": [
                        {"type": "belongs_to", "target": repo_id},
                        {"type": "responds_to", "target": node_id},
                    ],
                })
            themes.append({
                "id": node_id, "key": key,
                "title": require_text(theme.get("title"), f"{tpath}.title", 300),
                "summary": require_text(theme.get("summary"), f"{tpath}.summary"),
                "state": state, "confidence": confidence,
                "momentum": require_text(theme.get("momentum"), f"{tpath}.momentum", 500),
                "signal_ids": evidence,
                "distinct_items": len(subjects),
                "relationships": [{"type": "belongs_to", "target": repo_id}],
                "ideas": ideas,
            })
        output_repos.append({
            "id": repo_id, "name": name, "posture": posture,
            "summary": require_text(repo.get("summary"), f"{prefix}.summary"),
            "themes": themes,
        })
    return {
        "v": 1,
        "generated_at": candidate.get("generated_at") or now_ts(),
        "repositories": output_repos,
    }


def publish(root: Path = ROOT) -> dict:
    candidate_path = root / "insights.candidate.json"
    if not candidate_path.exists():
        raise InvalidInsights("insights.candidate.json was not produced")
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidInsights(f"candidate is not valid JSON: {exc}") from exc
    records = signals.read_jsonl(root / "signals.jsonl")
    repos = {entry["name"] for entry in signals.repo_entries(root / "config.yaml")}
    result = validate(candidate, records, repos)
    write_json(root / "insights.json", result)
    return {
        "repositories": len(result["repositories"]),
        "themes": sum(len(repo["themes"]) for repo in result["repositories"]),
        "ideas": sum(len(theme["ideas"]) for repo in result["repositories"] for theme in repo["themes"]),
        "path": str(root / "insights.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "publish"):
        child = sub.add_parser(command)
        child.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = prepare(args.root) if args.command == "prepare" else publish(args.root)
    except InvalidInsights as exc:
        parser.error(str(exc))
    if args.command == "prepare":
        result = result["selection"]
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
