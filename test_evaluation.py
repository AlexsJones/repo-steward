#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import evaluation


class CompactSignalTests(unittest.TestCase):
    def test_bounds_evidence_and_keeps_latest_staged_action(self):
        row = {
            "id": "sig_1", "ts": "2026-10-27T00:00:00Z", "kind": "steward_event",
            "repo": {"name": "owner/repo"}, "subject": {"id": "pr:1"},
            "source": {"updated_at": "2026-10-27T00:00:00Z"},
            "attributes": {"event": "approve"},
            "analysis": {"summary": "looks ready"},
            "evidence": [
                {"type": "comment", "text": "a" * 2500, "label": "first",
                 "recent_comments": [
                     {"author": "a", "text": "one"},
                     {"author": "b", "text": "2" * 1200},
                     {"author": "c", "text": "three"},
                 ]},
                {"type": "review", "label": "second"},
                "not-a-dict",
                {"type": "review", "label": "dropped"},
            ],
            "workflow": {
                "last_action": "approve",
                "notes": "n" * 900,
                "empty": "",
                "staged_actions": [
                    {"kind": "review", "body": "old"},
                    {"kind": "approve", "staged_at": "2026-10-26T00:00:00Z",
                     "executed_at": "2026-10-27T00:00:00Z", "body": "b" * 2000},
                ],
            },
        }
        out = evaluation.compact_evaluation_signal(row)
        self.assertEqual([item["label"] for item in out["evidence"]], ["first", "second"])
        self.assertEqual(len(out["evidence"][0]["text"]), 2000)
        comments = out["evidence"][0]["recent_comments"]
        self.assertEqual([comment["author"] for comment in comments], ["b", "c"])
        self.assertEqual(len(comments[0]["text"]), 1000)
        self.assertEqual(out["analysis"]["summary"], "looks ready")
        self.assertEqual(len(out["workflow"]["notes"]), 700)
        self.assertNotIn("empty", out["workflow"])
        staged = out["workflow"]["latest_staged_action"]
        self.assertEqual(staged["kind"], "approve")
        self.assertEqual(len(staged["body"]), 1500)


class ValidateTests(unittest.TestCase):
    def candidate(self, status="insufficient-data", signal_ids=None):
        dimension = {
            "key": "issue-triage", "title": "Issue triage", "status": status,
            "summary": "No issue judgments in the selection.",
        }
        if signal_ids is not None:
            dimension["signal_ids"] = signal_ids
        return {"v": 1, "posture": status, "summary": "Nothing to judge yet.",
                "dimensions": [dimension], "findings": [], "lessons": []}

    def test_insufficient_data_dimension_may_omit_signals(self):
        report, _ = evaluation.validate(self.candidate(), [], set())
        self.assertEqual(report["dimensions"][0]["signal_ids"], [])
        report, _ = evaluation.validate(self.candidate(signal_ids=[]), [], set())
        self.assertEqual(report["dimensions"][0]["signal_ids"], [])

    def test_other_dimension_status_still_requires_a_signal(self):
        with self.assertRaisesRegex(evaluation.InvalidEvaluation, "at least 1"):
            evaluation.validate(self.candidate("stable", []), [], set())


class PrepareTests(unittest.TestCase):
    def test_prepare_writes_selected_signals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.yaml").write_text("repos:\n  - name: owner/repo\n", encoding="utf-8")
            records = [
                {"id": "sig_item", "ts": "2026-10-01T00:00:00Z", "kind": "item_observed",
                 "repo": {"name": "owner/repo"}, "subject": {"id": "pr:1"},
                 "source": {}, "evidence": [{"label": "issue", "text": "body"}]},
                {"id": "sig_other", "ts": "2026-10-02T00:00:00Z", "kind": "item_observed",
                 "repo": {"name": "someone/else"}, "subject": {"id": "pr:9"}, "source": {}},
                {"id": "sig_event", "ts": "2026-10-03T00:00:00Z", "kind": "steward_event",
                 "repo": {"name": "owner/repo"}, "subject": {"id": "pr:1"},
                 "attributes": {"event": "approve"}, "source": {}},
            ]
            (root / "signals.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            selection = evaluation.prepare(root)["selection"]
            payload = json.loads((root / "evaluation-input.json").read_text(encoding="utf-8"))
        self.assertEqual(selection["available_signals"], 2)
        self.assertEqual(selection["selected_signals"], 2)
        self.assertEqual({row["id"] for row in payload["signals"]}, {"sig_item", "sig_event"})
        self.assertEqual(payload["signals"][0]["evidence"][0]["text"], "body")


if __name__ == "__main__":
    unittest.main()
