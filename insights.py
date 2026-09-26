#!/usr/bin/env python3
"""Prepare, validate and publish the build-candidate sweep.

The sweep answers one question: across the configured repositories, which
pieces of work are most worth building next, and what would it take? Each
candidate is a *theme* grounded in one or more open issues (a single
well-specified request can rank first), carrying a build brief the maintainer
can hand straight to `build.sh` with one click.

  prepare  read-only GitHub fetch (gh, no model) -> insights-input.json
  publish  validate insights.candidate.json against that snapshot -> insights.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import server

ROOT = Path(__file__).resolve().parent
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KINDS = {"feature", "fix", "improvement", "docs", "maintenance"}
READINESS = {"ready", "needs-design", "blocked"}
EFFORT = {"tiny", "small", "medium", "large"}
BUILD_COST = {"low", "medium", "high"}
MAX_THEMES, MAX_PER_REPO = 12, 4
# Full threads are fetched for the strongest candidates only; the rest travel
# as one-line listings so the model can still group them into a theme.
THREADS_PER_REPO, THREADS_TOTAL = 8, 50
LISTED_PER_REPO = 60
BODY_CHARS, COMMENT_CHARS, COMMENTS_KEPT = 3000, 800, 10
POSITIVE_LABELS = {"enhancement", "feature", "feature request", "bug", "help wanted",
                   "good first issue", "performance", "ux"}
NEGATIVE_LABELS = {"wontfix", "won't fix", "invalid", "duplicate", "question", "blocked",
                   "needs-info", "stale"}

REPO_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    stargazerCount
    description
    issues(states: OPEN, first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title createdAt updatedAt
        author { login }
        labels(first: 10) { nodes { name } }
        comments { totalCount }
        reactions { totalCount }
        participants { totalCount }
      }
    }
    pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title isDraft updatedAt
        author { login }
        closingIssuesReferences(first: 5) { nodes { number } }
      }
    }
  }
}
"""


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


def gh_json(args: list[str], attempts: int = 3):
    for attempt in range(attempts):
        p = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return json.loads(p.stdout)
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(p.stderr.strip()[:300] or "gh failed")


def days_since(ts: str, now: float) -> float:
    try:
        return (now - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))) / 86400
    except (TypeError, ValueError):
        return 9999.0


def score_issue(issue: dict, priority: str, now: float) -> float:
    """Cheap pre-ranking that decides which threads the model reads in full.
    It is not the ranking itself: the model weighs value and readiness."""
    labels = {name.lower() for name in issue["labels"]}
    if labels & NEGATIVE_LABELS:
        return -1.0
    age = days_since(issue["updated_at"], now)
    score = (2.0 * issue["reactions"] + 1.5 * issue["participants"]
             + 0.3 * min(issue["comments"], 20))
    score += 2.0 if labels & POSITIVE_LABELS else 0.0
    score += 3.0 if age <= 30 else 1.0 if age <= 90 else 0.0
    score += 2.0 if priority == "high" else 0.0
    return round(score, 2)


def fetch_thread(full: str, number: int) -> dict:
    data = gh_json(["issue", "view", str(number), "-R", full, "--json", "body,comments"])
    comments = data.get("comments") or []
    return {
        "body": (data.get("body") or "")[:BODY_CHARS],
        "comments": [{"author": (c.get("author") or {}).get("login"),
                      "at": c.get("createdAt"),
                      "text": (c.get("body") or "")[:COMMENT_CHARS]}
                     for c in comments[-COMMENTS_KEPT:]],
        "comments_omitted": max(0, len(comments) - COMMENTS_KEPT),
    }


def fetch_repo(repo: dict, now: float) -> dict:
    full = repo["name"]
    owner, name = full.split("/", 1)
    data = gh_json(["api", "graphql", "-f", f"query={REPO_QUERY}",
                    "-F", f"owner={owner}", "-F", f"name={name}"])["data"]["repository"]
    open_prs = [{"number": pr["number"], "title": pr["title"], "draft": pr["isDraft"],
                 "author": (pr.get("author") or {}).get("login"),
                 "updated_at": pr["updatedAt"],
                 "closes": [n["number"] for n in pr["closingIssuesReferences"]["nodes"]]}
                for pr in data["pullRequests"]["nodes"]]
    covered = {n: pr["number"] for pr in open_prs for n in pr["closes"]}
    issues = []
    watch = set(repo.get("watch") or ["issues", "prs", "discussions"])
    if "issues" in watch:
        for node in data["issues"]["nodes"]:
            issue = {
                "number": node["number"], "title": node["title"],
                "url": f"https://github.com/{full}/issues/{node['number']}",
                "author": (node.get("author") or {}).get("login"),
                "labels": [label["name"] for label in node["labels"]["nodes"]],
                "created_at": node["createdAt"], "updated_at": node["updatedAt"],
                "comments": node["comments"]["totalCount"],
                "reactions": node["reactions"]["totalCount"],
                "participants": node["participants"]["totalCount"],
            }
            if issue["number"] in covered:
                issue["open_pr"] = covered[issue["number"]]
            issue["prescore"] = score_issue(issue, repo.get("priority", ""), now)
            issues.append(issue)
    issues.sort(key=lambda i: i["prescore"], reverse=True)
    return {"name": full, "priority": repo.get("priority", "normal"),
            "stars": data["stargazerCount"], "description": data.get("description") or "",
            "open_issue_count": len(issues), "issues": issues[:LISTED_PER_REPO],
            "open_prs": open_prs}


def prepare(root: Path = ROOT) -> dict:
    now = time.time()
    repos, errors = [], []
    for repo in server.repos_config():
        try:
            repos.append(fetch_repo(repo, now))
        except (RuntimeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            errors.append({"repo": repo["name"], "error": str(exc)[:300]})

    # Full threads for the strongest candidates, best first across the fleet.
    ranked = sorted(((i["prescore"], r, i) for r in repos for i in r["issues"][:THREADS_PER_REPO]
                     if i["prescore"] >= 0 and "open_pr" not in i),
                    key=lambda t: t[0], reverse=True)[:THREADS_TOTAL]
    for _, repo, issue in ranked:
        try:
            issue["thread"] = fetch_thread(repo["name"], issue["number"])
        except (RuntimeError, json.JSONDecodeError) as exc:
            errors.append({"repo": repo["name"], "issue": issue["number"], "error": str(exc)[:200]})

    previous = {}
    try:
        previous = json.loads((root / "insights.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    context = {
        "v": 2,
        "prepared_at": now_ts(),
        "repositories": repos,
        "fetch_errors": errors,
        "previous_themes": [{"id": t.get("id"), "key": t.get("key"), "repo": t.get("repo"),
                             "title": t.get("title")}
                            for t in previous.get("themes", []) if isinstance(t, dict)],
        "builds": [{"theme_id": k, "status": v.get("status"), "pr_url": v.get("pr_url")}
                   for k, v in read_builds(root).get("items", {}).items()],
    }
    write_json(root / "insights-input.json", context)
    return context


def read_builds(root: Path = ROOT) -> dict:
    try:
        return json.loads((root / "builds.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"v": 1, "items": {}}


def require_text(value, path: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInsights(f"{path} must be non-empty text")
    if len(value) > limit:
        raise InvalidInsights(f"{path} exceeds {limit} characters")
    return value.strip()


def require_enum(value, allowed: set, path: str) -> str:
    if value not in allowed:
        raise InvalidInsights(f"{path} must be one of {sorted(allowed)}")
    return value


def text_list(value, path: str, *, minimum: int = 0, maximum: int = 10) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise InvalidInsights(f"{path} must be a list of {minimum}-{maximum} entries")
    return [require_text(v, f"{path}[{i}]", 400) for i, v in enumerate(value)]


def validate(candidate: dict, snapshot: dict) -> dict:
    """Mechanical checks: every cited issue exists in the snapshot the model
    read, enums are closed, and the list stays short enough to act on."""
    if not isinstance(candidate, dict) or candidate.get("v") != 2:
        raise InvalidInsights("candidate must be an object with v=2")
    repos = {r["name"]: r for r in snapshot.get("repositories", [])}
    issues = {name: {i["number"]: i for i in r["issues"]} for name, r in repos.items()}
    prs = {name: {p["number"] for p in r["open_prs"]} for name, r in repos.items()}

    pulses = {}
    for i, entry in enumerate(candidate.get("repositories") or []):
        if not isinstance(entry, dict) or entry.get("name") not in repos:
            raise InvalidInsights(f"repositories[{i}] names an unknown repository")
        pulses[entry["name"]] = require_text(entry.get("pulse"), f"repositories[{i}].pulse", 600)

    themes, keys, per_repo = [], set(), {}
    raw = candidate.get("themes")
    if not isinstance(raw, list) or len(raw) > MAX_THEMES:
        raise InvalidInsights(f"themes must be a list of at most {MAX_THEMES}")
    for i, t in enumerate(raw):
        path = f"themes[{i}]"
        if not isinstance(t, dict):
            raise InvalidInsights(f"{path} must be an object")
        repo = t.get("repo")
        if repo not in repos:
            raise InvalidInsights(f"{path}.repo {repo!r} is not a configured repository")
        per_repo[repo] = per_repo.get(repo, 0) + 1
        if per_repo[repo] > MAX_PER_REPO:
            raise InvalidInsights(f"{path}: more than {MAX_PER_REPO} themes for {repo}")
        key = t.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key) or (repo, key) in keys:
            raise InvalidInsights(f"{path}.key must be a unique kebab-case key")
        keys.add((repo, key))
        cited = t.get("issues")
        if not isinstance(cited, list) or not cited:
            raise InvalidInsights(f"{path}.issues must cite at least one open issue")
        for n in cited:
            if n not in issues[repo]:
                raise InvalidInsights(f"{path}.issues cites #{n}, which is not an open issue in the snapshot")
        cited_prs = t.get("prs") or []
        for n in cited_prs:
            if n not in prs[repo]:
                raise InvalidInsights(f"{path}.prs cites #{n}, which is not an open PR in the snapshot")
        value_score = t.get("value_score")
        if not isinstance(value_score, int) or not 1 <= value_score <= 5:
            raise InvalidInsights(f"{path}.value_score must be an integer 1-5")
        brief = t.get("brief")
        if not isinstance(brief, dict):
            raise InvalidInsights(f"{path}.brief must be an object")
        themes.append({
            "id": f"theme:{repo}:{key}", "key": key, "repo": repo, "rank": i + 1,
            "title": require_text(t.get("title"), f"{path}.title", 140),
            "kind": require_enum(t.get("kind"), KINDS, f"{path}.kind"),
            "summary": require_text(t.get("summary"), f"{path}.summary", 1200),
            "value": require_text(t.get("value"), f"{path}.value", 800),
            "value_score": value_score,
            "readiness": require_enum(t.get("readiness"), READINESS, f"{path}.readiness"),
            "readiness_note": require_text(t.get("readiness_note"), f"{path}.readiness_note", 600),
            "effort": require_enum(t.get("effort"), EFFORT, f"{path}.effort"),
            "build_cost": require_enum(t.get("build_cost"), BUILD_COST, f"{path}.build_cost"),
            "risk": require_text(t.get("risk"), f"{path}.risk", 600),
            "issues": cited, "prs": cited_prs,
            "brief": {
                "goal": require_text(brief.get("goal"), f"{path}.brief.goal", 800),
                "acceptance": text_list(brief.get("acceptance"), f"{path}.brief.acceptance",
                                        minimum=1, maximum=10),
                "likely_files": text_list(brief.get("likely_files") or [],
                                          f"{path}.brief.likely_files", maximum=15),
                "open_questions": text_list(brief.get("open_questions") or [],
                                            f"{path}.brief.open_questions", maximum=8),
            },
            # Evidence is copied from the snapshot, never from the model.
            "evidence": [{k: issues[repo][n].get(k) for k in
                          ("number", "title", "url", "reactions", "comments",
                           "participants", "updated_at", "labels")}
                         for n in cited],
        })
    return {
        "v": 2,
        "generated_at": now_ts(),
        "prepared_at": snapshot.get("prepared_at"),
        "repositories": [{"name": name, "pulse": pulses.get(name, ""),
                          "stars": r["stars"], "open_issues": r["open_issue_count"]}
                         for name, r in repos.items()],
        "themes": themes,
    }


def publish(root: Path = ROOT) -> dict:
    candidate = json.loads((root / "insights.candidate.json").read_text(encoding="utf-8"))
    snapshot = json.loads((root / "insights-input.json").read_text(encoding="utf-8"))
    graph = validate(candidate, snapshot)
    current = root / "insights.json"
    # Keep the last pattern-sweep graph once, for the record.
    try:
        if json.loads(current.read_text(encoding="utf-8")).get("v") == 1:
            archive = root / "insights.v1.json"
            if not archive.exists():
                archive.write_text(current.read_text(encoding="utf-8"), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    write_json(current, graph)
    return {"themes": len(graph["themes"]),
            "ready": sum(t["readiness"] == "ready" for t in graph["themes"])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.command == "prepare":
        context = prepare(args.root)
        print(json.dumps({"repositories": len(context["repositories"]),
                          "threads": sum("thread" in i for r in context["repositories"]
                                         for i in r["issues"]),
                          "fetch_errors": len(context["fetch_errors"])},
                         separators=(",", ":")))
    else:
        print(json.dumps(publish(args.root), separators=(",", ":")))


if __name__ == "__main__":
    main()
