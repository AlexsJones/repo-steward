#!/usr/bin/env python3
"""Squash-merge PRs the steward has signed off as "Good to merge".

The maintainer's standing rule (decisions.jsonl, 2026-09-25): a PR merges when

  - the steward account's latest review is APPROVED, sits on the PR's current
    head, and says "good to merge";
  - GitHub's review decision is APPROVED (nobody has requested changes since);
  - every check on the head has completed green and GitHub reports it CLEAN;
  - it is not a breaking change (`!` in the conventional-commit title) or a
    major-version dependency bump. A `feat` title is fine when the steward
    signed it off (decision 2026-10-24: the blanket feat exclusion was
    holding back translations and flags the steward had fully reviewed).

No engine, no tokens: gh only. Dry run by default. Eligible PRs form a queue,
oldest steward sign-off first; --limit caps how many one run acts on, and the
rest wait for the next run.

Two more buckets explain the approved-but-unmerged backlog without acting on it:

  blocked   steward-approved PRs whose CI run sits in GitHub's "awaiting
            approval" state (fork or bot PRs). Only the maintainer can approve
            that run; approve_workflows.sh does it in bulk.
  rereview  steward-approved PRs without a sign-off whose checks are now green
            and mergeable: the blocker the review cited has cleared, so the
            steward re-reviews them next tick and signs off if warranted.

  python3 merge_ready.py                    # list what would merge, and why others don't
  python3 merge_ready.py --merge            # squash-merge the eligible ones
  python3 merge_ready.py --merge --limit 5  # just the five longest-waiting
  python3 merge_ready.py --tick             # tick.sh's merge phase (see STEWARD.md)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import audit
import server

ROOT = Path(__file__).resolve().parent
GOOD_TO_MERGE = re.compile(r"good to merge", re.I)
BREAKING_TITLE = re.compile(r"^\s*\w+(\([^)]*\))?!:", re.I)
VERSION_BUMP = re.compile(r"\bfrom v?(\d+)\.\S*\s+to v?(\d+)\.", re.I)
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# Strict branch protection refuses a merge from a branch behind its base with
# this wording, even when the named check passed on the head.
BEHIND_REFUSAL = re.compile(r"Required status check .* is expected|not up to date", re.I)
# Heads this script created with update-branch, keyed "owner/repo#N" to the
# approved head they were built on: base catch-up, not new contributor code.
UPDATES = ROOT / ".merge_ready_updates.json"
FIELDS = ("number,title,isDraft,headRefOid,reviewDecision,mergeStateStatus,"
          "reviews,statusCheckRollup")


def gh(args):
    p = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=120)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr).strip()


def checks_green(rollup):
    if not rollup:
        return False, "no checks reported on the head"
    for c in rollup:
        if c.get("__typename") == "StatusContext":
            if c.get("state") != "SUCCESS":
                return False, f"status {c.get('context')} is {c.get('state')}"
        elif c.get("status") != "COMPLETED" or c.get("conclusion") not in GREEN:
            return False, f"check {c.get('name')} is {c.get('conclusion') or c.get('status')}"
    return True, ""


def load_updates():
    try:
        return json.loads(UPDATES.read_text())
    except (OSError, ValueError):
        return {}


def eligible(pr, steward, full, updates):
    """(True, reason) when the rule above allows a squash merge."""
    title = pr.get("title", "")
    if pr.get("isDraft"):
        return False, "draft"
    if BREAKING_TITLE.search(title):
        return False, "breaking change: stays with the maintainer"
    bump = VERSION_BUMP.search(title)
    if bump and bump.group(1) != bump.group(2):
        return False, f"major version bump {bump.group(1)} -> {bump.group(2)}"
    mine = [r for r in pr.get("reviews") or []
            if (r.get("author") or {}).get("login") == steward]
    if not mine:
        return False, "no steward review"
    last = mine[-1]
    if last.get("state") != "APPROVED" or not GOOD_TO_MERGE.search(last.get("body") or ""):
        return False, "latest steward review is not a \"good to merge\" approval"
    approved = (last.get("commit") or {}).get("oid")
    update = updates.get(f"{full}#{pr['number']}") or {}
    if approved != pr.get("headRefOid") and not (
            update.get("approved") == approved and update.get("head") == pr.get("headRefOid")):
        return False, "approval is on an older head"
    reviewed = approved or pr.get("headRefOid") or ""
    if pr.get("reviewDecision") != "APPROVED":
        return False, f"review decision is {pr.get('reviewDecision') or 'none'}"
    ok, why = checks_green(pr.get("statusCheckRollup"))
    if not ok:
        return False, why
    state = pr.get("mergeStateStatus")
    if state == "UNKNOWN":
        # The list endpoint reports mergeability lazily; a direct view asks
        # GitHub to compute it.
        ok, out = gh(["pr", "view", str(pr["number"]), "-R", full,
                      "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"])
        state = out if ok else state
    pr["mergeStateStatus"] = state
    if state not in ("CLEAN", "BEHIND"):
        return False, f"merge state is {state}"
    return True, f"steward approved at {reviewed[:8]}, checks green, not a breaking change"


def refresh_state(pr, full):
    """Re-read mergeStateStatus just before acting. The list read at the start
    of the run goes stale once an earlier merge moves the base: under strict
    protection the rest of that repo's queue turns BEHIND, and GitHub reports
    UNKNOWN for a moment while it recomputes."""
    for _ in range(5):
        ok, out = gh(["pr", "view", str(pr["number"]), "-R", full,
                      "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"])
        if ok and out != "UNKNOWN":
            pr["mergeStateStatus"] = out
            break
        time.sleep(2)
    return pr["mergeStateStatus"]


def confirmed_merge(num, full):
    """Whether GitHub reports the PR merged, plus the squash commit.

    `gh pr view` has no `merged` field. `state` is `MERGED` and `mergedAt`
    is set once the squash lands. A successful `gh pr merge` is not enough:
    that command also returns success for a branch update.
    """
    ok, out = gh(["pr", "view", str(num), "-R", full, "--json", "state,mergedAt,mergeCommit"])
    if not ok:
        return False, ""
    try:
        data = json.loads(out)
    except ValueError:
        return False, ""
    merged = data.get("state") == "MERGED" or bool(data.get("mergedAt"))
    if not merged:
        return False, ""
    return True, (data.get("mergeCommit") or {}).get("oid") or ""


def squash(pr, full, updates, auto_merge):
    """Squash-merge now, or for a branch behind a strict-protection base, bring
    it up to date: GitHub squashes it once the refreshed checks pass where the
    repo allows auto-merge, else the next run does (the approval on the
    pre-update head is what this rule vetted). Returns (ok, merged, detail).

    `merged` is true only after GitHub reports the PR merged.
    """
    num, head = str(pr["number"]), pr["headRefOid"]
    if pr["mergeStateStatus"] != "BEHIND":
        # --match-head-commit: GitHub refuses if someone pushed after the check.
        ok, out = gh(["pr", "merge", num, "-R", full, "--squash", "--match-head-commit", head])
        if ok:
            merged, merge_commit = confirmed_merge(num, full)
            if merged:
                pr["_merge_commit"] = merge_commit
                return True, True, out or "merged"
            return False, False, "merge command succeeded but the PR state is not MERGED"
        if not BEHIND_REFUSAL.search(out):
            return False, False, out or "merge failed"
        # The base moved after refresh_state looked: treat it as BEHIND.
    ok, out = gh(["pr", "update-branch", num, "-R", full])
    if not ok:
        return False, False, f"behind base; update-branch failed: {out}"
    # update-branch returns before the PR's head moves; wait for the new commit.
    for _ in range(20):
        ok, new_head = gh(["pr", "view", num, "-R", full, "--json", "headRefOid", "--jq", ".headRefOid"])
        if ok and new_head != head:
            break
        time.sleep(3)
    if ok and new_head != head:
        key = f"{full}#{num}"
        approved = updates.get(key, {}).get("approved", head) if updates.get(key, {}).get("head") == head else head
        updates[key] = {"approved": approved, "head": new_head}
        UPDATES.write_text(json.dumps(updates, indent=2) + "\n")
    if not auto_merge:
        return True, False, "branch updated; merges on the next run once checks pass"
    ok, out = gh(["pr", "merge", num, "-R", full, "--squash", "--auto"])
    return ok, False, ("branch updated; auto-merge queued" if ok
                       else f"branch updated; auto-merge failed: {out}")


def merges_per_tick(default=30):
    """limits.merges_per_tick from config.yaml; 0 turns the tick's merge
    queue off."""
    m = re.search(r"^\s*merges_per_tick:\s*(\d+)", (ROOT / "config.yaml").read_text(), re.M)
    return int(m.group(1)) if m else default


def approved_at(pr, steward):
    """When the steward's latest review was submitted: the queue's order."""
    mine = [r for r in pr.get("reviews") or []
            if (r.get("author") or {}).get("login") == steward]
    return (mine[-1].get("submittedAt") or "") if mine else ""


def steward_latest(pr, steward):
    mine = [r for r in pr.get("reviews") or []
            if (r.get("author") or {}).get("login") == steward]
    return mine[-1] if mine else None


def awaiting_approval(full):
    """head sha -> workflow names whose run GitHub holds for maintainer
    approval (first-time fork contributors, bot-opened PRs)."""
    ok, out = gh(["api", f"repos/{full}/actions/runs?status=action_required&per_page=100",
                  "--jq", "[.workflow_runs[] | {sha: .head_sha, name: .name}]"])
    heads = {}
    if ok:
        try:
            for run in json.loads(out):
                heads.setdefault(run["sha"], []).append(run["name"])
        except (ValueError, KeyError, TypeError):
            pass
    return heads


def collect(steward, updates):
    """Every open PR the steward approved, sorted into buckets:
    eligible entries are (approved_at, repo, pr, reason), oldest first;
    skipped, blocked and rereview entries are (repo, pr, reason)."""
    eligible_prs, skipped, blocked, rereview = [], [], [], []
    for repo in server.repos_config():
        full = repo["name"]
        # Reviews only: asking for check rollups across a busy repo's whole
        # open list makes GitHub's GraphQL endpoint 502.
        ok, out = gh(["pr", "list", "-R", full, "--state", "open", "-L", "200",
                      "--json", "number,reviews"])
        if not ok:
            print(f"{full}: gh pr list failed: {out[:200]}")
            continue
        held = None
        for listed in json.loads(out):
            signed = any(GOOD_TO_MERGE.search(r.get("body") or "") for r in listed.get("reviews") or [])
            last = steward_latest(listed, steward)
            approved = bool(last) and last.get("state") == "APPROVED"
            if not signed and not approved:
                continue
            ok, out = gh(["pr", "view", str(listed["number"]), "-R", full, "--json", FIELDS])
            if not ok:
                print(f"{full}#{listed['number']}: gh pr view failed: {out[:200]}")
                continue
            pr = json.loads(out)
            if held is None:
                held = awaiting_approval(full)
            waiting_runs = held.get(pr.get("headRefOid") or "", [])
            if signed:
                go, reason = eligible(pr, steward, full, updates)
                if go:
                    eligible_prs.append((approved_at(pr, steward), full, pr, reason))
                elif waiting_runs and not pr.get("isDraft"):
                    blocked.append((full, pr, "CI run awaiting your approval: " + ", ".join(waiting_runs)))
                else:
                    skipped.append((full, pr, reason))
                continue
            # Approved, no sign-off: the review named something that had to
            # clear first. Report whether it has.
            if pr.get("isDraft") or (last.get("commit") or {}).get("oid") != pr.get("headRefOid"):
                continue
            if waiting_runs:
                blocked.append((full, pr, "CI run awaiting your approval: " + ", ".join(waiting_runs)))
                continue
            bump = VERSION_BUMP.search(pr.get("title", ""))
            if BREAKING_TITLE.search(pr.get("title", "")) or (bump and bump.group(1) != bump.group(2)):
                continue
            green, _ = checks_green(pr.get("statusCheckRollup"))
            if green and pr.get("reviewDecision") == "APPROVED" \
                    and pr.get("mergeStateStatus") in ("CLEAN", "BEHIND", "UNKNOWN"):
                rereview.append((full, pr, "approved without sign-off; checks are green now"))
    # Oldest sign-off first: an approval nobody has merged has waited longest.
    eligible_prs.sort(key=lambda e: e[0])
    return eligible_prs, skipped, blocked, rereview


def entry(full, pr, detail, approved=None):
    out = {"repo": full, "number": pr["number"], "title": pr.get("title", ""),
           "url": f"https://github.com/{full}/pull/{pr['number']}", "detail": detail}
    if approved is not None:
        out["approved_at"] = approved
    return out


def reviewed_sha(pr, full, updates):
    """The commit the steward's sign-off sits on, not a later update-branch head."""
    key = f"{full}#{pr['number']}"
    update = updates.get(key) or {}
    if update.get("head") == pr.get("headRefOid") and update.get("approved"):
        return update["approved"]
    for review in reversed(pr.get("reviews") or []):
        if GOOD_TO_MERGE.search(review.get("body") or ""):
            oid = (review.get("commit") or {}).get("oid")
            if oid:
                return oid
    return pr.get("headRefOid") or ""


def changed_files(full, number):
    """Paths in the PR diff, or None when GitHub does not answer."""
    ok, out = gh(["pr", "diff", str(number), "-R", full, "--name-only"])
    if not ok:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def overlap_hold(full, number, paths, claimed):
    """Number of an earlier PR this run already claimed that shares a path."""
    if not paths:
        return None
    for other, other_paths in claimed.get(full, []):
        if set(paths) & set(other_paths):
            return other
    return None


def action_kind(done, detail):
    if done:
        return "merge"
    if "auto-merge" in detail:
        return "auto-merge"
    if "update-branch" in detail or detail.startswith("branch updated"):
        return "update-branch"
    return "merge"


def read_jsonl(path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def prior_failure_detail(rows, full, number):
    """Detail of the latest good-to-merge attempt when that attempt failed."""
    item = f"pr-{number}"
    mine = [row for row in rows if row.get("repo") == full and row.get("item") == item
            and row.get("action") == "good-to-merge"]
    if not mine or mine[-1].get("ok") is not False:
        return None
    return mine[-1].get("detail") or ""


def remember_stuck(result, prior_attempts, full, pr, detail, approved):
    """A second straight failure is listed for the next tick to escalate once."""
    previous = prior_failure_detail(prior_attempts, full, pr["number"])
    if previous is None:
        return
    stuck = entry(full, pr,
                  f"second consecutive failure: {detail[:160]}; previous: {previous[:160]}",
                  approved)
    result["stuck"].append(stuck)
    print(f"STUCK {full}#{pr['number']}  {stuck['detail'][:200]}")


def record_attempt(full, pr, reason, ok, done, detail, updates, trigger, files):
    """Append the approvals row and an audit event.

    A confirmed merge is the only `terminal` event. A branch update records
    `ok` for the update itself and must not be read as a merge.
    """
    kind = action_kind(done, detail)
    approved = reviewed_sha(pr, full, updates)
    head = pr.get("headRefOid") or ""
    # update-branch mutates GitHub's head, which the caller may still hold
    # as the pre-update object. The updates map has the new commit.
    mapped = updates.get(f"{full}#{pr['number']}") or {}
    if mapped.get("approved") == approved and mapped.get("head"):
        head = mapped["head"]
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "repo": full, "action": "good-to-merge", "item": f"pr-{pr['number']}",
               "ok": ok, "merged": bool(done), "detail": f"{kind}: {detail}"[:300],
               "reason": reason}
    with open(ROOT / "approvals.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
    data = {"action": kind, "method": "squash", "approved_head": approved, "head": head,
            "rule": "good-to-merge", "trigger": trigger}
    merge_commit = pr.get("_merge_commit") or ""
    if merge_commit:
        data["merge_commit"] = merge_commit
    if done:
        summary = f"good-to-merge rule: squash-merge pr #{pr['number']} — {reason}"
        event = "terminal"
    else:
        summary = f"good-to-merge rule: {kind} pr #{pr['number']} — {reason}"
        event = "merge_attempt"
    audit.append(event, "maintainer", "merge_ready", repo=full.split("/")[1],
                 ref=f"pr-{pr['number']}", ok=ok, detail=detail, summary=summary, data=data)
    if done:
        row = {"v": 1, "ts": payload["ts"], "repo": full, "number": pr["number"],
               "approved_head": approved, "head": head, "files": files or []}
        if merge_commit:
            row["merge_commit"] = merge_commit
        with (ROOT / "merges.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--merge", action="store_true", help="squash-merge eligible PRs")
    ap.add_argument("--limit", type=int, default=None,
                    help="act on at most N eligible PRs, oldest sign-off first")
    ap.add_argument("--tick", action="store_true",
                    help="run as tick.sh's merge phase: cap at limits.merges_per_tick, "
                         "merge only in live mode, write merge_queue.json")
    args = ap.parse_args()

    limit = args.limit
    if args.tick and not os.environ.get("STEWARD_RUNNING_TICK"):
        sys.exit("--tick is tick.sh's merge phase; run with --merge [--limit N] by hand")
    if args.tick:
        if limit is None:
            limit = merges_per_tick()
        # Draft mode posts nothing to GitHub, merges included: report the
        # queue, act on none of it.
        args.merge = args.merge or server.steward_mode() == "live"
        if limit == 0:
            args.merge = False
    elif args.merge and (server.tick_active() or server.decide_active()):
        sys.exit("steward busy (tick or decision run in progress): try again when it finishes")
    ok, steward = gh(["api", "user", "--jq", ".login"])
    if not ok:
        sys.exit(f"gh auth failed: {steward}")

    updates = load_updates()
    prior_attempts = read_jsonl(ROOT / "approvals.jsonl")
    candidates, skipped, blocked, rereview = collect(steward, updates)
    for full, pr, reason in skipped:
        print(f"skip  {full}#{pr['number']}  {pr['title'][:60]}  ({reason})")
    for full, pr, reason in blocked:
        print(f"BLOCKED {full}#{pr['number']}  {pr['title'][:60]}  ({reason})")
    for full, pr, reason in rereview:
        print(f"rereview {full}#{pr['number']}  {pr['title'][:60]}  ({reason})")

    result = {"merged": [], "queued": [], "failed": [], "waiting": [], "stuck": [],
              "skipped": [entry(full, pr, reason) for full, pr, reason in skipped],
              "blocked": [entry(full, pr, reason, approved_at(pr, steward)) for full, pr, reason in blocked],
              "rereview": [entry(full, pr, reason, approved_at(pr, steward)) for full, pr, reason in rereview]}
    acted = 0
    auto_merge = {}
    claimed = {}
    trigger = "tick" if args.tick else "manual"
    for approved, full, pr, reason in candidates:
        ref = f"{full}#{pr['number']}"
        behind = " (behind base: update, then auto-merge)" if pr["mergeStateStatus"] == "BEHIND" else ""
        if not args.merge or (limit is not None and acted >= limit):
            word = "would" if not args.merge else "wait "
            print(f"{word} {ref}  {pr['title'][:60]}{behind}")
            result["waiting"].append(entry(full, pr, reason + behind, approved))
            continue
        paths = changed_files(full, pr["number"])
        if paths is None:
            print(f"note  {ref}  changed files unavailable; overlap not checked")
        else:
            other = overlap_hold(full, pr["number"], paths, claimed)
            if other:
                detail = f"overlaps #{other}; held for the next tick"
                print(f"wait  {ref}  {pr['title'][:60]}  {detail}")
                result["waiting"].append(entry(full, pr, detail, approved))
                continue
            claimed.setdefault(full, []).append((pr["number"], paths))
        state = refresh_state(pr, full)
        if state not in ("CLEAN", "BEHIND"):
            # Typically a conflict an earlier merge this run introduced: report
            # it without spending a merge slot on an attempt GitHub will refuse.
            detail = f"merge state became {state} before this run reached it"
            print(f"FAILED {ref}  {pr['title'][:60]}  {detail}")
            result["failed"].append(entry(full, pr, detail, approved))
            record_attempt(full, pr, reason, False, False, detail, updates, trigger,
                           paths if paths else [])
            remember_stuck(result, prior_attempts, full, pr, detail, approved)
            continue
        if full not in auto_merge:
            ok, out = gh(["api", f"repos/{full}", "--jq", ".allow_auto_merge"])
            auto_merge[full] = ok and out == "true"
        ok, done, detail = squash(pr, full, updates, auto_merge[full])
        acted += 1
        label = "MERGED" if done else "QUEUED" if ok else "FAILED"
        print(f"{label} {ref}  {pr['title'][:60]}  {detail[:200]}")
        result["merged" if done else "queued" if ok else "failed"].append(
            entry(full, pr, detail[:300], approved))
        record_attempt(full, pr, reason, ok, done, detail, updates, trigger,
                       paths if paths else [])
        if not ok:
            remember_stuck(result, prior_attempts, full, pr, detail, approved)
    if args.merge:
        print(f"{len(result['merged'])} merged, {len(result['queued'])} queued, "
              f"{len(result['failed'])} failed, {len(result['waiting'])} waiting for a later run")
    if blocked:
        print(f"{len(blocked)} approved PRs wait on a CI run only you can approve: "
              f"./approve_workflows.sh lists them, --approve runs them")
    if rereview:
        print(f"{len(rereview)} approved PRs have gone green since their review: steward re-reviews next tick")
    if args.tick:
        snapshot = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "mode": server.steward_mode(), "limit": limit, "acting": args.merge,
                    **result}
        server.atomic_write_text(ROOT / "merge_queue.json", json.dumps(snapshot, indent=2) + "\n")
        # One machine-readable line for tick.sh's log and audit summary.
        print("SUMMARY " + json.dumps({k: (len(v) if isinstance(v, list) else v)
                                       for k, v in snapshot.items() if k != "ts"}))


if __name__ == "__main__":
    main()
