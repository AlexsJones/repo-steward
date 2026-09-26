#!/usr/bin/env python3
"""The build queue behind the Insights page's Build button.

  enqueue  (server) a theme from insights.json -> status queued
  reap     (build.sh) mark builds left `building` by a dead run as failed
  claim    (build.sh) oldest queued -> building; writes builds/<slug>.json
  finish   (build.sh) reads builds/<slug>.result.json -> pr-open|blocked|failed
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

import audit

ROOT = Path(__file__).resolve().parent
ACTIVE = {"queued", "building"}
RESULT_STATUSES = {"pr-open", "blocked"}


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(theme_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", theme_id.lower()).strip("-")


@contextmanager
def queue(root: Path = ROOT):
    """Read-modify-write builds.json under an exclusive lock: the server
    enqueues while build.sh claims and finishes."""
    path = root / "builds.json"
    with open(root / ".builds.json.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"v": 1, "items": {}}
        yield data
        data["updated_at"] = now_ts()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)


def read(root: Path = ROOT) -> dict:
    try:
        return json.loads((root / "builds.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"v": 1, "items": {}}


def enqueue(theme: dict, note: str = "", root: Path = ROOT) -> tuple[bool, dict | str]:
    with queue(root) as data:
        existing = data["items"].get(theme["id"])
        if existing and existing.get("status") in ACTIVE:
            return False, f"already {existing['status']}"
        if existing and existing.get("status") == "pr-open":
            return False, f"PR already open: {existing.get('pr_url')}"
        item = {
            "theme_id": theme["id"], "repo": theme["repo"], "key": theme["key"],
            "title": theme["title"], "status": "queued", "requested_at": now_ts(),
            "attempts": (existing or {}).get("attempts", 0), "note": note[:500],
            "theme": theme,
        }
        data["items"][theme["id"]] = item
    audit.append("build_requested", "maintainer", "insights", repo=theme["repo"].split("/")[-1],
                 ref=theme["id"], summary=f"build requested: {theme['title']}",
                 data={"issues": theme.get("issues"), "note": note[:200]})
    return True, item


def reap(root: Path = ROOT) -> int:
    """build.sh holds the single-flight lock when it calls this, so anything
    still marked building belongs to a run that died."""
    reaped = 0
    with queue(root) as data:
        for item in data["items"].values():
            if item.get("status") == "building":
                item.update(status="failed", finished_at=now_ts(),
                            summary="build run was interrupted; see logs/build.log")
                reaped += 1
    return reaped


def claim(root: Path = ROOT) -> str | None:
    with queue(root) as data:
        queued = sorted((i for i in data["items"].values() if i.get("status") == "queued"),
                        key=lambda i: i.get("requested_at", ""))
        if not queued:
            return None
        item = queued[0]
        item.update(status="building", started_at=now_ts(),
                    attempts=item.get("attempts", 0) + 1)
        for key in ("finished_at", "pr_url", "summary", "blockers", "verification", "draft"):
            item.pop(key, None)
    builds = root / "builds"
    builds.mkdir(exist_ok=True)
    name = slug(item["theme_id"])
    (builds / f"{name}.result.json").unlink(missing_ok=True)
    brief = {**item["theme"], "note": item.get("note", ""),
             "branch": f"steward/build-{item['key']}",
             "workdir": f"work/builds/{item['repo'].split('/')[-1]}"}
    (builds / f"{name}.json").write_text(json.dumps(brief, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
    return item["theme_id"]


def finish(theme_id: str, rc: int, root: Path = ROOT) -> dict:
    result_path = root / "builds" / f"{slug(theme_id)}.result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result = None
    with queue(root) as data:
        item = data["items"][theme_id]
        item["finished_at"] = now_ts()
        if isinstance(result, dict) and result.get("status") in RESULT_STATUSES:
            item["status"] = result["status"]
            for key in ("pr_url", "branch", "draft", "summary", "verification", "blockers"):
                if result.get(key) is not None:
                    item[key] = result[key]
        else:
            item["status"] = "failed"
            item["summary"] = f"build session ended (rc={rc}) without a valid result file; see logs/build.log"
        final = dict(item)
    audit.append("build_done", "steward", "build", repo=final["repo"].split("/")[-1],
                 ref=theme_id, ok=final["status"] == "pr-open",
                 summary=f"build {final['status']}: {final['title']}"
                         + (f" — {final['pr_url']}" if final.get("pr_url") else ""),
                 detail=final.get("summary", ""),
                 data={"status": final["status"], "pr_url": final.get("pr_url"), "rc": rc})
    return final


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("claim")
    sub.add_parser("reap")
    done = sub.add_parser("finish")
    done.add_argument("theme_id")
    done.add_argument("rc", type=int)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.command == "reap":
        print(reap(args.root))
    elif args.command == "claim":
        theme_id = claim(args.root)
        print(f"{theme_id}\t{slug(theme_id)}" if theme_id else "")
    else:
        print(json.dumps(finish(args.theme_id, args.rc, args.root), separators=(",", ":")))


if __name__ == "__main__":
    main()
