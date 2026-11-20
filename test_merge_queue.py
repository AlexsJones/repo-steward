#!/usr/bin/env python3
"""The tick's merge queue: oldest sign-off first, capped per run, draft-safe."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import audit
import merge_ready
import render_dashboard
import server

STEWARD = "steward-bot"
REPO = "owner/repo"


def pr(number, approved_at, title="fix: something", **extra):
    out = {"number": number, "title": title, "isDraft": False,
           "headRefOid": f"head{number:04d}abcdef", "reviewDecision": "APPROVED",
           "mergeStateStatus": "CLEAN",
           "statusCheckRollup": [{"__typename": "CheckRun", "name": "ci",
                                  "status": "COMPLETED", "conclusion": "SUCCESS"}],
           "reviews": [{"author": {"login": STEWARD}, "state": "APPROVED",
                        "body": "Looks right. Good to merge.", "submittedAt": approved_at,
                        "commit": {"oid": f"head{number:04d}abcdef"}}]}
    out.update(extra)
    return out


class MergeQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prs = {}
        self.merged = []
        self.auto = []
        self.strict = False
        self.refuse = {}
        self.files = {}
        self.report_merged = True
        for target, value in [(merge_ready, "ROOT"), (server, "ROOT")]:
            patcher = mock.patch.object(target, value, self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in [
            mock.patch.object(merge_ready, "UPDATES", self.root / ".merge_ready_updates.json"),
            mock.patch.object(audit, "LOG", self.root / "audit.jsonl"),
            mock.patch.object(merge_ready, "gh", side_effect=self.fake_gh),
            mock.patch.object(server, "repos_config", return_value=[{"name": REPO}]),
            mock.patch.object(server, "tick_active", return_value=True),
            mock.patch.object(server, "decide_active", return_value=False),
            mock.patch.dict(os.environ, {"STEWARD_RUNNING_TICK": "1"}),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, mode="live", merges=None):
        line = f"  merges_per_tick: {merges}\n" if merges is not None else ""
        (self.root / "config.yaml").write_text(f"mode: {mode}\nlimits:\n{line}", encoding="utf-8")

    def fake_gh(self, args):
        if args[:2] == ["api", "user"]:
            return True, STEWARD
        if args[:2] == ["pr", "list"]:
            return True, json.dumps([{"number": n, "reviews": p["reviews"]}
                                     for n, p in self.prs.items()])
        if args[:2] == ["pr", "view"]:
            p = self.prs[int(args[2])]
            if "--jq" in args:
                return True, p[args[args.index("--jq") + 1].lstrip(".")]
            return True, json.dumps(p)
        if args[0] == "api" and "/actions/runs" in args[1]:
            return True, json.dumps([{"sha": sha, "name": "CI"} for sha in getattr(self, "held", [])])
        if args[0] == "api" and args[1].startswith("repos/"):
            return True, "true"
        if args[:2] == ["pr", "diff"]:
            n = int(args[2])
            return True, "\n".join(self.files.get(n, [f"file{n}.txt"]))
        if args[:2] == ["pr", "update-branch"]:
            p = self.prs[int(args[2])]
            p["headRefOid"] += "-updated"
            p["mergeStateStatus"] = "BLOCKED"  # checks rerun on the new head
            return True, ""
        if args[:2] == ["pr", "merge"] and "--auto" in args:
            self.auto.append(int(args[2]))
            return True, ""
        if args[:2] == ["pr", "merge"]:
            n = int(args[2])
            if n in self.refuse:
                return False, self.refuse[n]
            if self.strict and self.prs[n]["mergeStateStatus"] == "BEHIND":
                return False, 'GraphQL: Required status check "DCO" is expected. (mergePullRequest)'
            self.merged.append(n)
            merged_pr = self.prs[n]
            if self.report_merged:
                merged_pr["state"] = "MERGED"
                merged_pr["mergedAt"] = "2026-10-28T00:00:00Z"
                merged_pr["mergeCommit"] = {"oid": f"merge{n:04d}abcdef"}
            if self.strict:  # the base moved: every other open PR is now behind it
                for other, p in self.prs.items():
                    if other not in self.merged and p["mergeStateStatus"] == "CLEAN":
                        p["mergeStateStatus"] = "BEHIND"
            return True, ""
        raise AssertionError(f"unexpected gh call {args}")

    def run_main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["merge_ready.py", *argv]), redirect_stdout(out):
            merge_ready.main()
        return out.getvalue()

    def snapshot(self):
        return json.loads((self.root / "merge_queue.json").read_text())

    def test_tick_merges_oldest_sign_offs_up_to_the_limit(self):
        self.config(merges=2)
        self.prs = {1: pr(1, "2026-09-30T00:00:00Z"), 2: pr(2, "2026-09-10T00:00:00Z"),
                    3: pr(3, "2026-09-20T00:00:00Z")}
        out = self.run_main("--tick")
        self.assertEqual(self.merged, [2, 3])
        snap = self.snapshot()
        self.assertEqual([e["number"] for e in snap["merged"]], [2, 3])
        self.assertEqual([e["number"] for e in snap["waiting"]], [1])
        self.assertIn('SUMMARY {"mode": "live", "limit": 2, "acting": true, "merged": 2', out)
        events = [json.loads(l) for l in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["data"]["trigger"] for e in events], ["tick", "tick"])

    def test_draft_mode_reports_the_queue_without_merging(self):
        self.config(mode="draft", merges=5)
        self.prs = {1: pr(1, "2026-09-30T00:00:00Z")}
        self.run_main("--tick")
        self.assertEqual(self.merged, [])
        snap = self.snapshot()
        self.assertFalse(snap["acting"])
        self.assertEqual([e["number"] for e in snap["waiting"]], [1])

    def test_zero_limit_turns_the_phase_off(self):
        self.config(merges=0)
        self.prs = {1: pr(1, "2026-09-30T00:00:00Z")}
        self.run_main("--tick")
        self.assertEqual(self.merged, [])

    def test_default_limit_when_config_omits_it(self):
        self.config()
        self.prs = {n: pr(n, f"2026-09-01T00:{n:02d}:00Z") for n in range(1, 33)}
        self.run_main("--tick")
        self.assertEqual(self.merged, list(range(1, 31)))

    def test_strict_base_updates_the_rest_of_the_repo_after_a_merge(self):
        # k8sgpt, 2026-10-13: the first merge left the others behind main, and
        # merging them from the stale CLEAN state failed as "DCO is expected".
        self.config(merges=5)
        self.strict = True
        self.prs = {n: pr(n, f"2026-09-0{n}T00:00:00Z") for n in (1, 2, 3)}
        self.run_main("--tick")
        self.assertEqual(self.merged, [1])
        self.assertEqual(self.auto, [2, 3])
        snap = self.snapshot()
        self.assertEqual([e["number"] for e in snap["queued"]], [2, 3])
        self.assertEqual(snap["failed"], [])

    def test_stale_clean_refused_as_missing_check_falls_back_to_update(self):
        self.config(merges=5)
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z")}
        self.refuse = {1: 'GraphQL: Required status check "DCO" is expected. (mergePullRequest)'}
        self.run_main("--tick")
        self.assertEqual(self.auto, [1])
        self.assertTrue(self.prs[1]["headRefOid"].endswith("-updated"))
        self.assertEqual([e["number"] for e in self.snapshot()["queued"]], [1])

    def test_other_merge_failures_are_not_retried_as_behind(self):
        self.config(merges=5)
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z")}
        self.refuse = {1: "GraphQL: Pull Request has merge conflicts (mergePullRequest)"}
        self.run_main("--tick")
        self.assertEqual(self.auto, [])
        self.assertIn("merge conflicts", self.snapshot()["failed"][0]["detail"])

    def test_conflict_found_on_refresh_does_not_spend_a_slot(self):
        self.config(merges=1)
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z"), 2: pr(2, "2026-09-02T00:00:00Z")}
        real = self.fake_gh

        def conflicted(args):
            if args[:3] == ["pr", "view", "1"] and "--jq" in args:
                return True, "DIRTY"
            return real(args)
        with mock.patch.object(merge_ready, "gh", side_effect=conflicted):
            self.run_main("--tick")
        self.assertEqual(self.merged, [2])
        failed = self.snapshot()["failed"]
        self.assertEqual([e["number"] for e in failed], [1])
        self.assertIn("became DIRTY", failed[0]["detail"])

    def test_ineligible_prs_are_skipped_not_counted(self):
        self.config(merges=1)
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z", title="feat!: big thing"),
                    2: pr(2, "2026-09-02T00:00:00Z")}
        self.run_main("--tick")
        self.assertEqual(self.merged, [2])
        self.assertEqual([e["number"] for e in self.snapshot()["skipped"]], [1])

    def test_signed_off_feature_merges_but_major_bump_does_not(self):
        self.config(merges=5)
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z", title="feat(i18n): German"),
                    2: pr(2, "2026-09-02T00:00:00Z", title="chore(deps): bump dirs from 6.0.0 to 7.0.0")}
        self.run_main("--tick")
        self.assertEqual(self.merged, [1])
        self.assertIn("major version bump", self.snapshot()["skipped"][0]["detail"])

    def test_held_ci_run_is_reported_as_blocked_not_skipped(self):
        self.config(merges=5)
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z", mergeStateStatus="UNSTABLE"),
                    2: pr(2, "2026-09-02T00:00:00Z", mergeStateStatus="UNSTABLE")}
        self.held = [self.prs[1]["headRefOid"]]
        self.run_main("--tick")
        snap = self.snapshot()
        self.assertEqual(self.merged, [])
        self.assertEqual([e["number"] for e in snap["blocked"]], [1])
        self.assertIn("awaiting your approval: CI", snap["blocked"][0]["detail"])
        self.assertEqual([e["number"] for e in snap["skipped"]], [2])

    def test_approval_without_sign_off_is_queued_for_rereview_once_green(self):
        self.config(merges=5)
        plain = {"author": {"login": STEWARD}, "state": "APPROVED",
                 "body": "Approving; CI has not run on this head yet.",
                 "submittedAt": "2026-09-01T00:00:00Z"}
        green = pr(1, "2026-09-01T00:00:00Z")
        green["reviews"] = [dict(plain, commit={"oid": green["headRefOid"]})]
        held = pr(2, "2026-09-02T00:00:00Z", statusCheckRollup=[])
        held["reviews"] = [dict(plain, commit={"oid": held["headRefOid"]})]
        stale = pr(3, "2026-09-03T00:00:00Z")
        stale["reviews"] = [dict(plain, commit={"oid": "olderhead"})]
        self.prs = {1: green, 2: held, 3: stale}
        self.held = [held["headRefOid"]]
        self.run_main("--tick")
        snap = self.snapshot()
        self.assertEqual(self.merged, [])
        self.assertEqual([e["number"] for e in snap["rereview"]], [1])
        self.assertEqual([e["number"] for e in snap["blocked"]], [2])
        self.assertEqual(snap["skipped"], [])

    def test_tick_flag_refused_outside_a_tick(self):
        self.config()
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            self.run_main("--tick")

    def test_branch_update_is_not_recorded_as_a_merge(self):
        self.config(merges=5)
        self.strict = True
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z"), 2: pr(2, "2026-09-02T00:00:00Z")}
        approved = self.prs[2]["headRefOid"]
        self.run_main("--tick")
        events = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        by_ref = {event["ref"]: event for event in events}
        self.assertEqual(by_ref["pr-1"]["event"], "terminal")
        self.assertEqual(by_ref["pr-1"]["data"]["action"], "merge")
        self.assertEqual(by_ref["pr-1"]["data"]["approved_head"], self.prs[1]["headRefOid"])
        updated = by_ref["pr-2"]
        self.assertEqual(updated["event"], "merge_attempt")
        self.assertEqual(updated["data"]["action"], "auto-merge")
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["data"]["approved_head"], approved)
        self.assertTrue(updated["data"]["head"].endswith("-updated"))
        merges = [json.loads(line) for line in (self.root / "merges.jsonl").read_text().splitlines()]
        self.assertEqual([row["number"] for row in merges], [1])
        self.assertEqual(merges[0]["merge_commit"], "merge0001abcdef")
        self.assertEqual(merges[0]["files"], ["file1.txt"])

    def test_unconfirmed_merge_is_a_failure(self):
        self.config(merges=5)
        self.report_merged = False
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z")}
        self.run_main("--tick")
        self.assertEqual(self.merged, [1])
        snap = self.snapshot()
        self.assertEqual(snap["merged"], [])
        self.assertIn("state is not MERGED", snap["failed"][0]["detail"])
        events = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(events[0]["event"], "merge_attempt")
        self.assertFalse(events[0]["ok"])
        self.assertFalse((self.root / "merges.jsonl").exists())

    def test_overlapping_prs_wait_for_the_next_tick(self):
        self.config(merges=5)
        self.prs = {1: pr(1, "2026-09-03T00:00:00Z"), 2: pr(2, "2026-09-01T00:00:00Z"),
                    3: pr(3, "2026-09-02T00:00:00Z")}
        self.files = {2: ["shared.go", "b.go"], 3: ["shared.go"], 1: ["a.go"]}
        self.run_main("--tick")
        self.assertEqual(self.merged, [2, 1])
        waiting = self.snapshot()["waiting"]
        self.assertEqual([row["number"] for row in waiting], [3])
        self.assertIn("overlaps #2", waiting[0]["detail"])

    def test_second_consecutive_failure_is_stuck(self):
        self.config(merges=5)
        prior = {"ts": "2026-10-01T00:00:00Z", "repo": REPO, "action": "good-to-merge",
                 "item": "pr-1", "ok": False, "merged": False, "detail": "merge: conflicts"}
        (self.root / "approvals.jsonl").write_text(json.dumps(prior) + "\n", encoding="utf-8")
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z"), 2: pr(2, "2026-09-02T00:00:00Z")}
        self.refuse = {1: "GraphQL: Pull Request has merge conflicts (mergePullRequest)"}
        self.run_main("--tick")
        stuck = self.snapshot()["stuck"]
        self.assertEqual([row["number"] for row in stuck], [1])
        self.assertIn("previous: merge: conflicts", stuck[0]["detail"])
        self.assertEqual(self.merged, [2])

    def test_failure_after_a_success_is_not_stuck(self):
        self.config(merges=5)
        prior = {"ts": "2026-10-01T00:00:00Z", "repo": REPO, "action": "good-to-merge",
                 "item": "pr-1", "ok": True, "merged": True, "detail": "merge: merged"}
        (self.root / "approvals.jsonl").write_text(json.dumps(prior) + "\n", encoding="utf-8")
        self.prs = {1: pr(1, "2026-09-01T00:00:00Z")}
        self.refuse = {1: "GraphQL: Pull Request has merge conflicts (mergePullRequest)"}
        self.run_main("--tick")
        self.assertEqual(self.snapshot()["stuck"], [])

    def test_manual_merge_still_refuses_during_a_tick(self):
        self.config()
        with self.assertRaises(SystemExit):
            self.run_main("--merge")

    def test_manual_limit(self):
        self.config()
        self.prs = {n: pr(n, f"2026-09-{n:02d}T00:00:00Z") for n in (3, 1, 2)}
        with mock.patch.object(server, "tick_active", return_value=False):
            self.run_main("--merge", "--limit", "1")
        self.assertEqual(self.merged, [1])
        self.assertFalse((self.root / "merge_queue.json").exists())


class MergeQueuePanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.yaml").write_text(
            "mode: live\nrepos:\n  - name: owner/repo\n    priority: high\n", encoding="utf-8")
        (self.root / "audit.jsonl").write_text(
            json.dumps({"event": "merge_queue", "via": "tick", "ts": "2026-10-01T06:00:00Z",
                        "data": {"merged": 4, "queued": 0, "failed": 0, "waiting": 1}}) + "\n"
            + json.dumps({"event": "tick_done", "via": "tick", "ts": "2026-10-02T06:00:00Z"}) + "\n",
            encoding="utf-8")

    def snapshot(self, ts):
        row = {"repo": REPO, "number": 7, "title": "fix: thing", "url": "https://x/7",
               "detail": "merged", "approved_at": "2026-09-30T00:00:00Z"}
        (self.root / "merge_queue.json").write_text(json.dumps(
            {"ts": ts, "mode": "live", "limit": 10, "acting": True, "merged": [row],
             "queued": [], "failed": [], "waiting": [], "skipped": []}), encoding="utf-8")

    def test_panel_shows_this_ticks_merges(self):
        self.snapshot("2026-10-02T06:30:00Z")
        page = render_dashboard.render(self.root)
        self.assertIn("Merge queue · this tick", page)
        self.assertIn('st ok">merged', page)
        self.assertIn("<b>1/10</b>", page)
        self.assertIn('Failed</span><b>0</b>', page)

    def test_panel_shows_blocked_and_rereview_buckets(self):
        self.snapshot("2026-10-02T06:30:00Z")
        snap = json.loads((self.root / "merge_queue.json").read_text())
        snap["blocked"] = [{"repo": REPO, "number": 8, "title": "fix: held", "url": "https://x/8",
                            "detail": "CI run awaiting your approval: CI", "approved_at": ""}]
        snap["rereview"] = [{"repo": REPO, "number": 9, "title": "fix: green", "url": "https://x/9",
                             "detail": "approved without sign-off; checks are green now", "approved_at": ""}]
        snap["stuck"] = [{"repo": REPO, "number": 4, "title": "fix: stuck", "url": "https://x/4",
                          "detail": "second consecutive failure", "approved_at": ""}]
        (self.root / "merge_queue.json").write_text(json.dumps(snap), encoding="utf-8")
        page = render_dashboard.render(self.root)
        self.assertIn("needs your CI approval", page)
        self.assertIn("re-review owed", page)
        self.assertIn(">escalate<", page)
        self.assertIn('Needs you</span><b>1</b>', page)

    def test_stale_snapshot_is_not_shown_as_this_tick(self):
        self.snapshot("2026-10-01T06:30:00Z")
        page = render_dashboard.render(self.root)
        self.assertIn("The latest tick had no merge phase.", page)
        self.assertNotIn('st ok">merged', page)


if __name__ == "__main__":
    unittest.main()
