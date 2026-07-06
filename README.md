# 🤝 Repo Steward

An autonomous maintainer's assistant for your open-source repos. A scheduled
[Claude Code](https://claude.com/claude-code) agent triages issues, reviews
pull requests across multiple iterations, authors bug-fix PRs, and keeps a
live dashboard of what's happening — escalating only tie-breaks and design
decisions to you.

Built for the maintainer whose day disappears into pasting PR diffs into a
chat window: the steward does that loop autonomously, across all your repos,
every hour, and shows its work.

## How it works

```
systemd timer (hourly)
        │
        ▼
claude -p "execute one steward tick"     ← headless Claude Code session
        │
        ├─ sync: gh polls each repo since last cursor
        ├─ triage new issues → classify, label, draft substantive replies
        ├─ review PRs → verdicts: approve-recommend / iterate / escalate
        ├─ delta re-review PRs whose authors pushed since last review
        ├─ author fix PRs for confirmed bugs (own clones, tests included)
        ├─ escalate tie-breaks to escalations.md — never blocks on them
        └─ write ledgers + metrics, regenerate dashboard.html
                                              │
                                              ▼
                            server.py (systemd, port 8377)
                            dashboard + Run-tick / Approve buttons
```

There is no daemon and no database: continuity comes from plain JSON ledgers
in `state/`, so every tick is a fresh, stateless session that picks up exactly
where the last one stopped. Everything is inspectable and editable with a text
editor.

## Guardrails

- **Never merges, closes, or force-pushes.** Terminal states belong to you.
  These are denied at the Claude Code permission layer
  (`.claude/settings.json`), not just in the prompt.
- **Draft mode first.** Out of the box, nothing is posted to GitHub — every
  would-be review/reply is staged on the dashboard so you can calibrate the
  steward's judgment before it speaks on your repos. Flip `mode: live` in
  `config.yaml` when ready.
- **Untrusted-content aware.** Issue/PR bodies are treated as data; the
  playbook instructs the steward to ignore embedded instructions and flag
  manipulation attempts. Contributor code is never executed on your shell.
- **Signed output.** In live mode every posted comment carries a signature
  from your config, so bot output is always auditable.
- **Bounded ticks.** Work per tick is capped (`limits` in config); a large
  backlog drains over days instead of producing one enormous, unreviewable burst.

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI, authenticated
- [gh](https://cli.github.com/) CLI, authenticated with push access to your repos
- Linux with a systemd user session, `python3`, `jq`

## Install

```bash
git clone https://github.com/<you>/repo-steward && cd repo-steward
cp config.example.yaml config.yaml   # edit: your repos, signature, limits
./install.sh                         # or --no-timer to only tick manually
```

Then either wait for the first scheduled tick or start one now:

```bash
systemctl --user start repo-steward.service
tail -f logs/tick.log
```

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

## Metrics

**http://localhost:8377/metrics.html** tracks the steward itself:

- **Tokens & cost per tick** — every tick runs through `tick.sh`, which
  captures the Claude Code usage envelope (input/output/cache tokens, cost,
  duration) into `usage.jsonl`.
- **Attention by repo** — cumulative steward actions per repo, the proxy for
  where the steward's effort goes (token usage is measured per tick, not per
  repo — one session works all repos).
- **Per-repo trends** — open issues/PRs over time from `metrics.jsonl`
  snapshots, plus a Δ-since-baseline table, so you can see which repos are
  heating up and whether the backlog is actually shrinking.

## Site uptime

Add a `sites:` block to config.yaml (see the example) and the installer
enables a token-free probe (`uptime_check.py`, every 5 minutes). Sites get
live status chips on the dashboard and 24h-uptime/latency cards on the
metrics page. A site is declared down after two consecutive failed probes;
the transition is logged to `incidents.jsonl` and escalated, and the next
steward tick investigates the linked repo (recent commits, failed deploy
workflows) — probes cost nothing, tokens are only spent when something
actually breaks.

## The dashboard buttons

- **Run tick now** — starts a tick on demand (refused while one is running).
- **Approve & post** — appears on each staged action. Executes it via `gh`
  under *your* auth — clicking is you acting, which is why it works even in
  draft mode. Executed actions are stamped and can never double-post; the
  audit trail is `approvals.jsonl`.

Buttons appear only when the page is served by `server.py`; static copies of
the dashboard are read-only.

## Files

| path | what | in git? |
|---|---|---|
| `STEWARD.md` | the tick playbook the agent follows — edit to change behavior | yes |
| `server.py` | dashboard server + approve/tick API | yes |
| `install.sh` | generates the systemd user units | yes |
| `config.yaml` | your repos, mode, limits, signature | no (yours) |
| `state/<repo>.json` | per-repo ledger: every item's status, verdict, staged actions | no |
| `escalations.md` | decisions parked for you | no |
| `metrics.jsonl` | one snapshot per repo per tick → trends | no |
| `logs/tick.log` | full output of every tick | no |

## Operating it

```bash
systemctl --user stop repo-steward.timer      # pause future ticks
systemctl --user start repo-steward.timer     # resume
systemctl --user start repo-steward.service   # one tick, now
journalctl --user -u repo-steward.service     # tick history
```

Uninstall: `systemctl --user disable --now repo-steward.timer repo-steward-dash.service`
and delete the three unit files from `~/.config/systemd/user/`.

## Costs & cadence

Each tick is a headless Claude Code session doing real review work — budget
accordingly. The defaults (hourly, 4 substantive + 12 light items) suit an
actively maintained portfolio; quiet repos cost almost nothing since a
no-change tick exits after the sync. Lengthen the cadence or shrink `limits`
for a lighter footprint.

## License

MIT
