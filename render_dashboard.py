#!/usr/bin/env python3
"""Render the dashboard deterministically from steward state.

Agent ticks update ledgers and activity. They do not get to synthesize the UI:
that made malformed/nested documents and literal shell expressions user-facing.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def config_repos(root: Path) -> list[dict]:
    text = (root / "config.yaml").read_text(encoding="utf-8")
    match = re.search(r"^repos:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    repos = []
    if not match:
        return repos
    for block in re.split(r"^(?=\s*-\s*name:)", match.group(1), flags=re.M):
        name = re.search(r"-\s*name:\s*(\S+/\S+)", block)
        if not name:
            continue
        priority = re.search(r"^\s*priority:\s*(\w+)", block, re.M)
        full = name.group(1)
        repos.append({"full": full, "short": full.split("/")[-1],
                      "priority": priority.group(1) if priority else "medium"})
    return repos


def mode(root: Path) -> str:
    match = re.search(r"^mode:\s*(\w+)", (root / "config.yaml").read_text(), re.M)
    return match.group(1) if match else "draft"


def load_states(root: Path, repos: list[dict]) -> dict[str, dict]:
    states = {}
    for repo in repos:
        path = root / "state" / f"{repo['short']}.json"
        states[repo["short"]] = json.loads(path.read_text()) if path.exists() else {"items": {}}
    return states


def item_url(repo: dict, key: str, item: dict) -> str:
    if item.get("url"):
        return item["url"]
    kind, _, number = key.partition("-")
    segment = {"pr": "pull", "issue": "issues", "disc": "discussions"}.get(kind, kind)
    return f"https://github.com/{repo['full']}/{segment}/{number}"


def open_decisions(root: Path, repos: list[dict], states: dict[str, dict]) -> list[dict]:
    path = root / "escalations.md"
    if not path.exists():
        return []
    sections = re.split(r"(?=^## )", path.read_text(encoding="utf-8"), flags=re.M)
    wanted = []
    for repo in repos:
        for key, item in states[repo["short"]].get("items", {}).items():
            if item.get("status") == "escalated":
                wanted.append((repo, key.split("-", 1)[-1], item))
    out = []
    for section in sections:
        heading = re.match(r"^## (.+)", section)
        if not heading or "✅ RESOLVED" in heading.group(1):
            continue
        match_item = next((entry for entry in wanted
                           if f"#{entry[1]}" in heading.group(1)
                           and (entry[0]["short"] in heading.group(1)
                                or entry[0]["full"] in section)), None)
        if not match_item:
            continue
        urls = re.findall(r"https://github\.com/[^\s)>]+", section)
        full = match_item[0]["full"]
        short = match_item[0]["short"]
        question = re.search(r"\*\*Question:\*\*\s*(.+?)(?=\n\n|\*\*Recommendation|$)", section, re.S)
        recommendation = re.search(r"\*\*Recommendation:\*\*\s*(.+?)(?=\n\n|$)", section, re.S)
        out.append({"title": heading.group(1), "repo": short, "full": full,
                    "url": urls[0] if urls else "", "urls": urls,
                    "question": re.sub(r"\s+", " ", question.group(1)).strip() if question else "Decision required.",
                    "recommendation": re.sub(r"\s+", " ", recommendation.group(1)).strip() if recommendation else ""})
    return out


def audit_events(root: Path) -> tuple[dict | None, list[dict]]:
    path = root / "audit.jsonl"
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    tick = next((e for e in reversed(events) if e.get("event") == "tick_done" and e.get("via") == "tick"), None)
    if not tick:
        return None, []
    start = tick.get("ts", "")
    actions = [e for e in events if e.get("event") == "steward_action" and e.get("via") == "tick"
               and e.get("ts", "") >= start]
    unique = []
    seen = set()
    for event in actions:
        sig = (event.get("ts"), event.get("repo"), event.get("ref"), event.get("summary"))
        if sig not in seen:
            seen.add(sig); unique.append(event)
    return tick, unique


def planned_items(repos: list[dict], states: dict[str, dict], limit=20) -> list[tuple]:
    priority = {"high": 0, "medium": 1, "low": 2}
    candidates = []
    for repo in repos:
        for key, item in states[repo["short"]].get("items", {}).items():
            status = item.get("status", "backlog")
            if status in {"done", "dismissed", "ready-for-maintainer", "escalated"}:
                continue
            last_activity = item.get("last_activity_at") or item.get("github_updated_at") or item.get("created_at") or ""
            last_action = item.get("last_action_at") or ""
            changed = bool(last_action and last_activity > last_action)
            if status != "backlog" and not changed and status not in {"iterating", "fix-in-flight", "posted"}:
                continue
            candidates.append((0 if changed else 1, priority.get(repo["priority"], 1), last_activity,
                               repo, key, item, changed))
    return sorted(candidates, key=lambda row: row[:3])[:limit]


def planned_action(key: str, item: dict, changed: bool) -> tuple[str, str]:
    if changed:
        return ("Read the new reply/push and continue the conversation", "New activity after the steward's last action")
    if key.startswith("issue-"):
        return ("Triage and reply", "Oldest unanswered in-scope issue")
    if key.startswith("disc-"):
        return ("Read and reply where useful", "Unanswered in-scope discussion")
    if item.get("status") == "iterating":
        return ("Delta re-review", "Contributor iteration is waiting")
    return ("Review the full diff", "Oldest never-reviewed in-scope PR")


BUILD_STATUS = {"queued": ("info", "queued"), "building": ("info", "building"),
                "pr-open": ("ok", "PR open"), "blocked": ("warn", "blocked"),
                "failed": ("crit", "failed")}


def load_builds(root: Path) -> list[dict]:
    try:
        items = json.loads((root / "builds.json").read_text(encoding="utf-8")).get("items", {})
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(items.values(), key=lambda b: b.get("requested_at") or "", reverse=True)


def day(ts: str) -> str:
    return (ts or "")[:10]


def panel(pid: str, title: str, count: int, body: str, *, attn: bool = False,
          hint: str = "", tools: str = "") -> str:
    cls = "panel attn" if attn and count else "panel"
    badge = f'<span class="count{" sig" if attn and count else ""}" data-count>{count}</span>'
    return (f'<section class="{cls}" id="{pid}" data-panel="{pid}">'
            f'<div class="panel-head" data-toggle><h2>{esc(title)}</h2>{badge}'
            + (f'<span class="hint">{esc(hint)}</span>' if hint else "")
            + (f'<span class="tools">{tools}</span>' if tools else "")
            + f'</div>{body}</section>')


def empty(text: str) -> str:
    return f'<div class="empty" data-empty>{esc(text)}</div>'


def render(root: Path = ROOT, output: Path | None = None) -> str:
    output = output or root / "dashboard.html"
    repos = config_repos(root)
    states = load_states(root, repos)
    current_mode = mode(root)
    tick, actions = audit_events(root)

    all_items = [(repo, key, item) for repo in repos
                 for key, item in states[repo["short"]].get("items", {}).items()]
    staged = [(repo, key, item, action) for repo, key, item in all_items
              if item.get("status") not in {"done", "dismissed"}
              for action in (item.get("staged_actions") or [])
              if not action.get("executed_at") and not action.get("superseded_at")]
    ready = [(repo, key, item) for repo, key, item in all_items
             if item.get("status") == "ready-for-maintainer" and item.get("verdict") == "approve-recommend"]
    ready.sort(key=lambda row: row[2].get("approve_recommend_since") or row[2].get("last_action_at") or "")
    decisions = open_decisions(root, repos, states)
    planned = planned_items(repos, states)
    builds = load_builds(root)
    open_builds = builds[:12]
    tick_ts = tick.get("ts", "") if tick else ""

    per_repo = {r["short"]: {"issues": 0, "prs": 0} for r in repos}
    for repo, _, item in all_items:
        if item.get("status") == "done":
            continue
        if item.get("type") == "issue":
            per_repo[repo["short"]]["issues"] += 1
        elif item.get("type") == "pr":
            per_repo[repo["short"]]["prs"] += 1
    open_issues = sum(v["issues"] for v in per_repo.values())
    open_prs = sum(v["prs"] for v in per_repo.values())

    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           "<title>Operations · Repo Steward</title>",
           '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/assets/logo-256.png">',
           '<link rel="stylesheet" href="/assets/steward.css">',
           "</head>", '<body data-page="operations">', "<main class=\"page\" data-ops>"]

    out.append('<div class="page-head"><h1>Operations</h1>'
               f'<span class="sub">{esc(current_mode)} mode · last tick '
               f'<span class="num" data-ts="{esc(tick_ts)}">{esc(tick_ts.replace("T", " ") or "never")}</span></span>'
               '<div class="tools"><input class="field" type="search" data-filter-input '
               'placeholder="filter rows  /" aria-label="Filter rows" style="width:220px"></div></div>')

    cells = [("Decisions", len(decisions), "decisions", bool(decisions)),
             ("Ready to merge", len(ready), "ready", False),
             ("Staged", len(staged), "staged", False),
             ("Building", sum(b.get("status") in {"queued", "building"} for b in builds), "builds", False),
             ("Next tick", len(planned), "next", False),
             ("Open issues", open_issues, None, False),
             ("Open PRs", open_prs, None, False)]
    out.append('<div class="readout">' + "".join(
        f'<div class="{"sig" if sig else ""}"><span class="label">{esc(label)}</span>'
        + (f'<b><a href="#{target}" style="text-decoration:none">{value}</a></b>' if target else f"<b>{value}</b>")
        + "</div>" for label, value, target, sig in cells) + "</div>")

    out.append('<div class="cols"><aside class="rail"><div class="panel" data-rail>'
               '<div class="panel-head"><h2>Repositories</h2>'
               f'<span class="count">{len(repos)}</span></div>'
               '<table class="grid"><thead><tr><th>Repo</th><th class="num">Iss</th><th class="num">PR</th><th class="num">Att</th></tr></thead><tbody>'
               '<tr class="sel" data-rail-repo=""><td><b>all</b></td>'
               f'<td class="num">{open_issues}</td><td class="num">{open_prs}</td><td class="num" data-att></td></tr>')
    for repo in repos:
        counts = per_repo[repo["short"]]
        prio = '<i class="pri" title="high priority"></i>' if repo["priority"] == "high" else ""
        out.append(f'<tr data-rail-repo="{esc(repo["short"])}" title="{esc(repo["full"])} · {esc(repo["priority"])} priority">'
                   f'<td class="mono">{prio}{esc(repo["short"])}</td><td class="num">{counts["issues"]}</td>'
                   f'<td class="num">{counts["prs"]}</td><td class="num" data-att></td></tr>')
    out.append("</tbody></table></div></aside><div>")

    # Decisions
    body = []
    for d in decisions:
        attrs = f' data-repo="{esc(d["repo"])}" data-nav data-row'
        if d["urls"]:
            attrs += f' data-resolve-on="{esc(",".join(d["urls"]))}"'
        title = esc(d["title"])
        if d["url"]:
            title = f'<a href="{esc(d["url"])}" target="_blank" rel="noopener">{title}</a>'
        body.append(f'<div class="decision"{attrs} style="padding:12px;border-bottom:1px solid var(--line)">'
                    f'<div style="display:flex;gap:8px;align-items:baseline;margin-bottom:6px"><span class="tag">{esc(d["repo"])}</span>'
                    f'<h3 style="font-size:13.5px" data-title>{title}</h3><span data-live></span></div>'
                    f'<dl class="kv"><dt>Question</dt><dd>{esc(d["question"])}</dd>'
                    + (f'<dt>Recommend</dt><dd>{esc(d["recommendation"])}</dd>' if d["recommendation"] else "")
                    + '</dl><div class="cmd" style="margin-top:10px"><input type="text" data-decide '
                    'placeholder="type your decision and press enter, e.g. go with #650, close #651 as superseded">'
                    '<span class="state" data-decide-state></span></div></div>')
    out.append(panel("decisions", "Decisions needed", len(decisions),
                     "".join(body) or empty("No decisions need you."), attn=True,
                     hint="type a decision; the steward carries it out"))

    # Ready
    rows = []
    for repo, key, item in ready:
        number = key.split("-", 1)[1]
        since = item.get("approve_recommend_since") or item.get("last_action_at") or ""
        rows.append(f'<tr data-repo="{esc(repo["short"])}" data-item="{esc(key)}" data-nav data-row>'
                    f'<td class="w-id"><a href="{esc(item_url(repo, key, item))}" target="_blank" rel="noopener">{esc(repo["short"])}#{esc(number)}</a></td>'
                    f'<td><span class="title">{esc(item.get("title"))}</span>'
                    f'<span class="sub">steward approved <span data-ts="{esc(since)}">{esc(day(since))}</span> · unchanged head'
                    + (f' · merge queued <span data-ts="{esc(item["merge_queued_at"])}">{esc(day(item["merge_queued_at"]))}</span>'
                       if item.get("merge_queued_at") else "") + '</span></td>'
                    '<td class="w-id" data-live><span class="muted">…</span></td>'
                    '<td class="w-act"><div class="btn-row" style="justify-content:flex-end">'
                    '<button class="btn ghost" data-act="review" title="Read the staged review">Review</button>'
                    '<button class="btn crit" data-act="dismiss" title="Drop from the queue; nothing is posted">Dismiss</button>'
                    '<button class="btn ok" data-act="merge" title="Post the staged review if needed, then merge as you">Merge</button>'
                    "</div></td></tr>")
    table = ('<div class="tablewrap"><table class="grid"><thead><tr><th>PR</th><th>Title</th><th>GitHub</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else ""
    out.append(panel("ready", "Ready for your final look", len(ready),
                     table or empty("No PRs are awaiting a final look."),
                     hint="approved by the steward; merge finishes it"))

    # Builds
    rows = []
    for b in open_builds:
        tone, label = BUILD_STATUS.get(b.get("status"), ("", b.get("status") or "?"))
        short = (b.get("repo") or "").split("/")[-1]
        pr = (f'<a href="{esc(b["pr_url"])}" target="_blank" rel="noopener">{esc(b["pr_url"].split("github.com/")[-1])}</a>'
              if b.get("pr_url") else '<span class="muted">—</span>')
        rows.append(f'<tr data-repo="{esc(short)}" data-row>'
                    f'<td class="w-id mono">{esc(short)}</td>'
                    f'<td><a class="title" href="/insights.html?theme={esc(b.get("theme_id"))}" style="text-decoration:none">{esc(b.get("title"))}</a>'
                    + (f'<span class="sub">{esc(b.get("summary"))}</span>' if b.get("summary") else "") + "</td>"
                    f'<td class="w-id"><span class="st {tone}">{esc(label)}</span></td><td class="w-id">{pr}</td>'
                    f'<td class="w-id num" data-ts="{esc(b.get("finished_at") or b.get("requested_at"))}">{esc(day(b.get("finished_at") or b.get("requested_at")))}</td></tr>')
    table = ('<div class="tablewrap"><table class="grid"><thead><tr><th>Repo</th><th>Theme</th><th>State</th><th>PR</th><th>When</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else ""
    out.append(panel("builds", "Builds", len(builds),
                     table or empty("No builds yet. Pick a theme on Insights and click Build."),
                     tools='<a class="btn ghost" href="/insights.html">Insights →</a>'))

    # Staged replies
    rows = []
    for repo, key, item, action in staged:
        kind = (action.get("kind") or "reply").replace("_", " ")
        rows.append(f'<tr data-repo="{esc(repo["short"])}" data-item="{esc(key)}" data-nav data-row data-staged>'
                    f'<td class="w-id"><a href="{esc(item_url(repo, key, item))}" target="_blank" rel="noopener">{esc(repo["short"])} {esc(key)}</a></td>'
                    f'<td><span class="title">{esc(item.get("title"))}</span></td><td class="w-id"><span class="tag">{esc(kind)}</span></td>'
                    '<td class="w-act"><div class="btn-row" style="justify-content:flex-end">'
                    '<button class="btn ghost" data-act="toggle">View</button>'
                    '<button class="btn ok" data-act="post" title="Post to GitHub under your account">Post</button></div></td></tr>'
                    f'<tr class="detail" data-detail hidden><td colspan="4"><pre>{esc(action.get("body") or ", ".join(action.get("labels") or []))}</pre></td></tr>')
    table = ('<div class="tablewrap"><table class="grid"><thead><tr><th>Item</th><th>Title</th><th>Kind</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else ""
    out.append(panel("staged", "Staged replies", len(staged), table or empty("No replies are staged.")))

    # Next tick
    rows = []
    grouped = defaultdict(list)
    for row in planned:
        grouped[row[3]["short"]].append(row)
    for short, group in grouped.items():
        rows.append(f'<tr class="group" data-group="{esc(short)}"><td colspan="4">{esc(short)}</td></tr>')
        for _, _, _, repo, key, item, changed in group:
            action, why = planned_action(key, item, changed)
            rows.append(f'<tr data-repo="{esc(short)}" data-row>'
                        f'<td class="w-id"><a href="{esc(item_url(repo, key, item))}" target="_blank" rel="noopener">{esc(key)}</a></td>'
                        f'<td><span class="title">{esc(item.get("title"))}</span></td><td>{esc(action)}</td>'
                        f'<td class="muted">{esc(why)}</td></tr>')
    table = ('<div class="tablewrap"><table class="grid"><thead><tr><th>Item</th><th>Title</th><th>Planned</th><th>Why</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else ""
    out.append(panel("next", "Next tick", len(planned), table or empty("No actionable work is queued.")))

    # Last tick activity
    rows = [f'<tr data-repo="{esc(e.get("repo"))}" data-row><td class="w-id mono">{esc(e.get("repo"))}</td>'
            f'<td class="w-id mono">{esc(e.get("ref"))}</td><td class="w-id"><span class="tag">{esc(e.get("kind"))}</span></td>'
            f'<td>{esc(e.get("summary"))}</td></tr>' for e in actions[-40:]]
    table = ('<div class="tablewrap"><table class="grid"><thead><tr><th>Repo</th><th>Ref</th><th>Kind</th><th>What happened</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else ""
    out.append(panel("activity", "Last tick", len(actions), table or empty("No actions were recorded in the latest tick."),
                     tools='<a class="btn ghost" href="/audit.html">Audit →</a><a class="btn ghost" href="/metrics.html">Metrics →</a>'))

    out.append('</div></div></main>'
               '<script src="/assets/steward-shell.js"></script>'
               '<script id="steward-controls" src="/steward-controls.js"></script></body></html>')
    page = "\n".join(out) + "\n"
    output.write_text(page, encoding="utf-8")
    return page


if __name__ == "__main__":
    render()
