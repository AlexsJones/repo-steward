#!/usr/bin/env python3
"""Squash-merge PRs the steward has signed off as "Good to merge".

The maintainer's standing rule (decisions.jsonl, 2026-09-25): a PR merges when

  - the steward account's latest review is APPROVED, sits on the PR's current
    head, and says "good to merge";
  - GitHub's review decision is APPROVED (nobody has requested changes since);
  - every check on the head has completed green and GitHub reports it CLEAN;
  - it is not a critical feature change: no `feat` or breaking (`!`) title,
    and no major-version dependency bump.

No engine, no tokens: gh only. Dry run by default. Eligible PRs form a queue,
oldest steward sign-off first; --limit caps how many one run acts on, and the
rest wait for the next run.

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
CRITICAL_TITLE = re.compile(r"^\s*(feat\b|\w+(\([^)]*\))?!:)", re.I)
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
    if CRITICAL_TITLE.search(title):
        return False, "feature or breaking change: stays with the maintainer"
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
    return True, f"steward approved at {pr['headRefOid'][:8]}, checks green, not a feature change"


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


def squash(pr, full, updates, auto_merge):
    """Squash-merge now, or for a branch behind a strict-protection base, bring
    it up to date: GitHub squashes it once the refreshed checks pass where the
    repo allows auto-merge, else the next run does (the approval on the
    pre-update head is what this rule vetted). Returns (ok, merged, detail)."""
    num, head = str(pr["number"]), pr["headRefOid"]
    if pr["mergeStateStatus"] != "BEHIND":
        # --match-head-commit: GitHub refuses if someone pushed after the check.
        ok, out = gh(["pr", "merge", num, "-R", full, "--squash", "--match-head-commit", head])
        if ok or not BEHIND_REFUSAL.search(out):
            return ok, ok, out or "merged"
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


def collect(steward, updates):
    """Every open PR carrying a "good to merge" review, as (eligible, skipped):
    eligible entries are (approved_at, repo, pr, reason), oldest first."""
    eligible_prs, skipped = [], []
    for repo in server.repos_config():
        full = repo["name"]
        # Reviews only: asking for check rollups across a busy repo's whole
        # open list makes GitHub's GraphQL endpoint 502.
        ok, out = gh(["pr", "list", "-R", full, "--state", "open", "-L", "200",
                      "--json", "number,reviews"])
        if not ok:
            print(f"{full}: gh pr list failed: {out[:200]}")
            continue
        for listed in json.loads(out):
            if not any(GOOD_TO_MERGE.search(r.get("body") or "") for r in listed.get("reviews") or []):
                continue
            ok, out = gh(["pr", "view", str(listed["number"]), "-R", full, "--json", FIELDS])
            if not ok:
                print(f"{full}#{listed['number']}: gh pr view failed: {out[:200]}")
                continue
            pr = json.loads(out)
            go, reason = eligible(pr, steward, full, updates)
            if go:
                eligible_prs.append((approved_at(pr, steward), full, pr, reason))
            else:
                skipped.append((full, pr, reason))
    # Oldest sign-off first: an approval nobody has merged has waited longest.
    eligible_prs.sort(key=lambda e: e[0])
    return eligible_prs, skipped


def entry(full, pr, detail, approved=None):
    out = {"repo": full, "number": pr["number"], "title": pr.get("title", ""),
           "url": f"https://github.com/{full}/pull/{pr['number']}", "detail": detail}
    if approved is not None:
        out["approved_at"] = approved
    return out


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
    candidates, skipped = collect(steward, updates)
    for full, pr, reason in skipped:
        print(f"skip  {full}#{pr['number']}  {pr['title'][:60]}  ({reason})")

    result = {"merged": [], "queued": [], "failed": [], "waiting": [],
              "skipped": [entry(full, pr, reason) for full, pr, reason in skipped]}
    acted = 0
    auto_merge = {}
    for approved, full, pr, reason in candidates:
        ref = f"{full}#{pr['number']}"
        behind = " (behind base: update, then auto-merge)" if pr["mergeStateStatus"] == "BEHIND" else ""
        if not args.merge or (limit is not None and acted >= limit):
            word = "would" if not args.merge else "wait "
            print(f"{word} {ref}  {pr['title'][:60]}{behind}")
            result["waiting"].append(entry(full, pr, reason + behind, approved))
            continue
        state = refresh_state(pr, full)
        if state not in ("CLEAN", "BEHIND"):
            # Typically a conflict an earlier merge this run introduced: report
            # it without spending a merge slot on an attempt GitHub will refuse.
            detail = f"merge state became {state} before this run reached it"
            print(f"FAILED {ref}  {pr['title'][:60]}  {detail}")
            result["failed"].append(entry(full, pr, detail, approved))
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
        with open(ROOT / "approvals.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "repo": full, "action": "good-to-merge",
                                "item": f"pr-{pr['number']}", "ok": ok,
                                "detail": f"squash: {detail}"[:300],
                                "reason": reason}) + "\n")
        audit.append("terminal", "maintainer", "merge_ready", repo=full.split("/")[1],
                     ref=f"pr-{pr['number']}", ok=ok, detail=detail,
                     summary=f"good-to-merge rule: squash-merge pr #{pr['number']} — {reason}",
                     data={"action": "merge", "method": "squash",
                           "head": pr["headRefOid"], "rule": "good-to-merge",
                           "trigger": "tick" if args.tick else "manual"})
    if args.merge:
        print(f"{len(result['merged'])} merged, {len(result['queued'])} queued, "
              f"{len(result['failed'])} failed, {len(result['waiting'])} waiting for a later run")
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
