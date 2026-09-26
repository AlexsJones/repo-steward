#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import builds
import insights

SNAPSHOT = {
    "prepared_at": "2026-09-25T08:00:00Z",
    "repositories": [{
        "name": "AlexsJones/llama-panel", "stars": 70, "open_issue_count": 1,
        "issues": [{"number": 3, "title": "Add support for llama-cpp router mode",
                    "url": "https://github.com/AlexsJones/llama-panel/issues/3",
                    "reactions": 0, "comments": 12, "participants": 2,
                    "updated_at": "2026-09-24T10:00:00Z", "labels": ["enhancement"]}],
        "open_prs": [],
    }],
}


def theme(**over):
    t = {
        "repo": "AlexsJones/llama-panel", "key": "router-mode", "title": "Drive llama-server router mode",
        "kind": "feature", "summary": "Talk to an existing router instead of spawning.",
        "issues": [3], "prs": [], "value": "The reporter runs router mode daily.",
        "value_score": 4, "readiness": "ready", "readiness_note": "States agreed on 2026-09-24.",
        "effort": "medium", "build_cost": "medium", "risk": "Router API is young.",
        "brief": {"goal": "Show and control router presets.", "acceptance": ["Lists presets"],
                  "likely_files": [], "open_questions": []},
    }
    t.update(over)
    return t


class ValidateTests(unittest.TestCase):
    def check(self, *themes_):
        return insights.validate({"v": 2, "themes": list(themes_)}, SNAPSHOT)

    def test_single_issue_theme_publishes_with_snapshot_evidence(self):
        graph = self.check(theme())
        t = graph["themes"][0]
        self.assertEqual(t["id"], "theme:AlexsJones/llama-panel:router-mode")
        self.assertEqual(t["rank"], 1)
        self.assertEqual(t["evidence"][0]["comments"], 12)

    def test_rejects_issue_not_in_snapshot(self):
        with self.assertRaisesRegex(insights.InvalidInsights, "#99"):
            self.check(theme(issues=[99]))

    def test_rejects_theme_without_issues(self):
        with self.assertRaisesRegex(insights.InvalidInsights, "at least one"):
            self.check(theme(issues=[]))

    def test_rejects_unknown_repo_and_enum(self):
        with self.assertRaises(insights.InvalidInsights):
            self.check(theme(repo="someone/else"))
        with self.assertRaises(insights.InvalidInsights):
            self.check(theme(readiness="soon"))

    def test_rejects_duplicate_keys_and_too_many_per_repo(self):
        with self.assertRaisesRegex(insights.InvalidInsights, "unique"):
            self.check(theme(), theme())
        with self.assertRaisesRegex(insights.InvalidInsights, "more than"):
            self.check(*[theme(key=f"k{i}") for i in range(insights.MAX_PER_REPO + 1)])

    def test_rejects_old_schema(self):
        with self.assertRaises(insights.InvalidInsights):
            insights.validate({"v": 1, "repositories": []}, SNAPSHOT)


class BuildQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = mock.patch.object(builds.audit, "append")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.theme = insights.validate({"v": 2, "themes": [theme()]}, SNAPSHOT)["themes"][0]

    def result(self, value):
        path = self.root / "builds" / f"{builds.slug(self.theme['id'])}.result.json"
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_enqueue_claim_finish_pr_open(self):
        ok, _ = builds.enqueue(self.theme, "keep it small", self.root)
        self.assertTrue(ok)
        self.assertFalse(builds.enqueue(self.theme, "", self.root)[0])
        theme_id = builds.claim(self.root)
        self.assertEqual(theme_id, self.theme["id"])
        brief = json.loads((self.root / "builds" / f"{builds.slug(theme_id)}.json").read_text())
        self.assertEqual(brief["branch"], "steward/build-router-mode")
        self.assertEqual(brief["note"], "keep it small")
        self.result({"status": "pr-open", "pr_url": "https://github.com/x/y/pull/9", "summary": "done"})
        final = builds.finish(theme_id, 0, self.root)
        self.assertEqual(final["status"], "pr-open")
        self.assertIn("PR already open", builds.enqueue(self.theme, "", self.root)[1])

    def test_missing_result_is_failed_and_can_be_retried(self):
        builds.enqueue(self.theme, "", self.root)
        theme_id = builds.claim(self.root)
        self.assertEqual(builds.finish(theme_id, 1, self.root)["status"], "failed")
        self.assertTrue(builds.enqueue(self.theme, "", self.root)[0])
        self.assertEqual(builds.read(self.root)["items"][theme_id]["attempts"], 1)

    def test_reap_fails_builds_left_by_a_dead_run(self):
        builds.enqueue(self.theme, "", self.root)
        builds.claim(self.root)
        self.assertEqual(builds.reap(self.root), 1)
        self.assertEqual(builds.read(self.root)["items"][self.theme["id"]]["status"], "failed")
        self.assertIsNone(builds.claim(self.root))


if __name__ == "__main__":
    unittest.main()
