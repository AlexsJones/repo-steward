#!/usr/bin/env python3
"""Repo Steward dashboard server.

Static file serving plus a minimal control API:
  GET  /api/status            -> {tick_active, mode, elapsed_sec, eta_sec, schedule,
                                  dashboard_ready}
  GET  /api/progress          -> {steps: [...]}  (per-item progress the tick emits)
  GET  /api/metrics|uptime    -> chart data
  POST /api/tick              -> start one steward tick (refused while one runs)
  POST /api/mode              -> {"mode": "draft"|"live"}  (rewrites config.yaml)
  POST /api/schedule          -> {"preset": "manual"|"hourly"|"6h"|"daily"|"weekly"}
  POST /api/limits            -> {"substantive": N, "light": N}  (per-tick work caps)
  GET  /api/watch             -> per-repo watched resources + priority
  POST /api/watch             -> {"repos": [{"name", "watch": [...], "priority"}]}
        rewrites the config.yaml repos block in place (comments survive)
  GET  /api/staged?repo=&item= -> the ledger item (staged review text, verdict)
  POST /api/approve           -> execute a staged action set via gh
        body: {"repo": "llmfit", "items": ["pr-646", ...]}
        For an approve-recommend PR this posts the review (if still unposted)
        AND merges the PR: the maintainer's click IS the terminal decision.
  POST /api/dismiss           -> mark items dismissed (drops off the queue, posts nothing)
        body: {"repo": "llmfit", "items": ["pr-646", ...]}
  POST /api/decide            -> record a typed maintainer decision; runs the
        decision executor (decide.sh) immediately when idle, else leaves it
        pending for the next tick (STEWARD.md step 0)
        body: {"repo": "llmfit", "refs": [...], "title": "...", "decision": "..."}
  GET  /api/decisions         -> recent decision entries + executor state
  GET  /api/audit?repo=&event=&limit= -> events from the decision log
        (audit.jsonl — see audit.py for the schema; every mutating endpoint
        here appends its event at the moment it acts)

Approvals run under the local gh auth — i.e. as Alex, because a human clicked.
Ledger writes are refused while a tick or the decision executor is active to
avoid racing the steward.
"""
import html as html_lib
import json
import os
import re
import subprocess
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import audit

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("STEWARD_PORT", "8377"))
HOST = os.environ.get("STEWARD_HOST", "0.0.0.0")
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"

# OnCalendar presets offered by the dashboard schedule control.
SCHEDULES = {
    "manual": (None, "Manual only"),
    "hourly": ("*-*-* *:17:00", "Hourly"),
    "6h": ("*-*-* 00,06,12,18:17:00", "Every 6 hours"),
    "daily": ("*-*-* 07:00:00", "Daily at 07:00"),
    "weekly": ("Mon *-*-* 07:00:00", "Weekly (Mon 07:00)"),
}

def first_run_page():
    """The dashboard before any tick has generated one: the real chrome and the
    real configured fleet, with the tick button and progress strip the controls
    script renders. dashboard-first-run.html is a tracked file; the repo cards
    are injected here so the filter lens sees them at load."""
    cards = []
    for r in repos_config():
        watched = ", ".join(r["watch"]) if len(r["watch"]) < len(RESOURCES) else "all resources"
        prio = r["priority"]
        cards.append(
            '<div class="card repo">'
            f'<span class="name">{html_lib.escape(r["short"])}</span>'
            f'<span class="quiet">{html_lib.escape(r["name"])}</span>'
            f'<span class="quiet"><span class="prio {prio}">{prio}</span> '
            f'watching {html_lib.escape(watched)}</span></div>')
    if not cards:
        cards.append('<div class="card repo"><span class="name">no repositories</span>'
                     '<span class="quiet">add a repos: entry to config.yaml</span></div>')
    page = (ROOT / "dashboard-first-run.html").read_text()
    return page.replace("<!--REPOS-->", "\n".join(cards))


def repo_map():
    """Short repo name -> owner/repo, parsed from config.yaml."""
    m = {}
    for line in (ROOT / "config.yaml").read_text().splitlines():
        match = re.match(r"\s*-\s*name:\s*(\S+/\S+)", line)
        if match:
            full = match.group(1)
            m[full.split("/")[1]] = full
    return m


RESOURCES = ("issues", "prs", "discussions")


def repos_config():
    """The repos: entries with name/priority/watch. watch defaults to every
    resource when the key is absent."""
    txt = (ROOT / "config.yaml").read_text()
    m = re.search(r"^repos:\s*$(.*?)(?=^\S|\Z)", txt, re.M | re.S)
    out = []
    if not m:
        return out
    for block in re.split(r"^(?=\s*-\s*name:)", m.group(1), flags=re.M):
        nm = re.search(r"-\s*name:\s*(\S+/\S+)", block)
        if not nm:
            continue
        pr = re.search(r"^\s*priority:\s*(\w+)", block, re.M)
        wt = re.search(r"^\s*watch:\s*\[([^\]]*)\]", block, re.M)
        watch = ([w.strip() for w in wt.group(1).split(",") if w.strip()]
                 if wt else list(RESOURCES))
        full = nm.group(1)
        out.append({"name": full, "short": full.split("/")[1],
                    "priority": pr.group(1) if pr else "medium", "watch": watch})
    return out


def set_watch(name, watch=None, priority=None):
    """Update one repo entry's watch/priority in config.yaml, touching only
    that entry's lines so hand-written comments survive."""
    if watch is not None:
        bad = [w for w in watch if w not in RESOURCES]
        if bad:
            return False, f"unknown resources: {', '.join(bad)}"
        if not watch:
            return False, "watch at least one resource"
    if priority is not None and priority not in ("high", "medium", "low"):
        return False, "priority must be high|medium|low"
    path = ROOT / "config.yaml"
    lines = path.read_text().splitlines(keepends=True)
    i = next((k for k, ln in enumerate(lines)
              if re.match(r"\s*-\s*name:\s*" + re.escape(name) + r"\s*(#.*)?$", ln)), None)
    if i is None:
        return False, f"{name!r} not in config"
    j = i + 1
    while j < len(lines) and not re.match(r"\s*-\s*name:|^\S", lines[j]):
        j += 1
    block = lines[i:j]
    if priority is not None:
        for k, ln in enumerate(block):
            mm = re.match(r"(\s*priority:\s*)\w+(.*)$", ln.rstrip("\n"))
            if mm:
                block[k] = mm.group(1) + priority + mm.group(2) + "\n"
                break
        else:
            block.insert(1, "    priority: " + priority + "\n")
    if watch is not None:
        wline = "    watch: [" + ", ".join(w for w in RESOURCES if w in watch) + "]\n"
        for k, ln in enumerate(block):
            if re.match(r"\s*watch:", ln):
                block[k] = wline
                break
        else:
            k = len(block)
            while k > 1 and block[k - 1].strip() == "":
                k -= 1
            block.insert(k, wline)
    path.write_text("".join(lines[:i] + block + lines[j:]))
    return True, None


def steward_mode():
    match = re.search(r"^mode:\s*(\w+)", (ROOT / "config.yaml").read_text(), re.M)
    return match.group(1) if match else "draft"


def steward_signature():
    m = re.search(r'^signature:\s*"(.*)"\s*$', (ROOT / "config.yaml").read_text(), re.M)
    if not m:
        return ""
    # YAML double-quoted: turn the \n escapes into real newlines.
    return m.group(1).replace("\\n", "\n")


def with_signature(body):
    """Ensure the posted body ends with the CURRENT config signature, so a
    change to the signature (or the project URL) takes effect immediately for
    every pending item, regardless of when it was staged."""
    sig = steward_signature()
    if not sig or not body or sig in body:
        return body
    marker = body.find("🤝")           # strip any older baked-in signature
    if marker != -1:
        body = body[:marker].rstrip()
    return body + "\n\n" + sig


def read_limits():
    txt = (ROOT / "config.yaml").read_text()

    def g(key, default):
        m = re.search(r"^\s*" + key + r":\s*(\d+)", txt, re.M)
        return int(m.group(1)) if m else default
    return {"substantive": g("substantive_items_per_tick", 8),
            "light": g("light_items_per_tick", 24)}


def set_limits(sub, light):
    try:
        sub, light = int(sub), int(light)
    except (TypeError, ValueError):
        return False, "limits must be integers"
    if not (1 <= sub <= 100 and 1 <= light <= 200):
        return False, "out of range (substantive 1-100, light 1-200)"
    txt = (ROOT / "config.yaml").read_text()
    txt, n1 = re.subn(r"^(\s*substantive_items_per_tick:\s*)\d+",
                      lambda m: m.group(1) + str(sub), txt, count=1, flags=re.M)
    txt, n2 = re.subn(r"^(\s*light_items_per_tick:\s*)\d+",
                      lambda m: m.group(1) + str(light), txt, count=1, flags=re.M)
    if not (n1 and n2):
        return False, "limits block not found in config.yaml"
    (ROOT / "config.yaml").write_text(txt)
    return True, {"substantive": sub, "light": light}


def tick_active():
    state = subprocess.run(
        ["systemctl", "--user", "is-active", "repo-steward.service"],
        capture_output=True, text=True,
    ).stdout.strip()
    return state in ("active", "activating")


# The decision executor is single-flight: one decide.sh at a time, and never
# alongside a tick — both rewrite ledgers. decide.sh maintains .decide.pid so
# a run spawned elsewhere (tick.sh pre-drains pending decisions) is seen too.
DECIDER = {"proc": None, "last_spawn": 0.0}


def decide_active():
    p = DECIDER["proc"]
    if p is not None and p.poll() is None:
        return True
    try:
        pid = int((ROOT / ".decide.pid").read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def spawn_decider():
    DECIDER["last_spawn"] = time.time()
    log = open(ROOT / "logs" / "decide.log", "a")
    DECIDER["proc"] = subprocess.Popen(
        ["bash", str(ROOT / "decide.sh")], cwd=ROOT, stdout=log, stderr=log)


def pending_decisions():
    """True if decisions.jsonl has entries the executor should act on.
    Entries carrying a `note` are excluded — that's the executor asking the
    maintainer for clarification, not work to retry."""
    p = ROOT / "decisions.jsonl"
    if not p.exists():
        return False
    for line in p.read_text().splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("status") == "pending" and not o.get("note"):
            return True
    return False


def merge_method_flag(full_repo):
    """gh pr merge flag: config.yaml `merge_method:` if set, else the first
    method the repo allows (squash > merge > rebase)."""
    m = re.search(r"^merge_method:\s*(\w+)", (ROOT / "config.yaml").read_text(), re.M)
    if m and m.group(1) in ("merge", "squash", "rebase"):
        return "--" + m.group(1)
    ok, out = run_gh(["api", f"repos/{full_repo}", "--jq",
                      '{squash:.allow_squash_merge, merge:.allow_merge_commit, rebase:.allow_rebase_merge}'])
    if ok:
        try:
            allowed = json.loads(out)
            for key in ("squash", "merge", "rebase"):
                if allowed.get(key):
                    return "--" + key
        except json.JSONDecodeError:
            pass
    return "--squash"


def merge_pr(full_repo, number):
    """Merge a PR on the maintainer's behalf — only ever called from their
    explicit dashboard approval of an approve-recommend item."""
    flag = merge_method_flag(full_repo)
    ok, out = run_gh(["pr", "merge", str(number), "-R", full_repo, flag])
    if ok:
        return True, f"merge {flag}: {(out or 'merged')[:200]}"
    retriable = ("not up to date with the base branch" in out
                 or ("Required status check" in out and "is expected" in out))
    if not retriable:
        return False, f"merge {flag}: {out[:200]}"
    # Branch protection blocks a direct merge: the head is stale against base
    # and/or required checks haven't reported for it. Refresh the head if it's
    # behind, then queue auto-merge so GitHub completes the merge once the
    # requirements are met.
    steps = []
    st_ok, st = run_gh(["pr", "view", str(number), "-R", full_repo,
                        "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"])
    if st_ok and st == "BEHIND":
        upd_ok, upd_out = run_gh(["api", "-X", "PUT",
                                  f"repos/{full_repo}/pulls/{number}/update-branch"])
        if not upd_ok:
            return False, f"merge {flag}: head behind base; update-branch failed: {upd_out[:200]}"
        steps.append("branch updated")
    ok, out = run_gh(["pr", "merge", str(number), "-R", full_repo, flag, "--auto"])
    if ok:
        return True, f"merge {flag}: " + "; ".join(steps + ["auto-merge queued"])
    return False, f"merge {flag}: " + "; ".join(steps + [f"auto-merge failed: {out[:200]}"])


def tick_elapsed_sec():
    """Seconds the running tick has been alive, or None if not running."""
    pid = subprocess.run(
        ["systemctl", "--user", "show", "repo-steward.service", "-p", "MainPID", "--value"],
        capture_output=True, text=True).stdout.strip()
    if not pid or pid == "0":
        return None
    et = subprocess.run(["ps", "-o", "etimes=", "-p", pid],
                        capture_output=True, text=True).stdout.strip()
    return int(et) if et.isdigit() else None


def eta_sec():
    """Median duration of recent measured ticks. Returns None until there are
    at least 3 samples — below that a single number is noise, not an estimate."""
    p = ROOT / "usage.jsonl"
    if not p.exists():
        return None
    durs = []
    for line in p.read_text().splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Decision-executor runs share usage.jsonl but are not ticks —
        # including them would drag the tick ETA down.
        if str(o.get("engine", "")).endswith("-decide"):
            continue
        d = o.get("duration_ms")
        if d:
            durs.append(d / 1000)
    if len(durs) < 3:
        return None
    durs.sort()
    return int(durs[len(durs) // 2])


def eta_remaining_from_timings(chunks_done):
    """Chunk-aware remaining-time estimate. timings.jsonl (written by tick.sh
    from real file mtimes) records when each chunk of past ticks completed;
    the estimate is the median time those ticks still had to run after their
    chunks_done-th chunk. None until there are 3 samples."""
    p = ROOT / "timings.jsonl"
    if not p.exists():
        return None
    rem = []
    for line in p.read_text().splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        offs = sorted(v for v in o.get("chunks", {}).values()
                      if isinstance(v, (int, float)))
        total = o.get("total_sec")
        if not offs or not isinstance(total, (int, float)):
            continue
        reached = offs[min(chunks_done, len(offs)) - 1] if chunks_done else 0
        rem.append(max(0, total - reached))
    if len(rem) < 3:
        return None
    rem.sort()
    return int(rem[len(rem) // 2])


def tick_progress():
    """Deterministic tick position, measured in chunks the tick provably
    completed: one per configured repo (ledger mtime >= tick start) plus the
    metrics and dashboard writes. The LLM's progress feed contributes only the
    free-text note — its self-reported timestamps and indices are never used."""
    el = tick_elapsed_sec()
    if el is None:
        return None
    start = time.time() - el - 5

    def touched(p):
        try:
            return p.exists() and p.stat().st_mtime >= start
        except OSError:
            return False

    repos = repo_map()
    synced = sum(touched(ROOT / "state" / f"{s}.json") for s in repos)
    metrics_done = touched(ROOT / "metrics.jsonl")
    dash_done = touched(ROOT / "dashboard.html")
    done = synced + metrics_done + dash_done
    total = len(repos) + 2
    if dash_done:
        phase = "finishing up"
    elif metrics_done:
        phase = "refreshing dashboard"
    elif repos and synced >= len(repos):
        phase = "executing work queue"
    else:
        phase = f"syncing repositories ({synced}/{len(repos)})"
    note = None
    prog = ROOT / "progress.jsonl"
    if prog.exists():
        for line in prog.read_text().splitlines():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("phase") in ("repo", "item") and o.get("msg"):
                r = o.get("repo")
                note = (r + ": " if r else "") + o["msg"]
    return {"chunks_done": done, "chunks_total": total, "phase": phase,
            "repos_done": min(synced, len(repos)), "repos_total": len(repos),
            "eta_remaining_sec": eta_remaining_from_timings(done), "note": note}


def current_schedule():
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "repo-steward.timer"],
        capture_output=True, text=True).stdout.strip() == "active"
    oncal, preset = None, "manual"
    tf = UNIT_DIR / "repo-steward.timer"
    if tf.exists():
        m = re.search(r"^OnCalendar=(.+)$", tf.read_text(), re.M)
        if m:
            oncal = m.group(1).strip()
    if active and oncal:
        preset = next((k for k, (cal, _) in SCHEDULES.items() if cal == oncal), "custom")
    label = SCHEDULES.get(preset, (None, oncal or "custom"))[1] if preset != "custom" else (oncal or "custom")
    return {"enabled": active, "oncalendar": oncal, "preset": preset if active else "manual",
            "label": label if active else "Manual only"}


def set_schedule(preset):
    if preset not in SCHEDULES:
        return False, f"unknown preset {preset!r}"
    tf = UNIT_DIR / "repo-steward.timer"
    if preset == "manual":
        subprocess.run(["systemctl", "--user", "disable", "--now", "repo-steward.timer"],
                       capture_output=True, text=True)
        return True, "manual"
    if not tf.exists():
        return False, "timer unit not found — run install.sh first"
    oncal = SCHEDULES[preset][0]
    text = tf.read_text()
    # Replace all OnCalendar lines with a single one for the chosen preset.
    lines = [ln for ln in text.splitlines() if not ln.startswith("OnCalendar=")]
    out = []
    for ln in lines:
        out.append(ln)
        if ln.strip() == "[Timer]":
            out.append(f"OnCalendar={oncal}")
    tf.write_text("\n".join(out) + "\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "repo-steward.timer"],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr.strip() or preset)


def run_gh(args):
    # subprocess raises rather than returning 127 when gh isn't on PATH, and this
    # server runs under a systemd unit whose PATH is not the maintainer's shell —
    # so "works in my terminal" says nothing about what this process can resolve.
    # Report it as a failed action instead of letting the exception kill the
    # request with no body for the dashboard to show.
    try:
        p = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, ("gh not found on this process's PATH "
                       f"(PATH={os.environ.get('PATH', '')!r}) — the dashboard unit "
                       "needs Environment=PATH and an EnvironmentFile with the token")
    except subprocess.TimeoutExpired:
        return False, "gh timed out after 60s"
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def resolve_discussion_id(full_repo, number, action):
    """GraphQL node id for a repo Discussion. Repo discussions are GraphQL-only
    — `gh issue/pr` verbs don't reach them — so a comment needs the node id, not
    the number. Prefer an id the tick stored on the action; else resolve it from
    owner/repo + number."""
    did = action.get("discussion_id")
    if did:
        return did
    owner, _, name = full_repo.partition("/")
    ok, out = run_gh([
        "api", "graphql",
        "-f", "query=query($owner:String!,$name:String!,$number:Int!){"
              "repository(owner:$owner,name:$name){discussion(number:$number){id}}}",
        "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
        "--jq", ".data.repository.discussion.id"])
    return out.strip() if ok else ""


def post_discussion_comment(full_repo, number, body, action):
    """Add a top-level comment to a repo Discussion via the GraphQL mutation.
    Returns (ok, detail) like run_gh."""
    did = resolve_discussion_id(full_repo, number, action)
    if not did:
        return False, f"could not resolve discussion #{number} in {full_repo}"
    return run_gh([
        "api", "graphql",
        "-f", "query=mutation($id:ID!,$body:String!){"
              "addDiscussionComment(input:{discussionId:$id,body:$body}){comment{url}}}",
        "-f", f"id={did}", "-f", f"body={body}",
        "--jq", ".data.addDiscussionComment.comment.url"])


def execute_action(full_repo, number, item_type, action):
    """Execute one staged action. Returns (ok, detail)."""
    kind = action.get("kind", "")
    body = with_signature(action.get("body", ""))
    labels = action.get("labels", [])
    results = []

    if item_type == "discussion":
        if not body:
            return True, "no discussion body to post"
        ok, out = post_discussion_comment(full_repo, number, body, action)
        return ok, f"discussion comment: {out[:200]}"

    if item_type == "pr" and "review" in kind:
        flag = "--approve" if "approve" in kind else (
            "--request-changes" if "request" in kind else "--comment")
        ok, out = run_gh(["pr", "review", str(number), "-R", full_repo, flag, "--body", body])
        results.append((ok, f"pr review {flag}: {out[:200]}"))
    elif body:
        sub = "pr" if item_type == "pr" else "issue"
        ok, out = run_gh([sub, "comment", str(number), "-R", full_repo, "--body", body])
        results.append((ok, f"{sub} comment: {out[:200]}"))

    if labels and item_type == "issue":
        ok, out = run_gh(["issue", "edit", str(number), "-R", full_repo,
                          "--add-label", ",".join(labels)])
        results.append((ok, f"labels: {out[:200]}"))

    return all(ok for ok, _ in results), "; ".join(d for _, d in results)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/status":
            active = tick_active()
            # Decisions typed while a tick ran queue up; drain them as soon as
            # the steward is free (this poll fires every 5s while the dashboard
            # is open). 5-minute backoff so a failing executor can't hot-loop.
            if (not active and not decide_active() and pending_decisions()
                    and time.time() - DECIDER["last_spawn"] > 300):
                spawn_decider()
            return self._json(200, {
                "tick_active": active,
                "mode": steward_mode(),
                "elapsed_sec": tick_elapsed_sec() if active else None,
                "eta_sec": eta_sec(),
                "progress": tick_progress() if active else None,
                "schedule": current_schedule(),
                "limits": read_limits(),
                # The first-run page polls this to swap itself for the real
                # dashboard as soon as a tick has written one.
                "dashboard_ready": (ROOT / "dashboard.html").exists(),
            })
        if self.path == "/api/progress":
            steps = []
            p = ROOT / "progress.jsonl"
            if p.exists():
                for line in p.read_text().splitlines()[-60:]:
                    try:
                        steps.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return self._json(200, {"steps": steps})
        if self.path == "/api/decisions":
            entries = []
            p = ROOT / "decisions.jsonl"
            if p.exists():
                for line in p.read_text().splitlines()[-50:]:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return self._json(200, {"decisions": entries, "executing": decide_active()})
        if self.path == "/api/watch":
            return self._json(200, {"repos": repos_config(), "resources": list(RESOURCES)})
        parsed = urlparse(self.path)
        if parsed.path == "/api/audit":
            qs = parse_qs(parsed.query)
            try:
                limit = min(int(qs.get("limit", ["200"])[0]), 2000)
            except ValueError:
                limit = 200
            return self._json(200, {"events": audit.read_events(
                limit=limit, repo=qs.get("repo", [None])[0],
                event=qs.get("event", [None])[0])})
        if parsed.path == "/api/staged":
            qs = parse_qs(parsed.query)
            short = qs.get("repo", [""])[0]
            key = qs.get("item", [""])[0]
            ledger_path = ROOT / "state" / f"{short}.json"
            if not ledger_path.exists():
                return self._json(404, {"error": "no ledger for repo"})
            item = json.loads(ledger_path.read_text())["items"].get(key)
            if not item:
                return self._json(404, {"error": "item not in ledger"})
            return self._json(200, {"item": item})
        if parsed.path == "/api/ghstate":
            qs = parse_qs(parsed.query)
            repo = qs.get("repo", [""])[0]
            num = qs.get("num", [""])[0]
            kind = qs.get("kind", ["issue"])[0]
            if "/" not in repo or not num.isdigit():
                return self._json(400, {"error": "repo=owner/name & numeric num required"})
            ep = "pulls" if kind == "pr" else "issues"
            ok, out = run_gh(["api", f"repos/{repo}/{ep}/{num}",
                              "--jq", '{state:.state, merged:(.merged // false), head:(.head.sha // "")}'])
            if not ok:
                return self._json(200, {"state": "unknown", "merged": False})
            try:
                res = json.loads(out)
            except json.JSONDecodeError:
                return self._json(200, {"state": "unknown", "merged": False})
            head = res.pop("head", "")
            if kind == "pr" and res.get("state") == "open" and head:
                # Latest APPROVED review, so the dashboard can mark rows that
                # are already approved on GitHub at the current head — where
                # the only remaining action is the maintainer's merge.
                ok2, out2 = run_gh([
                    "api", f"repos/{repo}/pulls/{num}/reviews", "--jq",
                    '[.[] | select(.state=="APPROVED")] | last // {} '
                    '| {c:(.commit_id // ""), at:(.submitted_at // "")}'])
                if ok2:
                    try:
                        r = json.loads(out2)
                        res["approved_at_head"] = bool(r.get("c")) and r["c"] == head
                        res["approved_at"] = r.get("at", "")
                    except json.JSONDecodeError:
                        pass
            return self._json(200, res)
        if self.path == "/api/metrics":
            def read_jsonl(name):
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
            return self._json(200, {"metrics": read_jsonl("metrics.jsonl"),
                                    "usage": read_jsonl("usage.jsonl")})
        if self.path == "/api/uptime":
            state_path = ROOT / "uptime_state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            samples = []
            p = ROOT / "uptime.jsonl"
            if p.exists():
                lines = p.read_text().splitlines()[-2400:]  # ~2 days at 4 sites/5min
                for line in lines:
                    try:
                        samples.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return self._json(200, {"state": state, "samples": samples})
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        if self.path.startswith("/dashboard.html") and not (ROOT / "dashboard.html").exists():
            payload = first_run_page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        if self.path == "/api/tick":
            if tick_active():
                return self._json(409, {"error": "a tick is already running"})
            if decide_active():
                return self._json(409, {"error": "the decision executor is running — try again shortly"})
            subprocess.run(["systemctl", "--user", "start", "--no-block",
                            "repo-steward.service"], check=False)
            audit.append("tick_requested", "maintainer", "dashboard",
                         summary="tick started from the dashboard")
            return self._json(200, {"started": True})

        if self.path == "/api/decide":
            text = (req.get("decision") or "").strip()
            if not text:
                return self._json(400, {"error": "empty decision"})
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "repo": req.get("repo", ""),
                "refs": req.get("refs", [])[:10],
                "title": (req.get("title") or "").strip()[:200],
                "context": (req.get("context") or "").strip()[:2000],
                "decision": text[:2000],
                "status": "pending",
            }
            with open(ROOT / "decisions.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")
            audit.append("decision_recorded", "maintainer", "dashboard",
                         repo=entry["repo"], ts=entry["ts"],
                         summary=entry["title"] or entry["decision"][:120],
                         data={"decision_ts": entry["ts"],
                               "decision": entry["decision"][:500],
                               "refs": entry["refs"]})
            if tick_active() or decide_active():
                return self._json(200, {"recorded": True, "mode": "queued", "id": entry["ts"]})
            spawn_decider()
            return self._json(200, {"recorded": True, "mode": "executing", "id": entry["ts"]})

        if self.path == "/api/terminal":
            # The decision executor's arm for terminal states. The engine's
            # permission layer denies `gh pr merge/close` even to decide.sh
            # (guardrail 1 stays mechanical), so an explicit merge/close typed
            # by the maintainer is carried out HERE, under their auth — and
            # only while a decision executor is actually running.
            if not decide_active():
                return self._json(403, {"error": "terminal actions are only served while the decision executor runs"})
            action = req.get("action")
            full = req.get("repo", "")
            kind = req.get("kind", "pr")
            num = str(req.get("number", ""))
            if action not in ("merge", "close") or "/" not in full or not num.isdigit():
                return self._json(400, {"error": "need action merge|close, repo owner/name, numeric number"})
            if action == "merge":
                if kind != "pr":
                    return self._json(400, {"error": "only PRs can merge"})
                ok, detail = merge_pr(full, num)
            else:
                sub = "pr" if kind == "pr" else "issue"
                args = [sub, "close", num, "-R", full]
                comment = (req.get("comment") or "").strip()
                if comment:
                    args += ["--comment", with_signature(comment)]
                ok, detail = run_gh(args)
            reason = (req.get("reason") or "").strip()
            with open(ROOT / "approvals.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "repo": full, "action": f"decision-{action}",
                                    "item": f"{kind}-{num}", "ok": ok, "detail": detail[:300],
                                    "reason": reason[:300]}) + "\n")
            audit.append("terminal", "maintainer", "decide",
                         repo=full.split("/")[1], ref=f"{kind}-{num}",
                         ok=ok, detail=detail,
                         summary=f"maintainer decision: {action} {kind} #{num}"
                                 + (f" — {reason}" if reason else ""),
                         data={"action": action})
            return self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        if self.path == "/api/mode":
            new_mode = req.get("mode")
            if new_mode not in ("draft", "live"):
                return self._json(400, {"error": "mode must be 'draft' or 'live'"})
            cfg_path = ROOT / "config.yaml"
            cfg = cfg_path.read_text()
            cfg, n = re.subn(r"^mode:\s*\w+", f"mode: {new_mode}", cfg, count=1, flags=re.M)
            if not n:
                return self._json(500, {"error": "no 'mode:' line found in config.yaml"})
            cfg_path.write_text(cfg)
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"mode → {new_mode}",
                         data={"setting": "mode", "mode": new_mode})
            return self._json(200, {"mode": new_mode})

        if self.path == "/api/schedule":
            ok, detail = set_schedule(req.get("preset", ""))
            if not ok:
                return self._json(400, {"error": detail})
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"schedule → {req.get('preset')}",
                         data={"setting": "schedule", "preset": req.get("preset")})
            return self._json(200, {"schedule": current_schedule()})

        if self.path == "/api/limits":
            ok, detail = set_limits(req.get("substantive"), req.get("light"))
            if not ok:
                return self._json(400, {"error": detail})
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"limits → substantive {detail['substantive']}, light {detail['light']}",
                         data={"setting": "limits", **detail})
            return self._json(200, {"limits": detail})

        if self.path == "/api/watch":
            entries = req.get("repos") or [req]
            for e in entries:
                ok, err = set_watch(e.get("name", ""), e.get("watch"), e.get("priority"))
                if not ok:
                    return self._json(400, {"error": err})
                name = e.get("name", "")
                audit.append("config_change", "maintainer", "dashboard",
                             repo=name.split("/")[1] if "/" in name else name,
                             summary="watch → " + ", ".join(e.get("watch") or ["(unchanged)"])
                                     + (f"; priority {e['priority']}" if e.get("priority") else ""),
                             data={"setting": "watch", "name": name,
                                   "watch": e.get("watch"), "priority": e.get("priority")})
            return self._json(200, {"repos": repos_config(), "resources": list(RESOURCES)})

        if self.path == "/api/dismiss":
            if tick_active() or decide_active():
                return self._json(409, {"error": "steward busy — try again when it finishes"})
            short = req.get("repo", "")
            ledger_path = ROOT / "state" / f"{short}.json"
            if not ledger_path.exists():
                return self._json(400, {"error": f"unknown repo {short!r}"})
            ledger = json.loads(ledger_path.read_text())
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            outcomes = {}
            for key in req.get("items", []):
                item = ledger["items"].get(key)
                if not item:
                    outcomes[key] = {"ok": False}
                    continue
                item["status"] = "dismissed"
                item["last_action"] = "dismissed by maintainer via dashboard (not posted)"
                item["last_action_at"] = now
                outcomes[key] = {"ok": True}
                audit.append("dismiss", "maintainer", "dashboard", repo=short,
                             ref=key, ok=True, ts=now,
                             summary="dismissed via dashboard (nothing posted)")
            ledger_path.write_text(json.dumps(ledger, indent=2))
            with open(ROOT / "approvals.jsonl", "a") as f:
                f.write(json.dumps({"ts": now, "repo": short, "action": "dismiss",
                                    "outcomes": outcomes}) + "\n")
            return self._json(200, {"outcomes": outcomes})

        if self.path == "/api/approve":
            if tick_active() or decide_active():
                return self._json(409, {"error": "steward busy — try again when it finishes"})
            repos = repo_map()
            short = req.get("repo", "")
            full = repos.get(short)
            if not full:
                return self._json(400, {"error": f"unknown repo {short!r}"})
            ledger_path = ROOT / "state" / f"{short}.json"
            ledger = json.loads(ledger_path.read_text())
            outcomes = {}
            for key in req.get("items", []):
                item = ledger["items"].get(key)
                if not item:
                    outcomes[key] = {"ok": False, "detail": "not in ledger"}
                    continue
                number = key.split("-", 1)[1]
                item_ok, details = True, []
                for action in item.get("staged_actions", []):
                    if action.get("executed_at"):
                        continue
                    try:
                        ok, detail = execute_action(full, number, item["type"], action)
                    except Exception as e:
                        # Never let one action's exception take down the whole
                        # request: the dashboard renders a bare failure with no
                        # cause, which is indistinguishable from a rejected post.
                        ok, detail = False, f"{type(e).__name__}: {e}"
                    details.append(detail)
                    if ok:
                        action["executed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    item_ok = item_ok and ok
                # Approving an approve-recommend PR is the maintainer's final
                # look: after the review is up (this click or a previous live
                # post), merge on their behalf.
                merged = False
                if item_ok and item.get("type") == "pr" and any(
                        a.get("kind") == "pr_review_approve"
                        for a in item.get("staged_actions", [])):
                    ok, detail = merge_pr(full, number)
                    details.append(detail)
                    merged = ok
                    item_ok = item_ok and ok
                if item_ok:
                    item["status"] = "done" if merged else "posted"
                    item["last_action"] = ("approved & merged by maintainer via dashboard"
                                           if merged else "approved by maintainer via dashboard; posted")
                    item["last_action_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                outcomes[key] = {"ok": item_ok, "merged": merged, "detail": "; ".join(details)}
                audit.append("approve", "maintainer", "dashboard", repo=short,
                             ref=key, ok=item_ok, detail="; ".join(details),
                             summary="approved via dashboard" + (" & merged" if merged else ""),
                             data={"merged": merged})
            ledger_path.write_text(json.dumps(ledger, indent=2))
            with open(ROOT / "approvals.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "repo": short, "outcomes": outcomes}) + "\n")
            return self._json(200, {"outcomes": outcomes})

        return self._json(404, {"error": "unknown endpoint"})

    def log_message(self, fmt, *args):
        pass  # keep journal quiet; approvals are logged to approvals.jsonl


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
