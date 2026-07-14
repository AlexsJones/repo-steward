<p align="center">
  <img src="assets/logo.svg" width="128" alt="Repo Steward — two interlinked commit rings">
</p>

# Repo Steward

> **An autonomous agent for open-source repository management.**

[![License: MIT](https://img.shields.io/badge/License-MIT-2ea44f.svg)](LICENSE)
[![Engine: Claude Code](https://img.shields.io/badge/engine-Claude%20Code%20%7C%20Codex%20%7C%20Gemini%20%7C%20opencode-1d6e62.svg)](#ai-backends)

Repo Steward is an agent that runs the operational side of maintaining
open-source repositories — triaging issues, reviewing pull requests across
multiple iterations, joining repository discussions, authoring bug-fix PRs, and
watching your project websites
— on a schedule or a button press, keeping a live dashboard of what's happening
and escalating only tie-breaks and design decisions to you.

Built for the maintainer whose day disappears into pasting PR diffs into a
chat window: the steward does that loop autonomously, across every repository
you give it, and shows its work.

- **Draft mode by default** — every review and reply is staged for your
  approval until you flip the live toggle; nothing speaks for you until it has
  earned it.
- **You are the terminal state** — the steward never merges or closes on its
  own judgment. Your dashboard click or typed decision is what merges,
  executed under *your* GitHub auth because *you* acted.
- **Decide in a sentence** — type a free-text decision on any escalation and
  press Enter; a focused executor interprets it and carries it out.
- **Shows its work honestly** — progress and ETAs derive from artifacts on
  disk and GitHub facts, never from the model's self-reporting; plus per-repo
  queues, staged action texts, token/cost metrics, trends, and uptime cards.

<p align="center">
  <img src="assets/example.png" width="900" alt="The Repo Steward dashboard: decisions needed, PRs ready for final look, fleet overview, and the next-tick queue">
</p>

## Quick links

| Get running | Use it daily | Understand it | Operate it |
|---|---|---|---|
| [Requirements](#requirements) | [The dashboard](#the-dashboard) | [How it works](#how-it-works) | [Operating it](#operating-it) |
| [Install](#install) | [Decisions & approvals](#decisions--approvals) | [Guardrails](#guardrails) | [Metrics](#metrics) |
| [AI backends](#ai-backends) | [Site uptime](#site-uptime) | [Files](#files) | [Costs & cadence](#costs--cadence) |
| | | [Why not an agent harness?](#why-not-a-general-purpose-agent-harness) | |

---

## Get running

### Requirements

- A headless agent CLI (see [backends](#ai-backends); default
  [Claude Code](https://claude.com/claude-code)), authenticated
- [gh](https://cli.github.com/) CLI, authenticated with push access to your repos
- Linux with a systemd user session, `python3`, `jq`

### Install

```bash
git clone https://github.com/<you>/repo-steward && cd repo-steward
cp config.example.yaml config.yaml   # edit: your repos, signature, limits
./install.sh                         # or --no-timer to only tick manually
```

Then either wait for the first scheduled tick or start one now:

```bash
make tick    # run one tick now
make logs    # follow it
```

`make help` lists every verb (`serve`, `start`, `status`, `open`,
`timer-on/off`, `uninstall`, …) — the friendly front door to the systemd units.

Open **http://localhost:8377/** — the dashboard shows decisions needing you,
staged actions with full text, per-repo queues, and (after a few ticks)
trend lines. It auto-refreshes; from other devices on your network use
`http://<host-ip>:8377/` (open the port in your firewall if needed).

Pin a model or change cadence via env at install time:

```bash
# every half hour on a strong model
STEWARD_MODEL=claude-opus-4-8 STEWARD_CADENCE="*-*-* *:07,37:00" ./install.sh
# or one bigger tick each morning (raise `limits` in config.yaml to match)
STEWARD_CADENCE="*-*-* 07:00:00" ./install.sh
```

### AI backends

The tick is an *agentic session* — it runs `gh`, edits ledgers, writes files —
so backends are headless coding-agent CLIs, selected at install time:

```bash
./install.sh                                            # Claude Code (default)
STEWARD_ENGINE=codex ./install.sh                       # OpenAI Codex CLI
STEWARD_ENGINE=gemini ./install.sh                      # Gemini CLI
STEWARD_ENGINE=opencode STEWARD_MODEL=ollama/qwen3 ./install.sh   # local models
STEWARD_ENGINE=custom STEWARD_ENGINE_CMD='my-agent --prompt "$PROMPT"' ./install.sh
```

- **Local / OpenAI-compatible providers** come in two flavors: run
  [opencode](https://opencode.ai) against Ollama/LM Studio/any provider it
  supports, or keep the Claude Code engine and point it at a proxy
  (`ANTHROPIC_BASE_URL` + [LiteLLM](https://github.com/BerriAI/litellm) routes
  to OpenAI, Bedrock, Vertex, or local models without any steward changes).
- **Caveats for non-Claude engines**: the merge/close/force-push *permission
  deny layer* ships as `.claude/settings.json`, which only Claude Code
  enforces — on other engines the playbook's guardrails still instruct, but
  nothing mechanically blocks; configure your engine's own sandbox/approval
  settings accordingly. Token/cost capture in `usage.jsonl` is currently
  Claude-only (other engines don't emit a usage envelope headlessly); the
  metrics page degrades gracefully. Engines other than Claude Code are
  lightly tested — reports and PRs welcome.

---

## Use it daily

### The dashboard

- **Run tick now** — starts a tick on demand (refused while one is running).
  The progress bar is *deterministic*: it counts chunks the tick provably
  completed (repo ledgers, metrics, dashboard writes — file mtimes, never the
  model's self-reported position), and the ETA is the median of real per-chunk
  timings from past ticks (`timings.jsonl`).
- **Decisions needed** — each escalation carries a text box: type what you
  want done, press Enter. See [Decisions & approvals](#decisions--approvals).
- **Ready for your final look** — the recommend-to-merge shortlist. Rows are
  live-checked against GitHub: one you merged/closed yourself drops out, and
  one whose approval is already posted at the PR's current head shows its age
  ("✓ approved on GitHub · 5d ago"). **✓ Approve & merge** posts the staged
  review (if still unposted) and merges the PR — your final look is the
  terminal decision.
- **Next tick** — the steward's plan: what it intends to do next tick and why
  each item is queued, including unfinished conversations it's waiting on.
  **Activity & trends** is the matching backward view — what it actually did
  last tick. Together they replace the old in-flight table.
- **👁 Watch** — per-repo matrix of what the steward tracks (issues / PRs /
  discussions) and each repo's priority; saves back to the `watch:` lists in
  `config.yaml`, applies next tick.
- **Mode toggle** — flip draft ⇄ live (rewrites `config.yaml`).
- **Schedule** — `Manual only / Hourly / Every 6h / Daily / Weekly`;
  live-configures the systemd timer. Ticks stay button-triggerable at any
  cadence.
- **⚙ Tick size** — the per-run work caps (substantive + light items). Raise
  for a bigger daily sweep, lower for cheaper, more frequent ticks. Applies to
  the next tick, so it's safe to change while one is running.

Buttons appear only when the page is served by `server.py`; static copies of
the dashboard are read-only.

### Decisions & approvals

Everything that touches GitHub under your name happens because you acted, and
every action lands in the `approvals.jsonl` audit trail:

- **Approve & merge** (Ready table) — executes the staged review via `gh`
  under your auth, then merges (method from `merge_method:` in config, else
  the first the repo allows: squash → merge → rebase). Works even in draft
  mode: clicking is you acting.
- **Typed decisions** (Decisions section) — type e.g. *"go with #650, close
  #651 as superseded"* and press Enter. The server records it to
  `decisions.jsonl` and runs `decide.sh`: a focused engine session that
  interprets your text and carries it out — comments, labels, ledger updates.
  Explicit merge/close instructions are executed by the server itself (the
  engine session stays mechanically denied those verbs; it *requests* them
  from `/api/terminal`, which only answers while a decision executor is
  running). If your text is too ambiguous to act on safely, the entry comes
  back asking for clarification instead of guessing. Decisions typed while a
  tick runs queue and drain as soon as the steward is free.
- **Dismiss** — drops a staged item without posting; recorded like everything
  else.

### The decision log

`audit.jsonl` is the append-only, serialisable record of everything anyone
decided or did — one JSON event per line, unified across every channel:
your dashboard clicks (approve/dismiss), typed decisions and their
executions, explicit merges/closes, config changes, tick runs, and the
steward's own actions (staged reviews, live posts, fix PRs, escalations,
observed outcomes). Steward events are written as structured lines to
`activity.jsonl` during a run and folded in when it ends, so the activity
you see on the dashboard is rendered from the same serialisable events the
log keeps forever.

Schema and event catalogue live in `audit.py`. Read it with
`make audit` (last events, pretty), `GET /api/audit?repo=&event=&limit=`,
or any jq one-liner — e.g. every terminal action ever taken:

```bash
jq -r 'select(.event=="terminal") | "\(.ts) \(.repo) \(.ref): \(.summary)"' audit.jsonl
```

An install that predates the log migrates its whole history once with
`make audit-backfill` — it converts `approvals.jsonl`, `decisions.jsonl`,
and `usage.jsonl` into the same format, idempotently (re-running never
duplicates an event).

### Metrics

**http://localhost:8377/metrics.html** tracks the steward itself:

- **Tokens & cost per tick** — every tick runs through `tick.sh`, which
  captures the Claude Code usage envelope (input/output/cache tokens, cost,
  duration) into `usage.jsonl`; decision-executor runs are captured too,
  tagged separately so they don't skew tick stats.
- **Attention by repo** — cumulative steward actions per repo, the proxy for
  where the steward's effort goes (token usage is measured per tick, not per
  repo — one session works all repos).
- **Per-repo trends** — open issues/PRs over time from `metrics.jsonl`
  snapshots, plus a Δ-since-baseline table, so you can see which repos are
  heating up and whether the backlog is actually shrinking.

### Site uptime

Add a `sites:` block to config.yaml (see the example) and the installer
enables a token-free probe (`uptime_check.py`, every 5 minutes). Sites get
live status chips on the dashboard and 24h-uptime/latency cards on the
metrics page. A site is declared down after two consecutive failed probes;
the transition is logged to `incidents.jsonl` and escalated, and the next
steward tick investigates the linked repo (recent commits, failed deploy
workflows) — probes cost nothing, tokens are only spent when something
actually breaks.

---

## Understand it

### How it works

```
systemd timer (hourly)                      you, on the dashboard
        │                                   type a decision ⏎ / ✓ approve & merge
        ▼                                                │
tick.sh ── drains typed decisions first                  ▼
        │                                   server.py (systemd, port 8377)
        ▼                                   records → decisions.jsonl
claude -p "execute one steward tick"        spawns decide.sh when idle
        │                                   executes gh actions under YOUR auth
        ├─ sync: gh polls each repo since last cursor
        ├─ triage new issues → classify, label, draft substantive replies
        ├─ join discussions → draft replies to unanswered threads (GraphQL)
        ├─ review PRs → verdicts: approve-recommend / iterate / escalate
        ├─ delta re-review PRs whose authors pushed since last review
        ├─ author fix PRs for confirmed bugs (own clones, tests included)
        ├─ escalate tie-breaks to escalations.md — never blocks on them
        └─ write ledgers + metrics, regenerate dashboard.html
```

There is no daemon and no database: continuity comes from plain JSON ledgers
in `state/`, so every tick is a fresh, stateless session that picks up exactly
where the last one stopped. Everything is inspectable and editable with a text
editor.

### Guardrails

- **The steward never merges, closes, or force-pushes on its own judgment.**
  Terminal states belong to you — reached only through your ✓ Approve & merge
  click or an explicit typed decision, both executed by `server.py` under your
  auth and logged to `approvals.jsonl`. For the agent sessions themselves the
  verbs are denied at the Claude Code permission layer
  (`.claude/settings.json`), not just in the prompt.
- **Draft mode first.** Out of the box, nothing is posted to GitHub — every
  would-be review/reply is staged on the dashboard so you can calibrate the
  steward's judgment before it speaks on your repos. Go live with the mode
  toggle on the dashboard (or edit `mode:` in `config.yaml` — same thing).
- **Untrusted-content aware.** Issue, PR, and discussion bodies are treated as data; the
  playbook instructs the steward to ignore embedded instructions and flag
  manipulation attempts. Contributor code is never executed on your shell.
  Typed dashboard decisions are the one *trusted* text channel — they come
  from you, on localhost.
- **Signed output.** In live mode every posted comment carries a signature
  from your config, so bot output is always auditable.
- **Bounded ticks.** Work per tick is capped (`limits` in config); a large
  backlog drains over days instead of producing one enormous, unreviewable burst.

### Files

The program is a handful of tracked files at the repo root; everything a
running install generates is gitignored (per-maintainer state). The tracked set:

| path | what | in git? |
|---|---|---|
| `STEWARD.md` | the tick playbook the agent follows — edit to change behavior | yes |
| `server.py` | dashboard server + approve / decide / terminal / tick API | yes |
| `steward-controls.js` | dashboard buttons, decision boxes + repo filter lens | yes |
| `tick.sh` | headless-agent wrapper each tick runs through; captures usage + chunk timings | yes |
| `decide.sh` | the decision executor `server.py`/`tick.sh` spawn for typed decisions | yes |
| `audit.py` | decision-log schema, append/read helpers, history backfill | yes |
| `uptime_check.py` | token-free site probe the uptime timer runs | yes |
| `install.sh` | generates the systemd user units | yes |
| `Makefile` | convenience verbs over the units — `make help` | yes |
| `.claude/settings.json` | the merge / close / force-push permission deny layer | yes |
| `config.example.yaml` | starter config — copy to `config.yaml` | yes |

Generated per install, never committed:

| path | what | in git? |
|---|---|---|
| `config.yaml` | your repos, mode, limits, signature | no (yours) |
| `dashboard.html` | the live board — regenerated every tick | no |
| `state/<repo>.json` | per-repo ledger: every item's status, verdict, staged actions | no |
| `escalations.md` | decisions parked for you | no |
| `decisions.jsonl` | your typed decisions + their outcomes | no |
| `approvals.jsonl` | audit trail of every action taken under your auth | no |
| `audit.jsonl` · `activity.jsonl` | the unified decision log + the current run's slice of it | no |
| `metrics.jsonl` · `usage.jsonl` · `timings.jsonl` | snapshots, token/cost envelopes, per-chunk tick timings | no |
| `logs/tick.log` · `logs/decide.log` | full output of every tick / decision run | no |

### Why not a general-purpose agent harness?

A reasonable question: agent harnesses and orchestration frameworks (Hermes,
OpenClaw, Pi, and the growing rest) already give you scheduling, tool use,
memory, and multi-agent coordination. Why hand-roll systemd + `gh` + JSON
files instead of building on one?

Because for *this* job the harness is the part you'd spend your time fighting,
and the properties that matter here come from deliberately **not** having one:

- **The state is plain files, not a runtime.** Every tick is a stateless,
  resumable `claude -p` invocation; continuity lives entirely in
  `state/<repo>.json`, `metrics.jsonl`, and `escalations.md` — versioned,
  greppable, and editable with a text editor. There's no daemon holding
  in-memory state, no database to migrate, no orchestration server to keep
  alive. A harness adds a stateful layer you now have to run, observe, and
  trust; here, if the machine reboots mid-tick, the next tick just re-reads the
  cursor and continues.

- **The trust surface is small enough to read in an afternoon.** The whole
  system is a handful of readable files: one playbook, one ~600-line stdlib
  Python server, two bash wrappers, one uptime probe. For software that acts on
  your repos under your GitHub identity, "you can audit all of it" is a
  feature, not a limitation.

- **Guardrails sit at the OS boundary, not inside a framework's config.**
  "Never merge, close, or force-push" is a `gh` permission deny-list enforced
  by Claude Code's sandbox — not a prompt instruction or a policy plugin a
  harness update could quietly change. Fewer moving parts between the intent
  and the enforcement.

- **The product is the human in the loop, not autonomy.** Draft mode,
  approve-to-post, escalate-don't-decide — the design optimizes for doing
  *less* on its own until you say otherwise. Most harnesses optimize the
  opposite direction; you'd be turning features off.

- **No lock-in to one harness's abstractions.** The tick engine is already
  swappable (`claude` / `codex` / `gemini` / `opencode` / `custom`). If you
  *want* a harness, point `STEWARD_ENGINE=custom` at it and the steward's
  file-based contract still holds. This isn't anti-harness — it's
  harness-agnostic, with the orchestration kept boring on purpose.

The honest tradeoff: a real harness gives you sophisticated multi-agent
planning, shared memory, and a tool ecosystem this doesn't have. Repo Steward
is a *steward*, not a general agent — a narrow job with strong guarantees. When
the job needs a fleet of coordinating agents, reach for the harness. When it
needs to reliably keep your PRs moving without becoming another system to
operate, reach for this.

---

## Operate it

### Operating it

The `make` targets wrap the systemd user units — run `make help` for the list:

```bash
make status      # dashboard / tick / timer state at a glance
make tick        # one tick, now
make timer-off   # pause scheduled ticks (dashboard stays up)
make timer-on    # resume them
make logs        # tail the tick log
make uninstall   # disable and stop every steward unit
```

These are thin wrappers over `systemctl --user` against the `repo-steward*`
units — run those directly if you prefer (`journalctl --user -u
repo-steward.service` for full tick history). After `make uninstall`, delete the
`repo-steward*` unit files from `~/.config/systemd/user/` to remove them fully.

### Costs & cadence

Each tick is a headless Claude Code session doing real review work — budget
accordingly. The defaults (hourly, 4 substantive + 12 light items) suit an
actively maintained portfolio; quiet repos cost almost nothing since a
no-change tick exits after the sync. Typed decisions spawn small focused
sessions, tracked in the same usage ledger. Lengthen the cadence or shrink
`limits` for a lighter footprint.

## License

MIT
