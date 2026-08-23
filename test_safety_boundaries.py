import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import authorize_tick_auto_merge
from tick_guard import check_review_integrity


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


if __name__ == "__main__":
    unittest.main()
