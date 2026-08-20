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
EXTRA_CSS = r"""
  .tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:var(--panel); }
  table { border-collapse:collapse; width:100%; min-width:760px; font-size:13.5px; }
  th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); font-weight:600; padding:10px 12px; border-bottom:1px solid var(--line); background:var(--panel-2); }
  td { padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
  tbody tr:last-child td { border-bottom:0; }
  .grouprow td { background:var(--panel-2); color:var(--accent); font:600 11px ui-monospace,Menlo,monospace; text-transform:uppercase; letter-spacing:.08em; }
  .num { white-space:nowrap; font-family:ui-monospace,Menlo,monospace; }
  .title-cell { max-width:420px; }
  .posture,.author,.quiet,.note,.snapshots { color:var(--muted); font-size:12px; }
  .posture { display:block; margin-top:3px; }
  .decision { margin-bottom:10px; }
  .decision:last-child { margin-bottom:0; }
  .decision h3 { margin:0 0 7px; font-size:15px; }
  .decision p { margin:5px 0; }
  .activity { line-height:1.7; }
  .activity .snapshots { padding-bottom:10px; border-bottom:1px solid var(--line); }
  .activity ul { margin:10px 0 0; padding-left:20px; }
  details.staged { border:1px solid var(--line); border-radius:8px; background:var(--panel); margin-bottom:8px; }
  details.staged summary { cursor:pointer; padding:10px 14px; font-weight:600; }
  details.staged pre { margin:0; padding:12px 16px; border-top:1px solid var(--line); background:var(--panel-2); white-space:pre-wrap; word-break:break-word; }
  .empty { color:var(--muted); }
  .fleet .repo { cursor:pointer; }
  .fleet .repo:hover { border-color:var(--accent); }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  header h1 { flex:0 0 auto; }
"""


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


def render(root: Path = ROOT, output: Path | None = None) -> str:
    output = output or root / "dashboard.html"
    repos = config_repos(root)
    states = load_states(root, repos)
    template = (root / "dashboard-first-run.html").read_text(encoding="utf-8")
    base_css = re.search(r"<style>(.*?)</style>", template, re.S).group(1)
    current_mode = mode(root)
    tick, actions = audit_events(root)

    all_items = [(repo, key, item) for repo in repos
                 for key, item in states[repo["short"]].get("items", {}).items()]
    open_issues = sum(item.get("type") == "issue" and item.get("status") != "done"
                      for _, _, item in all_items)
    open_prs = sum(item.get("type") == "pr" and item.get("status") != "done"
                   for _, _, item in all_items)
    staged_count = sum(
        1 for _, _, item in all_items
        if item.get("status") not in {"done", "dismissed"}
        for action in (item.get("staged_actions") or [])
        if not action.get("executed_at")
    )
    ready = [(repo, key, item) for repo, key, item in all_items
             if item.get("status") == "ready-for-maintainer" and item.get("verdict") == "approve-recommend"]
    ready.sort(key=lambda row: row[2].get("approve_recommend_since") or row[2].get("last_action_at") or "")
    decisions = open_decisions(root, repos, states)
    planned = planned_items(repos, states)

    tick_label = "no completed tick"
    if tick:
        tick_label = tick.get("ts", "").replace("T", " ").replace("Z", " UTC")
    parts = ["<!doctype html>", '<html lang="en"><head>', '<meta charset="utf-8">',
             '<meta http-equiv="refresh" content="300">', '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Repo Steward</title>', '<link rel="icon" href="/assets/logo.svg" type="image/svg+xml">',
             f"<style>{base_css}{EXTRA_CSS}</style>",
             '<link rel="stylesheet" href="/assets/site-nav.css">', "</head><body><main>",
             '<header class="site-header"><h1>Repo Steward</h1><div class="statusline">',
             f'<span class="chip">{esc(current_mode)}</span>',
             f'<span class="chip">repos {len(repos)}</span>',
             f'<span class="chip">issues {open_issues}</span>', f'<span class="chip">PRs {open_prs}</span>',
             f'<span class="chip">last tick {esc(tick_label)}</span>', f'<span class="chip">staged {staged_count}</span>',
             '</div><nav class="site-nav" aria-label="Primary navigation">',
             '<a href="/dashboard.html" aria-current="page">Operations</a>',
             '<a href="/insights.html">Insights</a>',
             '<a href="/evaluation.html">Self-evaluation</a>',
             '<a href="/metrics.html">Metrics</a>',
             '<a href="/audit.html">Audit</a></nav></header>']

    parts.append(f'<section><h2>Decisions needed <span class="count">· {len(decisions)}</span></h2>')
    if decisions:
        for decision in decisions:
            attrs = f' data-repo="{esc(decision["repo"])}"'
            if decision["urls"]:
                attrs += f' data-resolve-on="{esc(",".join(decision["urls"]))}"'
            title = esc(decision["title"])
            if decision["url"]:
                title = f'<a href="{esc(decision["url"])}">{title}</a>'
            parts.append(f'<div class="card decision"{attrs}><h3>{title}</h3>'
                         f'<p><strong>Question:</strong> {esc(decision["question"])}</p>'
                         + (f'<p class="quiet"><strong>Recommendation:</strong> {esc(decision["recommendation"])}</p>' if decision["recommendation"] else "")
                         + '</div>')
    else:
        parts.append('<div class="card empty">No decisions need you.</div>')
    parts.append('</section>')

    parts.append(f'<section><h2>Ready for your final look <span class="count">· {len(ready)}</span></h2>')
    if ready:
        parts.append('<div class="tablewrap"><table><thead><tr><th>PR</th><th>Title</th><th>Posture</th></tr></thead><tbody>')
        for repo, key, item in ready:
            number = key.split("-", 1)[1]
            since = item.get("approve_recommend_since") or item.get("last_action_at") or "earlier"
            parts.append(f'<tr data-repo="{esc(repo["short"])}" data-item="{esc(key)}">'
                         f'<td class="num"><a href="{esc(item_url(repo,key,item))}">{esc(repo["short"])} #{esc(number)}</a></td>'
                         f'<td class="title-cell">{esc(item.get("title"))}</td>'
                         f'<td>✓ approved by this steward<span class="posture">{esc(since)} · unchanged head · auto-merge after grace period</span></td></tr>')
        parts.append('</tbody></table></div>')
    else:
        parts.append('<div class="card empty">No PRs are awaiting a final look.</div>')
    parts.append('</section>')

    parts.append(f'<section><h2>Repositories <span class="count">· {len(repos)}</span></h2><div class="fleet">')
    for repo in repos:
        items = states[repo["short"]].get("items", {}).values()
        issues = sum(i.get("type") == "issue" and i.get("status") != "done" for i in items)
        items = states[repo["short"]].get("items", {}).values()
        prs = sum(i.get("type") == "pr" and i.get("status") != "done" for i in items)
        parts.append(f'<div class="card repo" data-repo="{esc(repo["short"])}"><span class="name">{esc(repo["short"])}</span>'
                     f'<span class="quiet">{issues} issues · {prs} PRs</span>'
                     f'<span class="quiet"><span class="prio {esc(repo["priority"])}">{esc(repo["priority"])}</span> {esc(repo["full"])}</span></div>')
    parts.append('</div></section>')

    parts.append(f'<section><h2>Next tick <span class="count">· {len(planned)}</span></h2>')
    if planned:
        parts.append('<div class="tablewrap"><table><thead><tr><th>Item</th><th>Title</th><th>Planned action</th><th>Why</th></tr></thead><tbody>')
        grouped = defaultdict(list)
        for row in planned:
            grouped[row[3]["short"]].append(row)
        for short, rows in grouped.items():
            parts.append(f'<tr class="grouprow" data-repo="{esc(short)}"><td colspan="4">{esc(short)}</td></tr>')
            for _, _, _, repo, key, item, changed in rows:
                action, why = planned_action(key, item, changed)
                parts.append(f'<tr data-repo="{esc(short)}"><td class="num"><a href="{esc(item_url(repo,key,item))}">{esc(key)}</a></td>'
                             f'<td class="title-cell">{esc(item.get("title"))}</td><td>{esc(action)}</td><td class="quiet">{esc(why)}</td></tr>')
        parts.append('</tbody></table></div>')
    else:
        parts.append('<div class="card empty">No actionable work is queued.</div>')
    parts.append('</section>')

    staged = [(repo, key, item, action) for repo, key, item in all_items
              if item.get("status") not in {"done", "dismissed"}
              for action in (item.get("staged_actions") or [])
              if not action.get("executed_at")]
    parts.append(f'<section><h2>Staged replies <span class="count">· {len(staged)}</span></h2>')
    if staged:
        for repo, key, item, action in staged:
            parts.append(f'<details class="staged" data-repo="{esc(repo["short"])}" data-items="{esc(key)}">'
                         f'<summary><a href="{esc(item_url(repo,key,item))}">{esc(repo["short"])} {esc(key)}</a> · {esc(item.get("title"))}</summary>'
                         f'<pre>{esc(action.get("body"))}</pre></details>')
    else:
        parts.append('<div class="card empty">No replies are staged.</div>')
    parts.append('</section>')

    parts.append(f'<section><h2>Activity &amp; trends <span class="count">· {len(actions)}</span></h2><div class="card activity">'
                 f'<p class="snapshots">Latest completed tick: {esc(tick_label)} · <a href="/metrics.html">full metrics →</a></p>')
    if actions:
        parts.append(f'<p><strong>{len(actions)} recorded action(s) across {len(set(e.get("repo") for e in actions))} repositories.</strong></p><ul>')
        for event in actions[-20:]:
            parts.append(f'<li><span class="num">{esc(event.get("repo"))} {esc(event.get("ref"))}</span> · {esc(event.get("summary"))}</li>')
        parts.append('</ul>')
    else:
        parts.append('<p class="empty">No actions were recorded in the latest tick.</p>')
    parts.append('</div></section></main><script id="steward-controls" src="/steward-controls.js"></script></body></html>')

    page = "\n".join(parts) + "\n"
    output.write_text(page, encoding="utf-8")
    return page


if __name__ == "__main__":
    render()
