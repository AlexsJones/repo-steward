#!/usr/bin/env python3
"""Prepare and validate Repo Steward's evidence-backed self-evaluation."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import insights
import signals


ROOT = Path(__file__).resolve().parent
POSTURES = {"improving", "stable", "needs-attention", "insufficient-data"}
ASSESSMENTS = {"supported", "mixed", "contradicted", "inconclusive"}
CONFIDENCE = {"low", "medium", "high"}
CATEGORIES = {"issue-triage", "discussion-response", "pr-review", "escalation",
              "insight-analysis", "proactive-work", "calibration", "process"}
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InvalidEvaluation(ValueError):
    pass


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def compact_evaluation_signal(row: dict) -> dict:
    out = insights.compact_signal(row)
    workflow = row.get("workflow") or {}
    if workflow:
        compact = {key: str(workflow[key])[:700]
                   for key in ("last_action", "last_action_at", "notes", "approve_recommend_since")
                   if workflow.get(key)}
        staged = workflow.get("staged_actions") or []
        if staged:
            action = staged[-1]
            compact["latest_staged_action"] = {
                "kind": action.get("kind"), "staged_at": action.get("staged_at"),
                "executed_at": action.get("executed_at"),
                "body": str(action.get("body") or "")[:1500],
            }
        out["workflow"] = compact
    return out


def prepare(root: Path = ROOT) -> dict:
    records = signals.read_jsonl(root / "signals.jsonl")
    configured = {entry["name"] for entry in signals.repo_entries(root / "config.yaml")}
    records = [row for row in records if row.get("repo", {}).get("name") in configured]
    item_versions = defaultdict(list)
    events = []
    for row in records:
        if row.get("kind") == "item_observed" and row.get("subject", {}).get("id"):
            item_versions[row["subject"]["id"]].append(row)
        elif row.get("kind") == "steward_event":
            events.append(row)
    outcome_events, routine_events = [], []
    for row in events:
        attributes = row.get("attributes") or {}
        if (attributes.get("event") in {"approve", "dismiss", "decision_executed", "terminal",
                                        "insight_decision"}
                or attributes.get("kind") in {"observed", "merged"}):
            outcome_events.append(row)
        else:
            routine_events.append(row)
    selected_events = (outcome_events[-80:] + routine_events[-40:])[-120:]
    relevant_subjects = {row.get("subject", {}).get("id") for row in selected_events}
    selected_items = []
    for subject in relevant_subjects:
        versions = item_versions.get(subject) or []
        versions.sort(key=lambda row: row.get("ts", ""))
        if versions:
            selected_items.append(versions[0])
            if versions[-1]["id"] != versions[0]["id"]:
                selected_items.append(versions[-1])
    selected_items = selected_items[-100:]
    selected = selected_items + selected_events
    context = {
        "v": 1,
        "prepared_at": now_ts(),
        "repositories": sorted(configured),
        "selection": {"available_signals": len(records), "selected_signals": len(selected),
                      "item_histories": len(item_versions), "steward_events": len(events)},
        "previous_evaluation": read_json(root / "evaluation.json", {}),
        "current_lessons": read_json(root / "lessons.json", {}),
        "signals": [compact_evaluation_signal(row) for row in selected],
    }
    insights.write_json(root / "evaluation-input.json", context)
    return context


def text(value, path: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidEvaluation(f"{path} must be non-empty text")
    if len(value) > limit:
        raise InvalidEvaluation(f"{path} exceeds {limit} characters")
    return value.strip()


def cited(ids, known, repo, path, minimum=1) -> list[str]:
    if not isinstance(ids, list):
        raise InvalidEvaluation(f"{path} must be a list")
    clean = list(dict.fromkeys(ids))
    if len(clean) < minimum:
        raise InvalidEvaluation(f"{path} requires at least {minimum} distinct signal(s)")
    for signal_id in clean:
        row = known.get(signal_id)
        if not row:
            raise InvalidEvaluation(f"{path} cites unknown signal {signal_id}")
        if repo != "_portfolio" and row.get("repo", {}).get("name") != repo:
            raise InvalidEvaluation(f"{path} cites evidence from another repository")
    return clean


def validate(candidate: dict, records: list[dict], configured: set[str]) -> tuple[dict, dict]:
    if not isinstance(candidate, dict) or candidate.get("v") != 1:
        raise InvalidEvaluation("candidate must be an object with v=1")
    posture = candidate.get("posture")
    if posture not in POSTURES:
        raise InvalidEvaluation(f"posture must be one of {sorted(POSTURES)}")
    known = {row.get("id"): row for row in records if row.get("id")}
    dimensions = []
    for index, dimension in enumerate(candidate.get("dimensions") or []):
        path = f"dimensions[{index}]"
        key = text(dimension.get("key"), f"{path}.key", 100)
        if not KEY_RE.fullmatch(key):
            raise InvalidEvaluation(f"{path}.key must be lowercase kebab-case")
        status = dimension.get("status")
        if status not in POSTURES:
            raise InvalidEvaluation(f"{path}.status must be one of {sorted(POSTURES)}")
        dimensions.append({"key": key, "title": text(dimension.get("title"), f"{path}.title", 200),
                           "status": status, "summary": text(dimension.get("summary"), f"{path}.summary"),
                           "signal_ids": cited(dimension.get("signal_ids"), known, "_portfolio",
                                               f"{path}.signal_ids")})
    if len(dimensions) > 8:
        raise InvalidEvaluation("dimensions exceeds 8")

    findings, finding_keys = [], set()
    raw_findings = candidate.get("findings") or []
    if len(raw_findings) > 20:
        raise InvalidEvaluation("findings exceeds 20")
    for index, finding in enumerate(raw_findings):
        path = f"findings[{index}]"
        key = text(finding.get("key"), f"{path}.key", 100)
        if not KEY_RE.fullmatch(key) or key in finding_keys:
            raise InvalidEvaluation(f"{path}.key must be unique lowercase kebab-case")
        finding_keys.add(key)
        repo = text(finding.get("repo"), f"{path}.repo", 300)
        if repo != "_portfolio" and repo not in configured:
            raise InvalidEvaluation(f"{path}.repo is not configured")
        category, assessment, confidence = (finding.get("category"), finding.get("assessment"),
                                            finding.get("confidence"))
        if category not in CATEGORIES:
            raise InvalidEvaluation(f"{path}.category must be one of {sorted(CATEGORIES)}")
        if assessment not in ASSESSMENTS:
            raise InvalidEvaluation(f"{path}.assessment must be one of {sorted(ASSESSMENTS)}")
        if confidence not in CONFIDENCE:
            raise InvalidEvaluation(f"{path}.confidence must be one of {sorted(CONFIDENCE)}")
        ids = cited(finding.get("signal_ids"), known, repo, f"{path}.signal_ids", 2)
        if not any(known[sid].get("kind") == "steward_event" for sid in ids):
            raise InvalidEvaluation(f"{path} must cite at least one steward event")
        findings.append({
            "id": f"evaluation:{repo}:{key}", "key": key, "repo": repo,
            "category": category, "assessment": assessment, "confidence": confidence,
            "title": text(finding.get("title"), f"{path}.title", 300),
            "original_judgment": text(finding.get("original_judgment"), f"{path}.original_judgment"),
            "observed_outcome": text(finding.get("observed_outcome"), f"{path}.observed_outcome"),
            "critique": text(finding.get("critique"), f"{path}.critique"),
            "recommendation": text(finding.get("recommendation"), f"{path}.recommendation"),
            "signal_ids": ids,
        })

    lessons = []
    raw_lessons = candidate.get("lessons") or []
    if len(raw_lessons) > 12:
        raise InvalidEvaluation("lessons exceeds 12")
    for index, lesson in enumerate(raw_lessons):
        path = f"lessons[{index}]"
        key = text(lesson.get("key"), f"{path}.key", 100)
        if not KEY_RE.fullmatch(key):
            raise InvalidEvaluation(f"{path}.key must be lowercase kebab-case")
        repo = text(lesson.get("repo"), f"{path}.repo", 300)
        if repo != "_portfolio" and repo not in configured:
            raise InvalidEvaluation(f"{path}.repo is not configured")
        sources = lesson.get("finding_keys")
        if not isinstance(sources, list) or not sources or not set(sources) <= finding_keys:
            raise InvalidEvaluation(f"{path}.finding_keys must cite current findings")
        confidence = lesson.get("confidence")
        if confidence not in CONFIDENCE:
            raise InvalidEvaluation(f"{path}.confidence must be one of {sorted(CONFIDENCE)}")
        lessons.append({"id": f"lesson:{repo}:{key}", "key": key, "repo": repo,
                        "guidance": text(lesson.get("guidance"), f"{path}.guidance", 1000),
                        "applies_to": text(lesson.get("applies_to"), f"{path}.applies_to", 500),
                        "confidence": confidence, "finding_keys": list(dict.fromkeys(sources))})
    report = {"v": 1, "generated_at": candidate.get("generated_at") or now_ts(),
              "posture": posture, "summary": text(candidate.get("summary"), "summary"),
              "dimensions": dimensions, "findings": findings, "lessons": lessons}
    lesson_doc = {"v": 1, "generated_at": report["generated_at"], "lessons": lessons}
    return report, lesson_doc


def publish(root: Path = ROOT) -> dict:
    candidate = read_json(root / "evaluation.candidate.json", None)
    if candidate is None:
        raise InvalidEvaluation("evaluation.candidate.json was not produced")
    records = signals.read_jsonl(root / "signals.jsonl")
    configured = {entry["name"] for entry in signals.repo_entries(root / "config.yaml")}
    report, lesson_doc = validate(candidate, records, configured)
    insights.write_json(root / "evaluation.json", report)
    insights.write_json(root / "lessons.json", lesson_doc)
    with (root / "evaluations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"findings": len(report["findings"]), "lessons": len(report["lessons"]),
            "posture": report["posture"], "path": str(root / "evaluation.json")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = prepare(args.root) if args.command == "prepare" else publish(args.root)
    except (InvalidEvaluation, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if args.command == "prepare":
        result = result["selection"]
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
