#!/usr/bin/env python3
"""Repo Steward dashboard server.

Static file serving plus a minimal control API:
  GET  /api/status            -> {tick_active, mode, elapsed_sec, eta_sec, schedule,
                                  dashboard_ready}
  GET  /api/progress          -> {steps: [...]}  (per-item progress the tick emits)
  GET  /api/metrics|uptime    -> chart data
  GET  /api/signals?repo=&kind=&limit= -> normalized evidence for insights
  GET  /api/insights           -> ranked build-candidate themes + build statuses
  GET  /api/evaluation         -> latest self-evaluation, lessons, and cited evidence
  POST /api/build              -> {theme_id, note?}  queue a theme and start build.sh,
        which implements its brief and opens a PR (never merges)
  POST /api/tick              -> {"action"?: "start"|"cancel"}  start one steward
        tick (refused while one runs), or stop the running one
  GET  /api/analysis          -> {insights|evaluation: {running, started_at, last}}
  POST /api/analysis          -> {"job": "insights"|"evaluation", "action"?:
        "start"|"cancel"}  start or stop one out-of-band run (refused while that
        same job runs; the two are independent)
  POST /api/mode              -> {"mode": "draft"|"live"}  (rewrites config.yaml)
  POST /api/backend           -> {"backend": "claude"|"codex"|"gemini"|"opencode"|"custom"}
  POST /api/schedule          -> {"preset": "manual"|"hourly"|"6h"|"daily"|"weekly"}
  POST /api/limits            -> {"substantive": N, "light": N, "proactive": N}
                                  (per-tick work caps)
  POST /api/signature         -> {"enabled": bool}  (toggle the comment sign-off)
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
  GET  /api/work              -> what the steward is running and has queued, and
        what (if anything) holds the ledgers so merges/posts must wait
  GET  /api/audit?repo=&event=&limit=&since=&until= -> events from the decision log
         (audit.jsonl — see audit.py for the schema; every mutating endpoint
         here appends its event at the moment it acts. since/until are YYYY-MM-DD dates.)

Approvals run under the local gh auth — i.e. as Alex, because a human clicked.
Ledger writes are refused while a tick or the decision executor is active to
avoid racing the steward.
"""
import fcntl
import html as html_lib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import audit
import signals
import builds
from tick_guard import review_record_errors

ROOT = Path(__file__).resolve().parent
CONFIG_LOCK = threading.RLock()
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

BACKENDS = {
    "claude": "Claude Code",
    "codex": "OpenAI Codex",
    "gemini": "Gemini CLI",
    "opencode": "OpenCode",
}


def current_backend():
    """Return the agent CLI configured for the next tick.

    The unit file is the source of truth: the dashboard process deliberately
    has no STEWARD_ENGINE environment of its own, and the most recent usage
    record describes the previous tick rather than the next one.
    """
    service = UNIT_DIR / "repo-steward.service"
    text = service.read_text(encoding="utf-8") if service.exists() else ""
    match = re.search(r"^Environment=STEWARD_ENGINE=([^\s]+)", text, re.M)
    engine = match.group(1) if match else "claude"
    model_match = re.search(r"^Environment=STEWARD_MODEL=(.+)$", text, re.M)
    custom_configured = bool(re.search(r"^Environment=STEWARD_ENGINE_CMD=.+$", text, re.M))
    options = []
    for value, label in BACKENDS.items():
        path = shutil.which(value)
        options.append({"value": value, "label": label, "available": bool(path)})
    if custom_configured or engine == "custom":
        options.append({"value": "custom", "label": "Custom command",
                        "available": custom_configured, "custom": True})
    elif engine not in BACKENDS:
        options.append({"value": engine, "label": engine.title(),
                        "available": True, "custom": True})
    return {
        "value": engine,
        "label": "Custom command" if engine == "custom" else BACKENDS.get(engine, engine.title()),
        "model": model_match.group(1).strip() if model_match else None,
        "options": options,
    }


def set_backend(engine):
    """Set the CLI used by subsequent ticks and reload the user unit."""
    service = UNIT_DIR / "repo-steward.service"
    if not service.exists():
        return False, "tick service not found — run install.sh first"
    text = service.read_text(encoding="utf-8")
    original_text = text
    if engine == "custom":
        if not re.search(r"^Environment=STEWARD_ENGINE_CMD=.+$", text, re.M):
            return False, "custom backend has no STEWARD_ENGINE_CMD — configure it with install.sh"
        binary = None
    elif engine in BACKENDS:
        binary = shutil.which(engine)
        if not binary:
            return False, f"{BACKENDS[engine]} is not installed or is not on the dashboard PATH"
    else:
        return False, "backend must be claude, codex, gemini, opencode, or custom"
    if not re.search(r"^Environment=STEWARD_ENGINE=", text, re.M):
        return False, "tick service has no STEWARD_ENGINE setting — run install.sh first"
    old = current_backend()["value"]
    text = re.sub(r"^Environment=STEWARD_ENGINE=.*$",
                  f"Environment=STEWARD_ENGINE={engine}", text, count=1, flags=re.M)
    bin_line = f"Environment=STEWARD_ENGINE_BIN={binary}" if binary else None
    if engine == "custom":
        text = re.sub(r"^Environment=STEWARD_ENGINE_BIN=.*\n?", "", text,
                      count=1, flags=re.M)
    elif re.search(r"^Environment=STEWARD_ENGINE_BIN=", text, re.M):
        text = re.sub(r"^Environment=STEWARD_ENGINE_BIN=.*$", bin_line,
                      text, count=1, flags=re.M)
    else:
        text = re.sub(r"^(Environment=STEWARD_ENGINE=.*)$", r"\1\n" + bin_line,
                      text, count=1, flags=re.M)
    # Model identifiers are provider-specific. Carrying one across a switch
    # makes the next run fail in a much less obvious place, so return the new
    # provider to its own default model.
    if old != engine:
        text = re.sub(r"^Environment=STEWARD_MODEL=.*\n?", "", text, flags=re.M)
    service.write_text(text, encoding="utf-8")
    reload_result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    if reload_result.returncode != 0:
        service.write_text(original_text, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, text=True)
        return False, (reload_result.stderr or reload_result.stdout).strip() or "systemd reload failed"
    return True, current_backend()

def first_run_page():
    """The dashboard before any tick has generated one: the real chrome and the
    real configured fleet, with the tick button and progress strip the controls
    script renders. dashboard-first-run.html is a tracked file; the repo cards
    are injected here so the filter lens sees them at load."""
    rows = []
    for r in repos_config():
        watched = ", ".join(r["watch"]) if len(r["watch"]) < len(RESOURCES) else "all"
        prio = '<i class="pri" title="high priority"></i>' if r["priority"] == "high" else ""
        rows.append(f'<tr title="{html_lib.escape(r["name"])}"><td class="mono">{prio}'
                    f'{html_lib.escape(r["short"])}</td><td class="muted">{html_lib.escape(watched)}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="2" class="muted">no repositories: add a repos: entry to config.yaml</td></tr>')
    cards = rows
    page = (ROOT / "dashboard-first-run.html").read_text()
    return page.replace("<!--REPOS-->", "\n".join(cards))


def versioned_dashboard(page, root=ROOT):
    """Point dashboard HTML at the exact controls asset currently on disk."""
    controls = root / "steward-controls.js"
    version = format(controls.stat().st_mtime_ns, "x") if controls.exists() else "missing"
    return re.sub(
        r'(/steward-controls\.js)(?:\?v=[^"\s]*)?',
        lambda match: match.group(1) + "?v=" + version,
        page,
    )


def repo_map():
    """Short repo name -> owner/repo, parsed from config.yaml."""
    m = {}
    for line in (ROOT / "config.yaml").read_text().splitlines():
        match = re.match(r"\s*-\s*name:\s*(\S+/\S+)", line)
        if match:
            full = match.group(1)
            m[full.split("/")[1]] = full
    return m


REF_PATH = {"pr": "pull", "issue": "issues", "disc": "discussions"}


def ref_url(full_repo, ref):
    """owner/repo + a ledger ref (pr-123/issue-45/disc-7) -> its GitHub URL,
    or None when either half is missing/unrecognised."""
    if not full_repo or not ref:
        return None
    kind, _, num = ref.partition("-")
    path = REF_PATH.get(kind)
    if not path or not num.isdigit():
        return None
    return f"https://github.com/{full_repo}/{path}/{num}"


RESOURCES = ("issues", "prs", "discussions")


def atomic_write_text(path, text):
    """Replace a text file without exposing a truncated intermediate state."""
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    with CONFIG_LOCK:
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
        atomic_write_text(path, "".join(lines[:i] + block + lines[j:]))
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


def signature_enabled():
    m = re.search(r"^signature_enabled:\s*(\w+)", (ROOT / "config.yaml").read_text(), re.M)
    return m.group(1).lower() != "false" if m else True


def with_signature(body):
    """Ensure the posted body ends with the CURRENT config signature, so a
    change to the signature (or the project URL) takes effect immediately for
    every pending item, regardless of when it was staged. With
    signature_enabled: false the same rule runs in reverse: any signature
    baked into an older staged draft is stripped before posting."""
    if not body:
        return body
    if not signature_enabled():
        marker = body.find("🤝")
        return body[:marker].rstrip() if marker != -1 else body
    sig = steward_signature()
    if not sig or sig in body:
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
            "light": g("light_items_per_tick", 24),
            "proactive": g("proactive_items_per_tick", 1)}


def set_limits(sub, light, proactive=None):
    try:
        sub, light = int(sub), int(light)
        proactive = read_limits()["proactive"] if proactive is None else int(proactive)
    except (TypeError, ValueError):
        return False, "limits must be integers"
    if not (1 <= sub <= 100 and 1 <= light <= 200 and 0 <= proactive <= 20):
        return False, "out of range (substantive 1-100, light 1-200, work queue 0-20)"
    with CONFIG_LOCK:
        path = ROOT / "config.yaml"
        txt = path.read_text()
        txt, n1 = re.subn(r"^(\s*substantive_items_per_tick:\s*)\d+",
                          lambda m: m.group(1) + str(sub), txt, count=1, flags=re.M)
        txt, n2 = re.subn(r"^(\s*light_items_per_tick:\s*)\d+",
                          lambda m: m.group(1) + str(light), txt, count=1, flags=re.M)
        if not (n1 and n2):
            return False, "limits block not found in config.yaml"
        txt, n3 = re.subn(r"^(\s*proactive_items_per_tick:\s*)\d+",
                          lambda m: m.group(1) + str(proactive), txt, count=1, flags=re.M)
        if not n3:
            # Older configs predate this limit. Add it next to the other tick caps,
            # using the light-limit indentation and leaving inline comments intact.
            txt, n3 = re.subn(
                r"^([ \t]*)light_items_per_tick:.*$",
                lambda m: m.group(0) + "\n" + m.group(1)
                + "proactive_items_per_tick: " + str(proactive),
                txt, count=1, flags=re.M,
            )
        if not n3:
            return False, "limits block not found in config.yaml"
        atomic_write_text(path, txt)
    return True, {"substantive": sub, "light": light, "proactive": proactive}


def tick_active():
    state = subprocess.run(
        ["systemctl", "--user", "is-active", "repo-steward.service"],
        capture_output=True, text=True,
    ).stdout.strip()
    return state in ("active", "activating")


def latest_tick_result():
    """The last completed tick, from the wrapper's authoritative audit line.
    This is intentionally independent of service state: systemd becomes
    inactive just after tick.sh has recorded the result."""
    p = ROOT / "audit.jsonl"
    if not p.exists():
        return None
    for line in reversed(p.read_text().splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "tick_done" or event.get("via") != "tick":
            continue
        data = event.get("data") or {}
        return {
            "ts": event.get("ts"),
            "ok": bool(event.get("ok")),
            "rc": data.get("rc"),
            "summary": event.get("summary", "tick finished"),
        }
    return None


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


# Out-of-band analysis jobs. The scripts take these locks themselves, so a run
# started with `make` is seen here too. Both jobs are read-only toward GitHub
# and the queue, so neither waits for the other or for a tick.
ANALYSIS_JOBS = {
    "insights": {"script": "insights.sh", "log": "insights.log", "output": "insights.json",
                 "lock": ".insights.lock", "label": "insight sweep"},
    "evaluation": {"script": "evaluate.sh", "log": "evaluation.log", "output": "evaluation.json",
                   "lock": ".evaluation.lock", "label": "self-evaluation"},
    # Not read-only: builds push a branch and open a PR. They work in their
    # own clones (work/builds/), so they do not wait for a tick either.
    "build": {"script": "build.sh", "log": "build.log", "output": "builds.json",
              "lock": ".build.lock", "label": "build"},
}
ANALYSIS_ENV_KEYS = ("STEWARD_ENGINE", "STEWARD_ENGINE_BIN", "STEWARD_MODEL",
                     "STEWARD_ENGINE_CMD", "PATH")
ANALYSIS_RESULT = re.compile(
    r"^=== (?:insights|evaluation) (\S+) (.*?) ===$")


# Launch time per job: systemd-run returns before the script takes its lock,
# so a just-started job counts as running for a short grace period.
ANALYSIS_LAUNCHED = {}
ANALYSIS_START_GRACE_SEC = 20


def analysis_running(job, root=ROOT):
    if time.time() - ANALYSIS_LAUNCHED.get(job, 0) < ANALYSIS_START_GRACE_SEC:
        return True
    lock = root / ANALYSIS_JOBS[job]["lock"]
    if not lock.exists():
        return False
    with open(lock, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def analysis_last_result(job, root=ROOT):
    """Outcome of the most recent finished run: the script's own log, unless
    the published output is newer (it can be published by hand)."""
    result = analysis_logged_result(job, root)
    try:
        output = json.loads((root / ANALYSIS_JOBS[job]["output"]).read_text(encoding="utf-8"))
        generated = output.get("generated_at")
    except (OSError, ValueError, AttributeError):
        generated = None
    if generated and (result is None or generated > result["ts"]):
        return {"ts": generated, "outcome": "published"}
    return result


def analysis_logged_result(job, root=ROOT):
    log = root / "logs" / ANALYSIS_JOBS[job]["log"]
    if not log.exists():
        return None
    for line in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()):
        match = ANALYSIS_RESULT.match(line)
        if not match:
            continue
        ts, rest = match.groups()
        if rest.startswith("published"):
            return {"ts": ts, "outcome": "published"}
        if rest.startswith("skipped"):
            continue
        if rest.startswith("cancelled"):
            return {"ts": ts, "outcome": "cancelled"}
        if "rejected" in rest:
            return {"ts": ts, "outcome": "rejected",
                    "detail": "the model's output failed validation; the previous result is kept"}
        if rest.startswith("failed") or rest.startswith("preparation failed"):
            rc = re.search(r"rc=(\d+)", rest)
            detail = {"124": "timed out", "143": "stopped"}.get(
                rc.group(1) if rc else "", rest)
            return {"ts": ts, "outcome": "failed", "detail": detail}
    return None


def analysis_status(root=ROOT):
    out = {}
    for job in ANALYSIS_JOBS:
        running = analysis_running(job, root)
        lock = root / ANALYSIS_JOBS[job]["lock"]
        started = None
        if running:
            stamp = lock.stat().st_mtime if lock.exists() else ANALYSIS_LAUNCHED[job]
            started = (datetime.fromtimestamp(max(stamp, ANALYSIS_LAUNCHED.get(job, 0)), timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"))
        out[job] = {"running": running, "started_at": started,
                    "last": analysis_last_result(job, root)}
    return out


def tick_unit_environment():
    """systemd properties that give a job the same engine/credentials as a tick,
    read from the unit that defines them (see current_backend)."""
    service = UNIT_DIR / "repo-steward.service"
    text = service.read_text(encoding="utf-8") if service.exists() else ""
    props = [f"EnvironmentFile={m.group(1).strip()}"
             for m in re.finditer(r"^EnvironmentFile=(.+)$", text, re.M)]
    for m in re.finditer(r"^Environment=([A-Z_]+)=(.*)$", text, re.M):
        if m.group(1) in ANALYSIS_ENV_KEYS:
            props.append(f"Environment={m.group(1)}={m.group(2).strip()}")
    return props


def analysis_unit(job):
    return f"repo-steward-{job}.service"


def unit_state(unit):
    return subprocess.run(["systemctl", "--user", "is-active", unit],
                          capture_output=True, text=True).stdout.strip()


def stop_unit(unit):
    result = subprocess.run(["systemctl", "--user", "stop", unit],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "systemctl stop failed"
    return True, None


def process_tree(pid):
    """A pid and its descendants, children before parents.

    A run started with `make` is not in a unit of its own, so stopping it means
    signalling the script and whatever it is waiting on (timeout, the agent CLI)
    rather than a cgroup."""
    children = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)],
                              capture_output=True, text=True).stdout.split()
    out = []
    for child in children:
        out.extend(process_tree(int(child)))
    out.append(pid)
    return out


def lock_pid(job, root=ROOT):
    try:
        return int((root / ANALYSIS_JOBS[job]["lock"]).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def note_analysis_cancelled(job, root=ROOT):
    """Record the stop in the script's own log: a signalled run writes nothing."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(root / "logs" / ANALYSIS_JOBS[job]["log"], "a", encoding="utf-8") as handle:
        handle.write(f"=== {job} {ts} cancelled by maintainer ===\n")
    return ts


def cancel_analysis(job, root=ROOT):
    label = ANALYSIS_JOBS[job]["label"]
    if not analysis_running(job, root):
        return False, f"no {label} is running"
    unit = analysis_unit(job)
    if unit_state(unit) in {"active", "activating", "reloading", "deactivating"}:
        ok, error = stop_unit(unit)
        if not ok:
            return False, error
    else:
        pid = lock_pid(job, root)
        if pid is None:
            return False, (f"the running {label} left no pid in its lock — "
                           "stop it where it was started")
        try:
            for target in process_tree(pid):
                os.kill(target, signal.SIGTERM)
        except OSError as error:
            return False, f"could not stop pid {pid}: {error}"
    ANALYSIS_LAUNCHED.pop(job, None)
    note_analysis_cancelled(job, root)
    return True, None


def start_analysis(job, root=ROOT):
    """Launch one job as a transient user unit so it outlives dashboard restarts."""
    spec = ANALYSIS_JOBS[job]
    (root / "logs").mkdir(exist_ok=True)
    log = root / "logs" / spec["log"]
    subprocess.run(["systemctl", "--user", "reset-failed", analysis_unit(job)],
                   capture_output=True)
    cmd = ["systemd-run", "--user", "--no-block", "--collect", "--quiet",
           f"--unit={analysis_unit(job)}",
           f"--description=Repo Steward {spec['label']}",
           f"--property=WorkingDirectory={root}",
           f"--property=StandardOutput=append:{log}",
           f"--property=StandardError=append:{log}"]
    cmd += [f"--property={prop}" for prop in tick_unit_environment()]
    cmd += ["/bin/bash", str(root / spec["script"])]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "systemd-run failed"
    ANALYSIS_LAUNCHED[job] = time.time()
    return True, None


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


def _jsonl_tail(path, n):
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _since(ts_epoch):
    return datetime.fromtimestamp(ts_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def work_status(root=ROOT):
    """Everything the steward is doing or has queued, for the Work panel.

    `running` lists live processes with what each is working on; `queued`
    lists work waiting for its turn. `ledger_lock` names what currently holds
    the ledgers: while set, merges, posts and dismissals are refused."""
    running, queued = [], []

    if tick_active():
        steps = [s for s in _jsonl_tail(root / "progress.jsonl", 40) if s.get("msg")]
        prog = tick_progress() or {}
        running.append({
            "kind": "tick", "title": "Tick",
            "elapsed_sec": tick_elapsed_sec(),
            "detail": " · ".join(x for x in (prog.get("phase"), prog.get("note")) if x),
            "steps": [{"repo": s.get("repo"), "ref": s.get("ref"), "msg": s.get("msg")} for s in steps[-8:]],
            "holds_ledgers": True,
        })

    decisions = _jsonl_tail(root / "decisions.jsonl", 200)
    waiting = [d for d in decisions if d.get("status") == "pending" and not d.get("note")]
    if decide_active():
        started = None
        try:
            started = (root / ".decide.pid").stat().st_mtime
        except OSError:
            pass
        actions = [a for a in _jsonl_tail(root / "activity.jsonl", 20) if a.get("summary")]
        running.append({
            "kind": "decide", "title": "Carrying out your decisions",
            "elapsed_sec": int(time.time() - started) if started else None,
            "detail": "; ".join((d.get("title") or d.get("decision") or "")[:90] for d in waiting) or "",
            "steps": [{"repo": a.get("repo"), "ref": a.get("ref"), "msg": a.get("summary")} for a in actions[-6:]],
            "holds_ledgers": True,
        })
    else:
        for d in waiting:
            queued.append({"kind": "decision", "title": (d.get("title") or d.get("decision") or "")[:140],
                           "since": d.get("ts"), "detail": (d.get("decision") or "")[:200]})
    for d in decisions:
        if d.get("status") == "pending" and d.get("note"):
            queued.append({"kind": "clarify", "title": (d.get("title") or "")[:140], "since": d.get("ts"),
                           "detail": "needs your clarification: " + d["note"][:200]})

    for job, spec in ANALYSIS_JOBS.items():
        if job == "build" or not analysis_running(job, root):
            continue
        lock = root / spec["lock"]
        running.append({"kind": job, "title": spec["label"].capitalize(),
                        "elapsed_sec": int(time.time() - lock.stat().st_mtime) if lock.exists() else None,
                        "detail": "read-only: does not block merges", "steps": [], "holds_ledgers": False})

    for b in builds.read(root).get("items", {}).values():
        if b.get("status") == "building":
            started = b.get("started_at")
            elapsed = None
            if started:
                try:
                    elapsed = int(time.time() - datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
                                  .replace(tzinfo=timezone.utc).timestamp())
                except ValueError:
                    pass
            running.append({"kind": "build", "title": "Build: " + (b.get("title") or ""), "elapsed_sec": elapsed,
                            "detail": b.get("repo", "") + " · own clone, does not block merges",
                            "steps": [], "holds_ledgers": False, "theme_id": b.get("theme_id")})
        elif b.get("status") == "queued":
            queued.append({"kind": "build", "title": "Build: " + (b.get("title") or ""),
                           "since": b.get("requested_at"), "detail": b.get("repo", ""),
                           "theme_id": b.get("theme_id")})

    holder = next((r for r in running if r["holds_ledgers"]), None)
    return {"running": running, "queued": queued, "recent": recent_work(root),
            "ledger_lock": ({"kind": holder["kind"], "title": holder["title"],
                             "elapsed_sec": holder.get("elapsed_sec"), "detail": holder.get("detail")}
                            if holder else None),
            "schedule": current_schedule()}


# Audit events that close a piece of work, and how the Work panel labels them.
RECENT_EVENTS = {"tick_done": "tick", "decide_done": "decisions", "decision_executed": "decision",
                 "terminal": "merge/close", "approve": "approve", "insights_done": "sweep",
                 "evaluation_done": "evaluation", "build_done": "build", "analysis_cancelled": "stopped"}


def recent_work(root=ROOT, limit=10):
    """The last finished pieces of work, newest first, from the decision log."""
    out = []
    for e in reversed(_jsonl_tail(root / "audit.jsonl", 600)):
        label = RECENT_EVENTS.get(e.get("event"))
        if not label:
            continue
        out.append({"ts": e.get("ts"), "kind": label, "ok": e.get("ok", True) is not False,
                    "repo": e.get("repo"), "ref": e.get("ref"), "via": e.get("via"),
                    "summary": (e.get("summary") or "")[:220]})
        if len(out) >= limit * 2:
            break
    return sorted(out, key=lambda r: r.get("ts") or "", reverse=True)[:limit]


def busy_error(action="try again"):
    """A specific refusal: name what holds the ledgers instead of 'busy'."""
    lock = work_status()["ledger_lock"]
    if not lock:
        return None
    what = ("a tick is running" if lock["kind"] == "tick"
            else "the decision runner is carrying out: " + (lock.get("detail") or "your decisions"))
    elapsed = lock.get("elapsed_sec")
    took = (f" ({elapsed // 60}m so far)" if elapsed and elapsed >= 60
            else f" ({elapsed}s so far)" if elapsed else "")
    return (f"Steward busy: {what}{took}"
            + f". {action} when it finishes; the Work panel in the top bar shows progress.")


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

    # `gh pr merge` describes a strict branch-protection refusal as a conflict
    # in some versions.  Check GitHub's structured merge state *before* using
    # that wording to decide that a contributor must resolve a real conflict.
    # Updating remains part of this explicit maintainer-approved merge action;
    # it is never performed by a background tick.
    st_ok, st = run_gh(["pr", "view", str(number), "-R", full_repo,
                        "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"])

    def queue_after_update(steps):
        ok, out = run_gh(["pr", "merge", str(number), "-R", full_repo,
                          flag, "--auto"])
        if ok:
            return True, f"merge {flag}: " + "; ".join(steps + ["auto-merge queued"])
        return False, f"merge {flag}: " + "; ".join(
            steps + [f"auto-merge failed: {out[:200]}"])

    def update_then_queue():
        upd_ok, upd_out = run_gh(["api", "-X", "PUT",
                                  f"repos/{full_repo}/pulls/{number}/update-branch"])
        if not upd_ok:
            return False, f"merge {flag}: head behind base; update-branch failed: {upd_out[:200]}"
        return queue_after_update(["branch updated"])

    if st_ok and st == "BEHIND":
        return update_then_queue()

    ok, out = run_gh(["pr", "merge", str(number), "-R", full_repo, flag])
    if ok:
        return True, f"merge {flag}: {(out or 'merged')[:200]}"

    # The base may have moved between the preflight query and merge request.
    # Recheck the structured state before interpreting gh's human text.
    if not st_ok or st != "BEHIND":
        st_ok, st = run_gh(["pr", "view", str(number), "-R", full_repo,
                            "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"])
    if st_ok and st == "BEHIND":
        return update_then_queue()

    if "merge commit cannot be cleanly created" in out or "not mergeable" in out:
        # The head conflicts with base (mergeStateStatus DIRTY). gh's stderr
        # suggests --auto, but auto-merge only waits out unmet requirements —
        # it never resolves conflicts — so don't parrot that advice. Dependabot
        # branches are fixable from here: a `@dependabot rebase` comment makes
        # the bot recreate the branch on top of base.
        head_ok, head = run_gh(["pr", "view", str(number), "-R", full_repo,
                                "--json", "headRefName", "--jq", ".headRefName"])
        if head_ok and head.startswith("dependabot/"):
            c_ok, _ = run_gh(["pr", "comment", str(number), "-R", full_repo,
                              "--body", "@dependabot rebase"])
            if c_ok:
                return False, (f"merge {flag}: PR conflicts with its base branch — "
                               "asked dependabot to rebase it; approve again once "
                               "the branch is refreshed")
        return False, (f"merge {flag}: PR conflicts with its base branch — the "
                       "author must rebase or resolve conflicts before it can "
                       "merge (the approval review itself is posted)")
    retriable = ("not up to date with the base branch" in out
                 or ("Required status check" in out and "is expected" in out))
    if not retriable:
        return False, f"merge {flag}: {out[:200]}"
    # Required checks may simply still be reporting. Queue auto-merge; this
    # does not fabricate a passing status or bypass branch protection.
    return queue_after_update([])


def auto_merge_days():
    match = re.search(r"^\s*auto_merge_after_days:\s*(\d+)",
                      (ROOT / "config.yaml").read_text(), re.M)
    return int(match.group(1)) if match else 0


def auto_merge_candidate(item, pr, days, now=None):
    """Pure eligibility check; GitHub facts are supplied by the caller."""
    if days <= 0:
        return False, "background auto-merge is disabled"
    if item.get("status") != "ready-for-maintainer" or item.get("verdict") != "approve-recommend":
        return False, "ledger item is not a steward approve-recommend"
    if item.get("iterations", 0) != 0:
        return False, "contributor pushed after the steward review"
    if pr.get("state") != "OPEN":
        return False, f"PR is {pr.get('state', 'not open')}"
    if pr.get("mergeStateStatus") in ("BEHIND", "DIRTY"):
        return False, f"PR merge state is {pr['mergeStateStatus']}"
    head = pr.get("headRefOid")
    if not head:
        return False, "GitHub did not return the current head"
    if item.get("head_oid") and item["head_oid"] != head:
        return False, "ledger head does not match GitHub"

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    matching = []
    for review in pr.get("reviews") or []:
        commit = review.get("commit") or {}
        submitted = review.get("submittedAt")
        if review.get("state") != "APPROVED" or commit.get("oid") != head or not submitted:
            continue
        try:
            when = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when <= cutoff:
            matching.append(when)
    if not matching:
        return False, f"no approval at the current head is at least {days} day(s) old"
    return True, "eligible"


def steward_approval_recorded(short, ref):
    """The ledger alone is mutable; require matching durable tick evidence."""
    path = ROOT / "audit.jsonl"
    if not path.exists():
        return False
    for line in reversed(path.read_text().splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "steward_action" or event.get("repo") != short or event.get("ref") != ref:
            continue
        if event.get("kind") == "posted" and re.search(
                r"approv|ready for your final look", event.get("summary", ""), re.I):
            return True
    return False


def authorize_tick_auto_merge(full_repo, number):
    if steward_mode() != "live":
        return False, "background auto-merge requires live mode"
    short = full_repo.split("/")[-1]
    if repo_map().get(short) != full_repo:
        return False, "repository is not configured"
    ref = f"pr-{number}"
    ledger_path = ROOT / "state" / f"{short}.json"
    if not ledger_path.exists():
        return False, "repository ledger is missing"
    item = json.loads(ledger_path.read_text()).get("items", {}).get(ref)
    if not item:
        return False, "PR is not in the repository ledger"
    evidence_errors = review_record_errors(item)
    if evidence_errors:
        return False, "approval lacks canonical review evidence: " + "; ".join(evidence_errors)
    latest_record = item["review_records"][-1]
    if latest_record.get("verdict") != "approve-recommend":
        return False, "latest canonical review is not an approval"
    if not latest_record.get("posted_at"):
        return False, "canonical approval has no posted_at timestamp"
    if not steward_approval_recorded(short, ref):
        return False, "no durable steward approval event exists"
    ok, out = run_gh(["pr", "view", str(number), "-R", full_repo,
                      "--json", "state,headRefOid,mergeStateStatus,reviews"])
    if not ok:
        return False, f"could not verify PR: {out[:200]}"
    try:
        pr = json.loads(out)
    except json.JSONDecodeError:
        return False, "GitHub returned invalid PR data"
    return auto_merge_candidate(item, pr, auto_merge_days())


def merge_pr_background(full_repo, number):
    """Merge without update-branch or queued auto-merge side effects."""
    flag = merge_method_flag(full_repo)
    ok, out = run_gh(["pr", "merge", str(number), "-R", full_repo, flag])
    return ok, f"background merge {flag}: {(out or 'merged')[:200]}"


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


def prepare_review_record(item, action, now):
    """Return (ok, detail) after linking a review action to durable evidence.

    Actions created before review records existed are retained as explicitly
    incomplete legacy evidence. Newly linked actions are held to the strict
    integrity contract and can never silently diverge from their record.
    """
    if item.get("type") != "pr" or "review" not in action.get("kind", ""):
        return True, ""
    records = item.get("review_records")
    record = records[-1] if isinstance(records, list) and records else None
    link = action.get("review_record_id")
    if link:
        errors = review_record_errors(item)
        if errors:
            return False, "; ".join(errors)
        if record.get("id") != link:
            return False, "staged review_record_id does not match review_record.id"
        return True, ""

    if isinstance(record, dict):
        return False, "review action is not linked to its existing review_record"

    verdict = "approve-recommend" if action.get("kind") == "pr_review_approve" else "iterate"
    signal = item.get("signal") if isinstance(item.get("signal"), dict) else {}
    test_evidence = signal.get("test_evidence")
    if isinstance(test_evidence, str) and test_evidence.strip():
        test_evidence = [test_evidence.strip()]
    elif not isinstance(test_evidence, list):
        test_evidence = []
    record_id = f"legacy-{(item.get('head_oid') or 'unknown')[:12]}-{now.replace(':', '')}"
    record = {
        "v": 1,
        "id": record_id,
        "head_oid": item.get("head_oid") or "unknown",
        "verdict": verdict,
        "body": action.get("body", ""),
        "recorded_at": action.get("staged_at") or now,
        "posted_at": None,
        "claims": [],
        "risk_areas": signal.get("risk_areas") if isinstance(signal.get("risk_areas"), list) else [],
        "test_evidence": test_evidence,
        "review_basis": signal.get("review_basis", ""),
        "integrity": "legacy-backfill",
    }
    item.setdefault("review_records", []).append(record)
    action["review_record_id"] = record_id
    return True, "legacy review evidence backfilled before posting"


def latest_review_record(item):
    records = item.get("review_records")
    return records[-1] if isinstance(records, list) and records and isinstance(records[-1], dict) else {}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        elif path.endswith((".html", ".js", ".css")):
            # Dashboard assets change in place. Force browsers to revalidate so
            # a long-lived tab cannot keep controls from an older deployment.
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

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
                "backend": current_backend(),
                "limits": read_limits(),
                "signature_enabled": signature_enabled(),
                "last_tick": latest_tick_result(),
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
        if self.path == "/api/work":
            return self._json(200, work_status())
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
            events = audit.read_events(
                limit=limit, repo=qs.get("repo", [None])[0],
                event=qs.get("event", [None])[0],
                since=qs.get("since", [None])[0],
                until=qs.get("until", [None])[0])
            rmap = repo_map()
            for e in events:
                u = ref_url(rmap.get(e.get("repo", "")), e.get("ref"))
                if u:
                    e["url"] = u
            return self._json(200, {"events": events})
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
                              "--jq", '{state:.state, merged:(.merged // false), head:(.head.sha // ""), auto_merge:(.auto_merge != null), behind:(.mergeable_state == "behind")}'])
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
        if parsed.path == "/api/signals":
            qs = parse_qs(parsed.query)
            try:
                limit = min(max(int(qs.get("limit", ["500"])[0]), 0), 5000)
            except ValueError:
                limit = 500
            records = signals.query(
                ROOT / "signals.jsonl", limit=limit,
                repo=qs.get("repo", [None])[0], kind=qs.get("kind", [None])[0])
            return self._json(200, {"signals": records})
        if parsed.path == "/api/insights":
            graph_path = ROOT / "insights.json"
            try:
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                graph = None
            except json.JSONDecodeError:
                return self._json(500, {"error": "published insights are invalid JSON"})
            if graph is not None and graph.get("v") != 2:
                graph = None  # the retired pattern graph; the page asks for a sweep
            return self._json(200, {"insights": graph, "builds": builds.read(ROOT)["items"],
                                    "building": analysis_running("build")})
        if parsed.path == "/api/evaluation":
            report_path = ROOT / "evaluation.json"
            if not report_path.exists():
                return self._json(200, {"evaluation": None, "lessons": [],
                                        "evidence": {}, "runs": 0})
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return self._json(500, {"error": "published evaluation is invalid JSON"})
            cited = set()
            for dimension in report.get("dimensions", []):
                cited.update(dimension.get("signal_ids", []))
            for finding in report.get("findings", []):
                cited.update(finding.get("signal_ids", []))
            evidence = {row["id"]: row for row in signals.read_jsonl(ROOT / "signals.jsonl")
                        if row.get("id") in cited}
            history = signals.read_jsonl(ROOT / "evaluations.jsonl")
            return self._json(200, {"evaluation": report,
                                    "lessons": report.get("lessons", []),
                                    "evidence": evidence, "runs": len(history)})
        if self.path == "/api/analysis":
            return self._json(200, analysis_status())
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
        if parsed.path == "/dashboard.html":
            dashboard = ROOT / "dashboard.html"
            page = dashboard.read_text(encoding="utf-8") if dashboard.exists() else first_run_page()
            payload = versioned_dashboard(page).encode()
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
            if req.get("action") == "cancel":
                if not tick_active():
                    return self._json(409, {"error": "no tick is running"})
                ok, error = stop_unit("repo-steward.service")
                if not ok:
                    return self._json(500, {"error": error})
                audit.append("tick_cancelled", "maintainer", "dashboard", ok=False,
                             summary="tick stopped from the dashboard mid-run")
                return self._json(200, {"cancelled": True})
            if tick_active():
                return self._json(409, {"error": "a tick is already running"})
            if decide_active():
                return self._json(409, {"error": "the decision executor is running — try again shortly"})
            subprocess.run(["systemctl", "--user", "start", "--no-block",
                            "repo-steward.service"], check=False)
            audit.append("tick_requested", "maintainer", "dashboard",
                         summary="tick started from the dashboard")
            return self._json(200, {"started": True})

        if self.path == "/api/analysis":
            job = req.get("job")
            if job not in ANALYSIS_JOBS:
                return self._json(400, {"error": "job must be insights or evaluation"})
            label = ANALYSIS_JOBS[job]["label"]
            if req.get("action") == "cancel":
                ok, error = cancel_analysis(job)
                if not ok:
                    return self._json(409 if "no " + label in error else 500, {"error": error})
                audit.append("analysis_cancelled", "maintainer", "dashboard", ok=False,
                             summary=f"{label} stopped from the dashboard mid-run",
                             data={"job": job})
                return self._json(200, {"cancelled": True, **analysis_status()})
            if analysis_running(job):
                return self._json(409, {"error": f"a {label} is already running"})
            ok, error = start_analysis(job)
            if not ok:
                return self._json(500, {"error": error})
            audit.append("analysis_requested", "maintainer", "dashboard",
                         summary=f"{label} started from the dashboard", data={"job": job})
            return self._json(200, {"started": True, **analysis_status()})

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

        if self.path == "/api/build":
            # The maintainer's Build click: queue the theme's brief and start
            # build.sh, which implements it and opens a PR (never merges).
            theme_id = (req.get("theme_id") or "").strip()
            try:
                graph = json.loads((ROOT / "insights.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._json(409, {"error": "no build-candidate list is published"})
            theme = next((t for t in graph.get("themes", []) if t.get("id") == theme_id), None)
            if not theme:
                return self._json(404, {"error": "theme is not in the current list"})
            ok, result = builds.enqueue(theme, (req.get("note") or "").strip(), ROOT)
            if not ok:
                return self._json(409, {"error": result})
            if not analysis_running("build"):
                started, error = start_analysis("build")
                if not started:
                    return self._json(500, {"error": f"queued, but the build runner did not start: {error}"})
            return self._json(200, {"queued": True, "builds": builds.read(ROOT)["items"],
                                    "building": True})

        if self.path == "/api/terminal":
            # The decision executor's arm for terminal states. The engine's
            # permission layer denies `gh pr merge/close` even to decide.sh
            # (guardrail 1 stays mechanical), so an explicit merge/close typed
            # by the maintainer is carried out HERE, under their auth — and
            # only while a decision executor is actually running.
            action = req.get("action")
            full = req.get("repo", "")
            kind = req.get("kind", "pr")
            num = str(req.get("number", ""))
            if action not in ("merge", "close") or "/" not in full or not num.isdigit():
                return self._json(400, {"error": "need action merge|close, repo owner/name, numeric number"})
            decision_terminal = decide_active()
            tick_auto_merge = (
                not decision_terminal and tick_active() and action == "merge" and kind == "pr"
                and (req.get("reason") or "").startswith("auto-merge: steward approved ")
            )
            if not decision_terminal and not tick_auto_merge:
                return self._json(403, {"error": "terminal action is not an active decision or verified tick auto-merge"})
            if tick_auto_merge:
                eligible, why = authorize_tick_auto_merge(full, num)
                if not eligible:
                    return self._json(403, {"error": f"auto-merge refused: {why}"})
                ok, detail = merge_pr_background(full, num)
            elif action == "merge":
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
                                    "repo": full, "action": ("tick-auto-merge" if tick_auto_merge else f"decision-{action}"),
                                    "item": f"{kind}-{num}", "ok": ok, "detail": detail[:300],
                                    "reason": reason[:300]}) + "\n")
            audit.append("terminal", "steward" if tick_auto_merge else "maintainer",
                         "tick" if tick_auto_merge else "decide",
                         repo=full.split("/")[1], ref=f"{kind}-{num}",
                         ok=ok, detail=detail,
                         summary=(("steward auto-merge: " if tick_auto_merge else "maintainer decision: ")
                                  + f"{action} {kind} #{num}")
                                 + (f" — {reason}" if reason else ""),
                         data={"action": action, "auto_merge": tick_auto_merge})
            return self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        if self.path == "/api/mode":
            new_mode = req.get("mode")
            if new_mode not in ("draft", "live"):
                return self._json(400, {"error": "mode must be 'draft' or 'live'"})
            with CONFIG_LOCK:
                cfg_path = ROOT / "config.yaml"
                cfg = cfg_path.read_text()
                cfg, n = re.subn(
                    r"^mode:\s*\w+", f"mode: {new_mode}", cfg, count=1, flags=re.M)
                if not n:
                    return self._json(500, {"error": "no 'mode:' line found in config.yaml"})
                atomic_write_text(cfg_path, cfg)
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"mode → {new_mode}",
                         data={"setting": "mode", "mode": new_mode})
            return self._json(200, {"mode": new_mode})

        if self.path == "/api/backend":
            if tick_active() or decide_active():
                return self._json(409, {"error": busy_error("Switch backend")})
            old = current_backend()
            ok, detail = set_backend(req.get("backend"))
            if not ok:
                return self._json(400, {"error": detail})
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"backend → {detail['label']}",
                         data={"setting": "backend", "backend": detail["value"],
                               "previous": old["value"], "model_reset": bool(old.get("model"))})
            return self._json(200, {"backend": detail,
                                    "model_reset": bool(old.get("model") and old["value"] != detail["value"])})

        if self.path == "/api/signature":
            enabled = req.get("enabled")
            if not isinstance(enabled, bool):
                return self._json(400, {"error": "enabled must be true or false"})
            with CONFIG_LOCK:
                cfg_path = ROOT / "config.yaml"
                cfg = cfg_path.read_text()
                line = f"signature_enabled: {str(enabled).lower()}"
                cfg, n = re.subn(r"^signature_enabled:.*$", line, cfg, count=1, flags=re.M)
                if not n:
                    cfg, n = re.subn(
                        r"^(signature:)", line + "\n\\1", cfg, count=1, flags=re.M)
                if not n:
                    cfg = cfg.rstrip("\n") + "\n" + line + "\n"
                atomic_write_text(cfg_path, cfg)
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"signature → {'on' if enabled else 'off'}",
                         data={"setting": "signature_enabled", "enabled": enabled})
            return self._json(200, {"signature_enabled": enabled})

        if self.path == "/api/schedule":
            ok, detail = set_schedule(req.get("preset", ""))
            if not ok:
                return self._json(400, {"error": detail})
            audit.append("config_change", "maintainer", "dashboard",
                         summary=f"schedule → {req.get('preset')}",
                         data={"setting": "schedule", "preset": req.get("preset")})
            return self._json(200, {"schedule": current_schedule()})

        if self.path == "/api/limits":
            ok, detail = set_limits(req.get("substantive"), req.get("light"),
                                    req.get("proactive"))
            if not ok:
                return self._json(400, {"error": detail})
            audit.append("config_change", "maintainer", "dashboard",
                         summary=(f"limits → substantive {detail['substantive']}, "
                                  f"light {detail['light']}, work queue {detail['proactive']}"),
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
                return self._json(409, {"error": busy_error("Try again") or "steward busy"})
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
                return self._json(409, {"error": busy_error("Try again") or "steward busy"})
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
                latest = latest_review_record(item)
                for action in item.get("staged_actions", []):
                    if action.get("executed_at") or action.get("superseded_at"):
                        continue
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    # A review staged for a head the PR has since moved past
                    # (a bot re-pin, a rebase) was replaced by a newer record.
                    # Posting it would review the wrong commit; skip it, and
                    # never let it block the review and merge that are current.
                    link = action.get("review_record_id")
                    if (link and latest.get("id") and link != latest["id"]
                            and "review" in action.get("kind", "")):
                        action["superseded_at"] = now
                        details.append(f"skipped review staged for an older head ({link})")
                        continue
                    ok, detail = prepare_review_record(item, action, now)
                    if not ok:
                        details.append(detail)
                        item_ok = False
                        continue
                    if detail:
                        details.append(detail)
                    # The canonical record must reach disk before the network
                    # post. A crash can lose the outcome marker, never the
                    # judgment that was sent.
                    if item.get("type") == "pr" and "review" in action.get("kind", ""):
                        ledger_path.write_text(json.dumps(ledger, indent=2))
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
                        records = item.get("review_records")
                        record = records[-1] if isinstance(records, list) and records else None
                        if isinstance(record, dict) and action.get("review_record_id") == record.get("id"):
                            record["posted_at"] = action["executed_at"]
                    item_ok = item_ok and ok
                # Approving an approve-recommend PR is the maintainer's final
                # look: after the review is up (this click or a previous live
                # post), merge on their behalf. Key off the verdict, not off a
                # staged approve action: a PR already approved on GitHub has no
                # review left to post and so carries zero staged actions — the
                # "awaiting your merge" posture, which must still merge.
                merged = False
                if item_ok and item.get("type") == "pr" and (
                        item.get("verdict") == "approve-recommend"
                        or any(a.get("kind") == "pr_review_approve"
                               for a in item.get("staged_actions", []))):
                    if not details:
                        details.append("no staged actions; merging on verdict")
                    ok, detail = merge_pr(full, number)
                    details.append(detail)
                    # merge_pr succeeds both when GitHub merged and when it only
                    # queued auto-merge behind refreshed checks. Only the first
                    # is merged; the item stays Ready until GitHub says so.
                    queued = ok and "auto-merge queued" in detail
                    merged = ok and not queued
                    item_ok = item_ok and ok
                else:
                    queued = False
                stamp_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if item_ok and queued:
                    item["merge_queued_at"] = stamp_now
                    item["last_action"] = "approved by maintainer via dashboard; GitHub auto-merge queued"
                    item["last_action_at"] = stamp_now
                elif item_ok:
                    item.pop("merge_queued_at", None)
                    item["status"] = "done" if merged else "posted"
                    item["last_action"] = ("approved & merged by maintainer via dashboard"
                                           if merged else "approved by maintainer via dashboard; posted")
                    item["last_action_at"] = stamp_now
                outcomes[key] = {"ok": item_ok, "merged": merged, "queued": queued,
                                 "detail": "; ".join(details)}
                audit_record = latest_review_record(item)
                audit.append("approve", "maintainer", "dashboard", repo=short,
                             ref=key, ok=item_ok, detail="; ".join(details),
                             summary="approved via dashboard" + (" & merged" if merged else
                                                                 " & merge queued" if queued else ""),
                             data={"merged": merged, "queued": queued,
                                   "review_record_id": audit_record.get("id"),
                                   "review_integrity": audit_record.get("integrity", "canonical")})
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
