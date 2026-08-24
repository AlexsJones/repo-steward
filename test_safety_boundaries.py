import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import authorize_tick_auto_merge
from tick_guard import check_review_integrity, check_tick


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


class SafetyBoundaryTests(unittest.TestCase):
    def review_check(self, old, current, activity=""):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        before, state = root / "before", root / "state"
        before.mkdir(); state.mkdir()
        write(before / "repo.json", {"items": {"pr-1": old}})
        write(state / "repo.json", {"items": {"pr-1": current}})
        activity_path = root / "activity.jsonl"
        activity_path.write_text(activity, encoding="utf-8")
        result = check_review_integrity(before, state, activity_path)
        temp.cleanup()
        return result

    def test_legacy_field_hydration_is_not_a_new_judgment(self):
        old = {"type": "pr", "status": "ready-for-maintainer",
               "verdict": "approve-recommend"}
        current = {**old, "head_oid": "abc", "review_records": []}

        result = self.review_check(old, current)

        self.assertTrue(result["ok"])
        self.assertEqual(["repo:pr-1"], result["legacy_missing"])

    def test_real_head_change_still_requires_fresh_review_evidence(self):
        old = {"type": "pr", "status": "ready-for-maintainer",
               "verdict": "approve-recommend", "head_oid": "abc"}

        result = self.review_check(old, {**old, "head_oid": "def"})

        self.assertFalse(result["ok"])
        self.assertEqual("repo:pr-1", result["violations"][0]["item"])

    def test_local_escalation_is_not_treated_as_an_outbound_review(self):
        old = {"type": "pr", "status": "backlog", "head_oid": "abc"}
        current = {**old, "status": "escalated"}
        activity = '{"kind":"escalated","repo":"repo","ref":"pr-1"}\n'

        result = self.review_check(old, current, activity)

        self.assertTrue(result["ok"])
        self.assertEqual([], result["checked"])

    def test_auto_merge_refuses_legacy_approval_without_canonical_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "state").mkdir()
            write(root / "state" / "repo.json", {"items": {"pr-1": {
                "type": "pr", "status": "ready-for-maintainer",
                "verdict": "approve-recommend", "head_oid": "abc"}}})
            with mock.patch("server.ROOT", root), \
                 mock.patch("server.steward_mode", return_value="live"), \
                 mock.patch("server.repo_map", return_value={"repo": "owner/repo"}), \
                 mock.patch("server.run_gh") as run_gh:
                ok, reason = authorize_tick_auto_merge("owner/repo", "1")

            self.assertFalse(ok)
            self.assertIn("canonical review evidence", reason)
            run_gh.assert_not_called()

    def proactive_check(self, config, activity):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name); state = root / "state"; state.mkdir()
        (root / "config.yaml").write_text(config, encoding="utf-8")
        (root / "activity.jsonl").write_text(activity, encoding="utf-8")
        write(root / "proactive.json", {"items": {"idea:one": {
            "status": "selected", "selected_at": "2026-08-20T00:00:00Z"}}})
        result = check_tick(state, root / "config.yaml", root / "activity.jsonl",
                            "2026-08-24T10:00:00Z", root / "proactive.json")
        temp.cleanup()
        return result

    def test_selected_work_item_requires_incremental_progress(self):
        result = self.proactive_check("proactive_items_per_tick: 1\n", "")

        self.assertTrue(result["proactive_queue_failure"])
        self.assertEqual(1, result["required_proactive_actions"])
        self.assertEqual(["idea:one"], result["proactive_eligible"])

    def test_proactive_activity_satisfies_the_reserved_slot(self):
        activity = ('{"kind":"proactive","ref":"idea:one","ok":true,'
                    '"summary":"wrote bounded proposal"}\n')
        result = self.proactive_check("proactive_items_per_tick: 1\n", activity)

        self.assertFalse(result["proactive_queue_failure"])
        self.assertEqual(1, result["proactive_actions"])

    def test_completed_proactive_action_is_not_counted_as_another_required_item(self):
        activity = ('{"kind":"proactive","ref":"idea:one","ok":true,'
                    '"summary":"wrote bounded proposal"}\n')
        result = self.proactive_check("proactive_items_per_tick: 3\n", activity)

        self.assertFalse(result["proactive_queue_failure"])
        self.assertEqual(1, result["required_proactive_actions"])
        self.assertEqual(1, result["proactive_actions"])

    def test_one_idea_cannot_satisfy_another_ideas_reserved_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root / "state"; state.mkdir()
            (root / "config.yaml").write_text(
                "proactive_items_per_tick: 3\n", encoding="utf-8")
            (root / "activity.jsonl").write_text(
                '{"kind":"proactive","ref":"idea:one","ok":true}\n'
                '{"kind":"proactive","ref":"idea:one","ok":true}\n'
                '{"kind":"proactive","ref":"idea:unrelated","ok":true}\n',
                encoding="utf-8")
            write(root / "proactive.json", {"items": {
                "idea:one": {"status": "selected"},
                "idea:two": {"status": "nominated"},
            }})

            result = check_tick(
                state, root / "config.yaml", root / "activity.jsonl",
                "2026-08-24T10:00:00Z", root / "proactive.json")

            self.assertTrue(result["proactive_queue_failure"])
            self.assertEqual(2, result["required_proactive_actions"])
            self.assertEqual(1, result["proactive_actions"])

    def test_zero_proactive_cap_explicitly_disables_reserved_progress(self):
        result = self.proactive_check("proactive_items_per_tick: 0\n", "")

        self.assertFalse(result["proactive_queue_failure"])
        self.assertEqual(0, result["required_proactive_actions"])


if __name__ == "__main__":
    unittest.main()
