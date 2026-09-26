#!/usr/bin/env python3
"""Squash-merge PRs the steward has signed off as "Good to merge".

The maintainer's standing rule (decisions.jsonl, 2026-09-25): a PR merges when

  - the steward account's latest review is APPROVED, sits on the PR's current
    head, and says "good to merge";
  - GitHub's review decision is APPROVED (nobody has requested changes since);
  - every check on the head has completed green and GitHub reports it CLEAN;
  - it is not a critical feature change: no `feat` or breaking (`!`) title,
    and no major-version dependency bump.

No engine, no tokens: gh only. Dry run by default.

  python3 merge_ready.py            # list what would merge, and why others don't
  python3 merge_ready.py --merge    # squash-merge the eligible ones
"""
import argparse
import json
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


def squash(pr, full, updates, auto_merge):
    """Squash-merge now, or for a branch behind a strict-protection base, bring
    it up to date: GitHub squashes it once the refreshed checks pass where the
    repo allows auto-merge, else the next run does (the approval on the
    pre-update head is what this rule vetted). Returns (ok, merged, detail)."""
    num, head = str(pr["number"]), pr["headRefOid"]
    if pr["mergeStateStatus"] != "BEHIND":
        # --match-head-commit: GitHub refuses if someone pushed after the check.
        ok, out = gh(["pr", "merge", num, "-R", full, "--squash", "--match-head-commit", head])
        return ok, ok, out or "merged"
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--merge", action="store_true", help="squash-merge eligible PRs")
    args = ap.parse_args()

    if args.merge and (server.tick_active() or server.decide_active()):
        sys.exit("steward busy (tick or decision run in progress): try again when it finishes")
    ok, steward = gh(["api", "user", "--jq", ".login"])
    if not ok:
        sys.exit(f"gh auth failed: {steward}")

    merged = 0
    updates = load_updates()
    for repo in server.repos_config():
        full = repo["name"]
        auto_merge = None
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
            ref = f"{full}#{pr['number']}"
            go, reason = eligible(pr, steward, full, updates)
            if not go:
                print(f"skip  {ref}  {pr['title'][:60]}  ({reason})")
                continue
            if not args.merge:
                behind = " (behind base: update, then auto-merge)" if pr["mergeStateStatus"] == "BEHIND" else ""
                print(f"would {ref}  {pr['title'][:60]}{behind}")
                continue
            if auto_merge is None:
                ok, out = gh(["api", f"repos/{full}", "--jq", ".allow_auto_merge"])
                auto_merge = ok and out == "true"
            ok, done, detail = squash(pr, full, updates, auto_merge)
            label = "MERGED" if done else "QUEUED" if ok else "FAILED"
            print(f"{label} {ref}  {pr['title'][:60]}  {detail[:200]}")
            merged += done
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
                               "head": pr["headRefOid"], "rule": "good-to-merge"})
    if args.merge:
        print(f"{merged} merged")


if __name__ == "__main__":
    main()
