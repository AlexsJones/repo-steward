#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import render_dashboard

REPO = "acme/widget"


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config.yaml").write_text(
            f"mode: live\nrepos:\n  - name: {REPO}\n    priority: high\n", encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()
        self.ledger({})
        self.audit([self.tick("2026-10-02T06:00:00Z")])

    def ledger(self, items):
        (self.root / "state" / "widget.json").write_text(json.dumps({"items": items}), encoding="utf-8")

    def audit(self, events):
        (self.root / "audit.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    @staticmethod
    def tick(ts, rc=0):
        return {"event": "tick_done", "via": "tick", "ts": ts, "ok": rc == 0, "data": {"rc": rc}}

    def briefing(self):
        page = render_dashboard.render(self.root)
        start = page.index('id="briefing"')
        return page, page[start:page.index("</section>", start)]

    def test_briefing_sits_above_every_other_section(self):
        page, _ = self.briefing()
        self.assertLess(page.index('id="briefing"'), page.index('class="readout"'))

    def test_quiet_tick_says_nothing_needs_you(self):
        _, brief = self.briefing()
        self.assertIn("Nothing needs you today.", brief)
        self.assertNotIn("attn", brief)

    def test_expired_login_is_named_with_the_fix_and_the_streak(self):
        self.audit([self.tick("2026-10-01T06:00:00Z"), self.tick("2026-10-02T06:00:00Z", 1),
                    self.tick("2026-10-03T06:00:00Z", 1)])
        (self.root / "logs" / "tick.log").write_text(
            "=== tick 2026-10-03T06:00:00Z engine=claude (rc=1) ===\n"
            "Failed to authenticate: OAuth session expired and could not be refreshed\n"
            "=== tick 2026-10-03T06:00:00Z merge queue (rc=0) ===\n", encoding="utf-8")
        _, brief = self.briefing()
        self.assertIn("2 ticks in a row have failed, since 2026-10-02", brief)
        self.assertIn("Claude login expired", brief)
        self.assertIn("claude /login", brief)
        self.assertIn(">2026-10-01<", brief)

    def test_known_exit_code_is_explained(self):
        self.audit([self.tick("2026-10-02T06:00:00Z", 124)])
        _, brief = self.briefing()
        self.assertIn("The latest tick failed", brief)
        self.assertIn("hit the tick time limit", brief)

    def test_items_are_ordered_most_urgent_first(self):
        self.ledger({
            "pr-5": {"type": "pr", "title": "feat: five", "status": "ready-for-maintainer",
                     "verdict": "approve-recommend"},
            "issue-6": {"type": "issue", "title": "bug: six", "status": "escalated"},
            "issue-7": {"type": "issue", "title": "bug: seven", "status": "posted",
                        "staged_actions": [{"kind": "reply", "body": "hello"}]},
        })
        (self.root / "escalations.md").write_text(
            "## widget #6 — pick an approach\n\n**Question:** A or B?\n\n", encoding="utf-8")
        (self.root / "merge_queue.json").write_text(json.dumps({
            "ts": "2026-10-02T06:30:00Z", "mode": "live", "limit": 10, "acting": True,
            "merged": [{"repo": REPO, "number": 1, "title": "fix: one", "url": "https://x/1"}],
            "queued": [], "failed": [], "waiting": [], "skipped": [],
            "blocked": [{"repo": REPO, "number": 8, "title": "fix: held", "url": "https://x/8",
                         "detail": "CI run awaiting your approval: CI"}]}), encoding="utf-8")
        _, brief = self.briefing()
        order = [brief.index(text) for text in (
            "1 decision waiting on you", "cannot merge until you approve the CI run",
            "1 PR approved by the steward", "1 staged reply waiting for you to post")]
        self.assertEqual(order, sorted(order))
        self.assertIn('<span class="count sig">4</span>', brief)
        self.assertIn("1 merged", brief)

    def test_prs_the_merge_queue_is_handling_are_not_a_final_look(self):
        self.ledger({"pr-5": {"type": "pr", "title": "feat: five", "status": "ready-for-maintainer",
                              "verdict": "approve-recommend"}})
        (self.root / "merge_queue.json").write_text(json.dumps({
            "ts": "2026-10-02T06:30:00Z", "mode": "live", "limit": 10, "acting": True, "merged": [],
            "queued": [{"repo": REPO, "number": 5, "title": "feat: five", "url": "https://x/5"}],
            "failed": [], "waiting": [], "skipped": []}), encoding="utf-8")
        _, brief = self.briefing()
        self.assertNotIn("waiting for your merge", brief)
        self.assertIn("1 on auto-merge", brief)

    def test_long_lists_are_cut_to_three_with_a_link_to_the_rest(self):
        self.ledger({f"pr-{n}": {"type": "pr", "title": f"feat: {n}", "status": "ready-for-maintainer",
                                 "verdict": "approve-recommend"} for n in range(1, 6)})
        _, brief = self.briefing()
        self.assertIn("5 PRs approved by the steward", brief)
        self.assertIn('<a href="#ready">+2 more</a>', brief)


if __name__ == "__main__":
    unittest.main()
