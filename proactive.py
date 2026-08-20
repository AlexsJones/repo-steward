#!/usr/bin/env python3
"""Materialize selected insight ideas into a durable proactive-work queue."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TERMINAL = {"done", "dismissed"}


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


def read_json(path: Path, default, *, strict: bool = False):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if strict:
            raise
        return default


def latest_decisions(path: Path) -> dict[str, dict]:
    latest = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("idea_id"):
            latest[entry["idea_id"]] = entry
    return latest


def idea_index(graph: dict) -> dict[str, dict]:
    out = {}
    for repo in graph.get("repositories", []):
        for theme in repo.get("themes", []):
            for idea in theme.get("ideas", []):
                out[idea["id"]] = {
                    "repo": repo["name"], "theme_id": theme["id"],
                    "theme": theme["title"], **idea,
                }
    return out


def sync(root: Path = ROOT, now: str | None = None) -> dict:
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    graph = read_json(root / "insights.json", {"repositories": []}, strict=True)
    ideas = idea_index(graph)
    decisions = latest_decisions(root / "insight-decisions.jsonl")
    path = root / "proactive.json"
    queue = read_json(path, {"v": 1, "updated_at": now, "items": {}}, strict=True)
    items = queue.setdefault("items", {})
    changed = 0

    for idea_id, decision in decisions.items():
        action = decision.get("action")
        existing = items.get(idea_id)
        idea = ideas.get(idea_id)
        if action in {"select", "nominate"} and idea:
            target_status = "nominated" if action == "nominate" else "selected"
            if not existing:
                existing = {
                    "id": idea_id, "status": target_status, "selected_at": decision.get("ts"),
                    "last_control_at": decision.get("ts"), "attempts": 0,
                }
                items[idea_id] = existing
                changed += 1
            elif str(existing.get("last_control_at") or "") < str(decision.get("ts") or ""):
                if existing.get("status") not in {"in-progress", "pr-open", "done"}:
                    existing["status"] = target_status
                existing["last_control_at"] = decision.get("ts")
                changed += 1
            for key in ("repo", "theme_id", "theme", "title", "problem", "rationale",
                        "scope", "risk", "suggested_next_action", "signal_ids"):
                existing[key] = idea.get(key)
            existing["selection_note"] = decision.get("note", "")
            existing["execution_intent"] = "implement" if action == "nominate" else "explore"
            if action == "nominate":
                existing["nominated_at"] = decision.get("ts")
        elif existing and str(existing.get("last_control_at") or "") < str(decision.get("ts") or ""):
            if existing.get("status") not in TERMINAL or action == "dismiss":
                existing["status"] = {"defer": "deferred", "dismiss": "dismissed",
                                      "reset": "unselected"}.get(action, existing.get("status"))
            existing["last_control_at"] = decision.get("ts")
            existing["control_note"] = decision.get("note", "")
            changed += 1

    for idea_id, item in items.items():
        if idea_id not in ideas and item.get("status") in {"selected", "nominated"}:
            item["status"] = "superseded"
            item["notes"] = "idea is absent from the latest validated insight graph"
            changed += 1

    queue["v"] = 1
    queue["updated_at"] = now
    write_json(path, queue)
    return {"items": len(items),
            "selected": sum(item.get("status") == "selected" for item in items.values()),
            "nominated": sum(item.get("status") == "nominated" for item in items.values()),
            "changed": changed, "path": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sync"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(sync(args.root), separators=(",", ":")))


if __name__ == "__main__":
    main()
