import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tick_guard import (check_review_integrity, check_tick, recover_from_audit,
                        repair_ledgers, review_record_errors)
from server import (auto_merge_candidate, find_insight_idea, insight_decisions,
                    prepare_review_record)
from render_dashboard import render
from signals import (collect as collect_signals, item_signal,
                     query as query_signals)
from insights import InvalidInsights, prepare as prepare_insights, validate as validate_insights
from proactive import sync as sync_proactive
from evaluation import InvalidEvaluation, validate as validate_evaluation


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


class TickGuardTest(unittest.TestCase):
    def _review_item(self):
        body = "Reviewed parser and fallback paths; both preserve prior behavior."
        record = {
            "v": 1, "id": "rr-abc-1", "head_oid": "abc",
            "verdict": "approve-recommend", "body": body,
            "recorded_at": "2026-08-20T10:00:00Z", "posted_at": None,
            "claims": ["Parser keeps the existing fallback behavior"],
            "risk_areas": ["fallback parsing"],
            "test_evidence": ["pytest tests/test_parser.py passed"],
            "review_basis": "Read the full diff and traced both parser branches",
            "integrity": "canonical",
        }
        return {
            "type": "pr", "status": "ready-for-maintainer",
            "verdict": "approve-recommend", "head_oid": "abc",
            "review_records": [record],
            "staged_actions": [{"kind": "pr_review_approve", "body": body,
                                "review_record_id": "rr-abc-1"}],
        }

    def test_review_integrity_accepts_linked_canonical_record(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); before = root / "before"; state = root / "state"
            before.mkdir(); state.mkdir()
            write(state / "repo.json", {"items": {"pr-1": self._review_item()}})
            activity = root / "activity.jsonl"
            activity.write_text('{"kind":"staged","repo":"repo","ref":"pr-1"}\n')
            result = check_review_integrity(before, state, activity)
            self.assertTrue(result["ok"])
            self.assertEqual(["repo:pr-1"], result["checked"])

    def test_review_history_flows_into_insight_evidence(self):
        signal = item_signal("owner/repo", "pr-1", self._review_item(),
                             "2026-08-20T10:01:00Z")
        records = signal["workflow"]["review_records"]
        self.assertEqual("rr-abc-1", records[0]["id"])
        self.assertEqual(["pytest tests/test_parser.py passed"],
                         records[0]["test_evidence"])

    def test_review_integrity_rejects_new_judgment_without_record(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); before = root / "before"; state = root / "state"
            before.mkdir(); state.mkdir()
            item = {"type": "pr", "status": "ready-for-maintainer",
                    "verdict": "approve-recommend", "head_oid": "abc"}
            write(state / "repo.json", {"items": {"pr-1": item}})
            activity = root / "activity.jsonl"; activity.write_text("")
            result = check_review_integrity(before, state, activity)
            self.assertFalse(result["ok"])
            self.assertIn("missing review_records entry", result["violations"][0]["errors"])

    def test_review_integrity_reports_unchanged_legacy_gap_without_failing(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); before = root / "before"; state = root / "state"
            before.mkdir(); state.mkdir()
            ledger = {"items": {"pr-1": {"type": "pr",
                "status": "ready-for-maintainer", "verdict": "approve-recommend",
                "head_oid": "abc"}}}
            write(before / "repo.json", ledger); write(state / "repo.json", ledger)
            activity = root / "activity.jsonl"; activity.write_text("")
            result = check_review_integrity(before, state, activity)
            self.assertTrue(result["ok"])
            self.assertEqual(["repo:pr-1"], result["legacy_missing"])

    def test_review_record_detects_changed_body_and_head(self):
        item = self._review_item()
        item["head_oid"] = "new"
        item["staged_actions"][0]["body"] = "different"
        errors = review_record_errors(item)
        self.assertTrue(any("current PR head" in error for error in errors))
        self.assertTrue(any("body differs" in error for error in errors))

    def test_review_integrity_rejects_rewriting_prior_record(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); before = root / "before"; state = root / "state"
            before.mkdir(); state.mkdir()
            old_item = self._review_item()
            write(before / "repo.json", {"items": {"pr-1": old_item}})
            current_item = json.loads(json.dumps(old_item))
            current_item["review_records"][0]["body"] = "silently rewritten"
            current_item["staged_actions"][0]["body"] = "silently rewritten"
            write(state / "repo.json", {"items": {"pr-1": current_item}})
            activity = root / "activity.jsonl"; activity.write_text("")
            result = check_review_integrity(before, state, activity)
            self.assertFalse(result["ok"])
            self.assertTrue(any("append-only" in error
                                for error in result["violations"][0]["errors"]))

    def test_review_integrity_allows_posted_at_to_be_filled_once(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); before = root / "before"; state = root / "state"
            before.mkdir(); state.mkdir()
            old_item = self._review_item()
            write(before / "repo.json", {"items": {"pr-1": old_item}})
            current_item = json.loads(json.dumps(old_item))
            current_item["review_records"][0]["posted_at"] = "2026-08-20T11:00:00Z"
            current_item["staged_actions"][0]["executed_at"] = "2026-08-20T11:00:00Z"
            write(state / "repo.json", {"items": {"pr-1": current_item}})
            activity = root / "activity.jsonl"
            activity.write_text('{"kind":"posted","repo":"repo","ref":"pr-1"}\n')
            result = check_review_integrity(before, state, activity)
            self.assertTrue(result["ok"], result["violations"])

    def test_dashboard_backfills_legacy_review_before_posting(self):
        item = {"type": "pr", "head_oid": "abc",
                "signal": {"review_basis": "read diff", "test_evidence": "CI passed"}}
        action = {"kind": "pr_review_approve", "body": "Looks good",
                  "staged_at": "2026-08-19T10:00:00Z"}
        ok, detail = prepare_review_record(item, action, "2026-08-20T10:00:00Z")
        self.assertTrue(ok)
        self.assertIn("backfilled", detail)
        record = item["review_records"][-1]
        self.assertEqual("Looks good", record["body"])
        self.assertEqual(record["id"], action["review_record_id"])
        self.assertEqual("legacy-backfill", record["integrity"])

    def test_self_evaluation_requires_outcome_evidence_and_grounds_lessons(self):
        records = [
            {"id": "action", "kind": "steward_event", "repo": {"name": "owner/repo"},
             "subject": {"id": "repo:owner/repo/pr-1"}},
            {"id": "outcome", "kind": "item_observed", "repo": {"name": "owner/repo"},
             "subject": {"id": "repo:owner/repo/pr-1"}},
        ]
        candidate = {"v": 1, "posture": "stable", "summary": "Evidence is mixed.",
            "dimensions": [{"key": "pr-review-quality", "title": "PR review quality",
                "status": "stable", "summary": "One review has an outcome.",
                "signal_ids": ["action", "outcome"]}],
            "findings": [{"key": "check-authorship", "repo": "owner/repo",
                "category": "pr-review", "assessment": "mixed", "confidence": "high",
                "title": "Approval authorship was ambiguous", "original_judgment": "Called it reviewed.",
                "observed_outcome": "The same steward posted the approval.",
                "critique": "It was not independent corroboration.",
                "recommendation": "Check signature and author.",
                "signal_ids": ["action", "outcome"]}],
            "lessons": [{"key": "verify-review-author", "repo": "owner/repo",
                "guidance": "Verify approval authorship before citing review.",
                "applies_to": "PRs described as independently reviewed", "confidence": "high",
                "finding_keys": ["check-authorship"]}]}
        report, lessons = validate_evaluation(candidate, records, {"owner/repo"})
        self.assertEqual("evaluation:owner/repo:check-authorship", report["findings"][0]["id"])
        self.assertEqual("lesson:owner/repo:verify-review-author", lessons["lessons"][0]["id"])

        candidate["findings"][0]["signal_ids"] = ["outcome", "missing"]
        with self.assertRaisesRegex(InvalidEvaluation, "unknown signal"):
            validate_evaluation(candidate, records, {"owner/repo"})

    def test_self_evaluation_rejects_unlinked_lesson(self):
        records = [{"id": "a", "kind": "steward_event", "repo": {"name": "owner/repo"}},
                   {"id": "b", "kind": "item_observed", "repo": {"name": "owner/repo"}}]
        candidate = {"v": 1, "posture": "insufficient-data", "summary": "Too early.",
                     "dimensions": [], "findings": [],
                     "lessons": [{"key": "generic", "repo": "owner/repo",
                                   "guidance": "Be careful.", "applies_to": "everything",
                                   "confidence": "low", "finding_keys": ["not-a-finding"]}]}
        with self.assertRaisesRegex(InvalidEvaluation, "must cite current findings"):
            validate_evaluation(candidate, records, {"owner/repo"})

    def test_self_evaluation_page_has_its_own_section_contract(self):
        page = Path(__file__).with_name("evaluation.html").read_text(encoding="utf-8")
        self.assertIn("/api/evaluation", page)
        self.assertIn("Critical findings", page)
        self.assertIn("Lessons fed into future ticks", page)

    def test_selected_insight_materializes_and_control_changes_are_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idea = {"id": "idea:owner/repo:one", "title": "One", "problem": "P",
                    "rationale": "R", "scope": "small", "risk": "low",
                    "suggested_next_action": "investigate", "signal_ids": ["sig_1"]}
            write(root / "insights.json", {"repositories": [{"name": "owner/repo",
                  "themes": [{"id": "theme:owner/repo:a", "title": "A", "ideas": [idea]}]}]})
            (root / "insight-decisions.jsonl").write_text(
                '{"idea_id":"idea:owner/repo:one","action":"select","ts":"2026-08-20T01:00:00Z"}\n',
                encoding="utf-8")
            first = sync_proactive(root, "2026-08-20T01:01:00Z")
            self.assertEqual(1, first["selected"])
            queue = json.loads((root / "proactive.json").read_text())
            self.assertEqual("selected", queue["items"][idea["id"]]["status"])

            with (root / "insight-decisions.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"idea_id":"idea:owner/repo:one","action":"defer",'
                             '"ts":"2026-08-20T02:00:00Z","note":"next release"}\n')
            sync_proactive(root, "2026-08-20T02:01:00Z")
            queue = json.loads((root / "proactive.json").read_text())
            self.assertEqual("deferred", queue["items"][idea["id"]]["status"])
            self.assertEqual("next release", queue["items"][idea["id"]]["control_note"])

    def test_selected_idea_missing_from_new_graph_is_superseded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "insights.json", {"repositories": []})
            write(root / "proactive.json", {"items": {
                "idea:gone": {"id": "idea:gone", "status": "selected"}}})
            result = sync_proactive(root, "2026-08-20T03:00:00Z")
            queue = json.loads((root / "proactive.json").read_text())
            self.assertEqual(1, result["changed"])
            self.assertEqual("superseded", queue["items"]["idea:gone"]["status"])

    def test_proactive_sync_refuses_corrupt_durable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "insights.json").write_text('{"repositories": []}', encoding="utf-8")
            (root / "proactive.json").write_text('{broken', encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                sync_proactive(root, "2026-08-20T03:00:00Z")
            self.assertEqual('{broken', (root / "proactive.json").read_text())

    def test_insight_decisions_are_latest_wins_and_resettable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "insight-decisions.jsonl").write_text(
                '{"idea_id":"idea:one","action":"select","ts":"1"}\n'
                '{"idea_id":"idea:two","action":"dismiss","ts":"2"}\n'
                '{"idea_id":"idea:one","action":"defer","ts":"3"}\n'
                '{"idea_id":"idea:two","action":"reset","ts":"4"}\n', encoding="utf-8")
            decisions = insight_decisions(root)
            self.assertEqual("defer", decisions["idea:one"]["action"])
            self.assertNotIn("idea:two", decisions)

    def test_find_insight_idea_returns_its_parent_nodes(self):
        idea = {"id": "idea:owner/repo:one"}
        theme = {"id": "theme:owner/repo:a", "ideas": [idea]}
        repo = {"id": "repo:owner/repo", "themes": [theme]}
        self.assertEqual((repo, theme, idea), find_insight_idea({"repositories": [repo]}, idea["id"]))

    def test_insights_canvas_exposes_graph_and_decision_controls(self):
        page = Path(__file__).with_name("insights.html").read_text(encoding="utf-8")
        self.assertIn("/api/insights", page)
        self.assertIn("/api/insight-decision", page)
        self.assertIn("repository → theme → potential idea", page)
        self.assertIn('data-action="select"', page)

    def test_insight_validation_builds_stable_nodes_from_real_evidence(self):
        records = []
        for number in range(1, 4):
            records.append({
                "id": f"sig_{number}", "kind": "item_observed",
                "repo": {"name": "owner/llmfit"},
                "subject": {"id": f"repo:owner/llmfit/issue-{number}"},
            })
        candidate = {"v": 1, "generated_at": "2026-08-20T00:00:00Z", "repositories": [{
            "name": "owner/llmfit", "posture": "heating", "summary": "Reports are rising.",
            "themes": [{
                "key": "session-expiry", "title": "Sessions expire unexpectedly",
                "summary": "Three reports describe the same interrupted workflow.",
                "state": "persistent", "confidence": "high", "momentum": "Three recent reports.",
                "signal_ids": ["sig_1", "sig_2", "sig_3"],
                "ideas": [{
                    "key": "resume-session-reliably", "title": "Reliable session resumption",
                    "problem": "Users lose work after sleep.", "state": "proposed",
                    "rationale": "All three reports share the failure mode.",
                    "scope": "medium — authentication lifecycle", "risk": "Token-provider variance.",
                    "suggested_next_action": "Design and test refresh recovery.",
                    "signal_ids": ["sig_1", "sig_2", "sig_3"],
                }],
            }],
        }]}
        result = validate_insights(candidate, records, {"owner/llmfit"})
        theme = result["repositories"][0]["themes"][0]
        self.assertEqual("theme:owner/llmfit:session-expiry", theme["id"])
        self.assertEqual("idea:owner/llmfit:resume-session-reliably", theme["ideas"][0]["id"])
        self.assertEqual(3, theme["distinct_items"])

    def test_insight_validation_rejects_unsupported_recurrence(self):
        records = [{"id": "sig_1", "kind": "item_observed",
                    "repo": {"name": "owner/llmfit"},
                    "subject": {"id": "repo:owner/llmfit/issue-1"}}]
        candidate = {"v": 1, "repositories": [{
            "name": "owner/llmfit", "posture": "stable", "summary": "Quiet.",
            "themes": [{"key": "one-report", "title": "One report", "summary": "Only one.",
                        "state": "recurring", "confidence": "low", "momentum": "Unknown.",
                        "signal_ids": ["sig_1"], "ideas": []}],
        }]}
        with self.assertRaisesRegex(InvalidInsights, "requires 2 distinct"):
            validate_insights(candidate, records, {"owner/llmfit"})

    def test_insight_validation_counts_ref_scoped_events_but_not_metrics(self):
        records = [
            {"id": "event_1", "kind": "steward_event", "repo": {"name": "owner/llmfit"},
             "subject": {"id": "repo:owner/llmfit/pr-1"}},
            {"id": "event_2", "kind": "steward_event", "repo": {"name": "owner/llmfit"},
             "subject": {"id": "repo:owner/llmfit/pr-2"}},
            {"id": "metric", "kind": "repository_metric", "repo": {"name": "owner/llmfit"},
             "subject": {"id": "repo:owner/llmfit"}},
        ]
        candidate = {"v": 1, "repositories": [{
            "name": "owner/llmfit", "posture": "stable", "summary": "Two PR events.",
            "themes": [{"key": "review-pattern", "title": "Review pattern",
                        "summary": "Two PRs share a pattern.", "state": "recurring",
                        "confidence": "medium", "momentum": "Current window.",
                        "signal_ids": ["event_1", "event_2", "metric"], "ideas": []}],
        }]}
        result = validate_insights(candidate, records, {"owner/llmfit"})
        self.assertEqual(2, result["repositories"][0]["themes"][0]["distinct_items"])

    def test_insight_context_keeps_latest_item_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text("repos:\n  - name: owner/llmfit\n")
            rows = [
                {"id": "old", "ts": "2026-08-18T00:00:00Z", "kind": "item_observed",
                 "repo": {"name": "owner/llmfit"}, "subject": {"id": "same"},
                 "source": {"updated_at": "2026-08-18T00:00:00Z"}},
                {"id": "new", "ts": "2026-08-19T00:00:00Z", "kind": "item_observed",
                 "repo": {"name": "owner/llmfit"}, "subject": {"id": "same"},
                 "source": {"updated_at": "2026-08-19T00:00:00Z"}},
            ]
            (root / "signals.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            context = prepare_insights(root)
            self.assertEqual(["new"], [row["id"] for row in context["signals"]])

    def test_signal_collection_is_deduplicated_and_keeps_evidence_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            (root / "config.yaml").write_text(
                "repos:\n  - name: owner/llmfit\n", encoding="utf-8")
            write(root / "state" / "llmfit.json", {"items": {"issue-7": {
                "type": "issue", "title": "Login expires after sleep",
                "body": "Steps to reproduce", "url": "https://example.test/7",
                "status": "triaged", "github_updated_at": "2026-08-19T00:00:00Z",
                "comments": [{"author": {"login": "ada"}, "body": "Still occurs"}],
                "signal": {"intent": "bug", "topics": ["authentication"],
                           "confidence": "high"},
            }}})
            (root / "audit.jsonl").write_text(
                '{"ts":"2026-08-19T00:01:00Z","event":"steward_action",'
                '"repo":"llmfit","ref":"issue-7","kind":"posted",'
                '"summary":"asked for a trace"}\n', encoding="utf-8")
            (root / "metrics.jsonl").write_text(
                '{"ts":"2026-08-19T00:02:00Z","repo":"llmfit","open_issues":1}\n',
                encoding="utf-8")

            first = collect_signals(root, "2026-08-19T00:03:00Z")
            second = collect_signals(root, "2026-08-19T00:04:00Z")
            records = query_signals(root / "signals.jsonl", repo="owner/llmfit", limit=20)

            self.assertEqual(3, first["added"])
            self.assertEqual(0, second["added"])
            item = next(record for record in records if record["kind"] == "item_observed")
            self.assertEqual("repo:owner/llmfit", item["repo"]["id"])
            self.assertEqual("repo:owner/llmfit/issue-7", item["subject"]["id"])
            self.assertEqual("Steps to reproduce", item["evidence"][0]["text"])
            self.assertEqual("bug", item["analysis"]["intent"])
            self.assertNotIn("analysis", item["evidence"][0])

            state = json.loads((root / "state" / "llmfit.json").read_text())
            state["items"]["issue-7"]["status"] = "posted"
            write(root / "state" / "llmfit.json", state)
            changed = collect_signals(root, "2026-08-19T00:05:00Z")
            self.assertEqual(1, changed["added"])

    def test_repair_preserves_workflow_and_fresh_github_fields(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            before, state = root / "before", root / "state"
            before.mkdir(); state.mkdir()
            write(before / "llmfit.json", {"cursor": "old", "items": {"issue-1": {
                "status": "posted", "iterations": 2, "last_action": "asked for logs",
                "last_action_at": "2026-08-18T00:00:00Z", "verdict": None,
                "staged_actions": [], "notes": "waiting",
                "review_records": [{"id": "preserve-me"}],
                "github_updated_at": "2026-08-18T00:00:00Z"}}})
            write(state / "llmfit.json", {"cursor": "new", "items": {"issue-1": {
                "status": "backlog", "iterations": 0, "last_action": None,
                "last_action_at": None, "verdict": None, "staged_actions": [],
                "notes": "new reply", "github_updated_at": "2026-08-19T00:00:00Z"}}})
            result = repair_ledgers(before, state)
            item = json.loads((state / "llmfit.json").read_text())["items"]["issue-1"]
            self.assertEqual(["llmfit:issue-1"], result["repaired_items"])
            self.assertEqual("posted", item["status"])
            self.assertEqual("asked for logs", item["last_action"])
            self.assertEqual("2026-08-19T00:00:00Z", item["github_updated_at"])
            self.assertEqual("waiting new reply", item["notes"])
            self.assertEqual("preserve-me", item["review_records"][0]["id"])

    def test_check_rejects_sync_only_with_actionable_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"; state.mkdir()
            write(state / "llmfit.json", {"items": {"issue-1": {
                "status": "backlog", "last_action": None,
                "last_activity_at": "2026-08-18T00:00:00Z"}}})
            (root / "config.yaml").write_text("activity_floor_days: 90\n")
            (root / "activity.jsonl").write_text(
                '{"kind":"observed","ref":"pr-2"}\n', encoding="utf-8")
            result = check_tick(state, root / "config.yaml", root / "activity.jsonl",
                                "2026-08-19T00:00:00Z")
            self.assertTrue(result["sync_only_failure"])
            self.assertTrue(result["under_budget_failure"])
            self.assertEqual(1, result["actionable_total"])
            self.assertEqual(1, result["required_actions"])

    def test_check_rejects_partially_spent_budget(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"; state.mkdir()
            items = {f"issue-{number}": {
                "status": "backlog", "last_action": None,
                "last_activity_at": "2026-08-18T00:00:00Z"
            } for number in range(30)}
            write(state / "llmfit.json", {"items": items})
            (root / "config.yaml").write_text(
                "activity_floor_days: 90\nsubstantive_items_per_tick: 20\n"
                "light_items_per_tick: 40\n", encoding="utf-8")
            (root / "activity.jsonl").write_text(
                "".join(json.dumps({"kind": "posted", "ref": f"issue-{n}"}) + "\n"
                        for n in range(5)), encoding="utf-8")
            result = check_tick(state, root / "config.yaml", root / "activity.jsonl",
                                "2026-08-19T00:00:00Z")
            self.assertEqual(20, result["required_actions"])
            self.assertEqual(5, result["queue_actions"])
            self.assertTrue(result["under_budget_failure"])

    def test_check_rejects_pr_only_budget_when_conversations_wait(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"; state.mkdir()
            items = {
                **{f"pr-{number}": {"type": "pr", "status": "backlog",
                    "last_action": None, "last_activity_at": "2026-08-18T00:00:00Z"}
                   for number in range(20)},
                **{f"issue-{number}": {"type": "issue", "status": "backlog",
                    "last_action": None, "last_activity_at": "2026-08-18T00:00:00Z"}
                   for number in range(5)},
            }
            write(state / "llmfit.json", {"items": items})
            (root / "config.yaml").write_text(
                "activity_floor_days: 90\nsubstantive_items_per_tick: 20\n"
                "light_items_per_tick: 40\nmin_conversation_actions_per_tick: 5\n",
                encoding="utf-8")
            (root / "activity.jsonl").write_text(
                "".join(json.dumps({"kind": "posted", "ref": f"pr-{n}"}) + "\n"
                        for n in range(20)), encoding="utf-8")
            result = check_tick(state, root / "config.yaml", root / "activity.jsonl",
                                "2026-08-19T00:00:00Z")
            self.assertEqual(20, result["queue_actions"])
            self.assertEqual(0, result["conversation_actions"])
            self.assertEqual(5, result["required_conversation_actions"])
            self.assertTrue(result["under_budget_failure"])

    def test_audit_recovery_only_changes_blank_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"; state.mkdir()
            write(state / "llmfit.json", {"items": {
                "issue-1": {"type": "issue", "status": "backlog", "iterations": 0,
                            "last_action": None, "last_action_at": None, "verdict": None,
                            "staged_actions": []},
                "issue-2": {"type": "issue", "status": "backlog", "iterations": 0,
                            "last_action": "keep", "last_action_at": "old", "verdict": None,
                            "staged_actions": []}}})
            audit = root / "audit.jsonl"
            audit.write_text(
                '{"event":"steward_action","kind":"posted","repo":"llmfit",'
                '"ref":"issue-1","ts":"2026-08-18T00:00:00Z","summary":"asked"}\n'
                '{"event":"steward_action","kind":"posted","repo":"llmfit",'
                '"ref":"issue-2","ts":"2026-08-18T00:00:00Z","summary":"overwrite"}\n')
            result = recover_from_audit(state, audit)
            items = json.loads((state / "llmfit.json").read_text())["items"]
            self.assertEqual(["llmfit:issue-1"], result["recovered_items"])
            self.assertEqual("posted", items["issue-1"]["status"])
            self.assertEqual("keep", items["issue-2"]["last_action"])

    def test_tick_runner_returns_77_for_sync_only_run(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            shutil.copy(Path(__file__).with_name("tick.sh"), root / "tick.sh")
            shutil.copy(Path(__file__).with_name("tick_guard.py"), root / "tick_guard.py")
            shutil.copy(Path(__file__).with_name("render_dashboard.py"), root / "render_dashboard.py")
            shutil.copy(Path(__file__).with_name("signals.py"), root / "signals.py")
            shutil.copy(Path(__file__).with_name("proactive.py"), root / "proactive.py")
            shutil.copy(Path(__file__).with_name("dashboard-first-run.html"),
                        root / "dashboard-first-run.html")
            (root / "state").mkdir()
            write(root / "state" / "llmfit.json", {"cursor": "old", "items": {
                "issue-1": {"status": "backlog", "last_action": None,
                            "last_activity_at": "2099-01-01T00:00:00Z"}}})
            (root / "config.yaml").write_text(
                "repos:\n  - name: owner/llmfit\nlimits:\n  activity_floor_days: 90\n",
                encoding="utf-8")
            env = os.environ.copy()
            env.update(
                STEWARD_ENGINE="custom",
                STEWARD_ENGINE_CMD="touch state/llmfit.json metrics.jsonl dashboard.html",
            )
            result = subprocess.run(
                ["bash", str(root / "tick.sh")], cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(77, result.returncode, result.stderr)
            signal_rows = [json.loads(line)
                           for line in (root / "signals.jsonl").read_text().splitlines()]
            self.assertTrue(any(row.get("kind") == "item_observed" for row in signal_rows))
            audit = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
            self.assertTrue(any(event.get("data", {}).get("reason") == "queue_budget_underused"
                                for event in audit))

    def test_tick_prompt_identifies_parent_service_as_own_launcher(self):
        source = Path(__file__).with_name("tick.sh").read_text(encoding="utf-8")
        self.assertIn("STEWARD_RUNNING_TICK=1", source)
        self.assertIn("YOUR OWN launcher", source)
        self.assertIn("do not inspect, monitor, wait for", source)

    def test_background_merge_requires_old_approval_at_unchanged_head(self):
        item = {"status": "ready-for-maintainer", "verdict": "approve-recommend",
                "iterations": 0, "head_oid": "abc"}
        pr = {"state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc",
              "reviews": [{"state": "APPROVED", "submittedAt": "2026-08-15T00:00:00Z",
                           "commit": {"oid": "abc"}}]}
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        self.assertEqual((True, "eligible"), auto_merge_candidate(item, pr, 3, now))
        pr["headRefOid"] = "new-head"
        ok, reason = auto_merge_candidate(item, pr, 3, now)
        self.assertFalse(ok)
        self.assertIn("head", reason)

    def test_background_merge_never_updates_behind_branch(self):
        item = {"status": "ready-for-maintainer", "verdict": "approve-recommend",
                "iterations": 0, "head_oid": "abc"}
        pr = {"state": "OPEN", "mergeStateStatus": "BEHIND", "headRefOid": "abc",
              "reviews": [{"state": "APPROVED", "submittedAt": "2026-08-10T00:00:00Z",
                           "commit": {"oid": "abc"}}]}
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        ok, reason = auto_merge_candidate(item, pr, 3, now)
        self.assertFalse(ok)
        self.assertIn("BEHIND", reason)

    def test_dashboard_renderer_emits_valid_control_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "dashboard.html"
            page = render(Path(__file__).parent, output)
            self.assertEqual(1, page.count('<meta charset="utf-8">'))
            self.assertEqual(1, page.count('<main>'))
            self.assertEqual(1, page.count('</main>'))
            self.assertIn('<div class="fleet">', page)
            self.assertIn('data-item="pr-', page)
            self.assertIn('<h2>Next tick <span class="count">', page)
            self.assertIn('id="steward-controls"', page)
            self.assertIn('class="site-nav"', page)
            self.assertIn('href="/evaluation.html"', page)
            self.assertIn('href="/audit.html"', page)
            self.assertIn('aria-current="page">Operations', page)
            self.assertNotIn('([.items[]', page)


if __name__ == "__main__":
    unittest.main()
