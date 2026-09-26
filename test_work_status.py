#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class WorkStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {"tick_active": False, "decide_active": False,
                            "analysis_running": False}.items():
            patcher = mock.patch.object(server, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        schedule = mock.patch.object(server, "current_schedule", return_value={"label": "Manual only"})
        schedule.start()
        self.addCleanup(schedule.stop)

    def write(self, name, rows):
        (self.root / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_idle_has_no_lock(self):
        work = server.work_status(self.root)
        self.assertIsNone(work["ledger_lock"])
        self.assertEqual([], work["running"])

    def test_decision_runner_holds_the_ledgers_and_names_its_work(self):
        self.write("decisions.jsonl", [{"ts": "t1", "title": "llmfit #916 badge", "status": "pending"}])
        self.write("activity.jsonl", [{"repo": "llmfit", "ref": "pr-916", "summary": "closed as declined"}])
        (self.root / ".decide.pid").write_text("1")
        server.decide_active.return_value = True
        work = server.work_status(self.root)
        self.assertEqual("decide", work["ledger_lock"]["kind"])
        self.assertIn("llmfit #916", work["ledger_lock"]["detail"])
        self.assertEqual("closed as declined", work["running"][0]["steps"][-1]["msg"])
        self.assertEqual([], work["queued"])

    def test_pending_decisions_queue_while_idle_and_clarifications_surface(self):
        self.write("decisions.jsonl", [
            {"ts": "t1", "title": "waiting one", "status": "pending"},
            {"ts": "t2", "title": "unclear one", "status": "pending", "note": "which PR?"},
            {"ts": "t3", "title": "done one", "status": "executed"}])
        kinds = [q["kind"] for q in server.work_status(self.root)["queued"]]
        self.assertEqual(["decision", "clarify"], kinds)

    def test_builds_run_without_locking(self):
        (self.root / "builds.json").write_text(json.dumps({"items": {
            "a": {"status": "building", "title": "Router mode", "repo": "o/r", "started_at": "2026-09-25T10:00:00Z"},
            "b": {"status": "queued", "title": "Next", "repo": "o/r", "requested_at": "2026-09-25T10:01:00Z"}}}))
        work = server.work_status(self.root)
        self.assertIsNone(work["ledger_lock"])
        self.assertEqual(["build"], [r["kind"] for r in work["running"]])
        self.assertEqual(["build"], [q["kind"] for q in work["queued"]])

    def test_recent_work_is_newest_first_and_marks_failures(self):
        self.write("audit.jsonl", [
            {"ts": "2026-09-26T16:11:36Z", "event": "decide_done", "summary": "decision run finished"},
            {"ts": "2026-09-26T16:12:55Z", "event": "decision_executed", "repo": "llmfit", "summary": "declined #916"},
            {"ts": "2026-09-26T16:22:23Z", "event": "approve", "ok": False, "repo": "k8sgpt", "summary": "approved"},
            {"ts": "2026-09-26T16:30:00Z", "event": "config_change", "summary": "not work"}])
        recent = server.recent_work(self.root)
        self.assertEqual(["approve", "decision", "decisions"], [r["kind"] for r in recent])
        self.assertFalse(recent[0]["ok"])

    def test_busy_error_names_the_holder(self):
        with mock.patch.object(server, "work_status", return_value={"ledger_lock": {
                "kind": "tick", "title": "Tick", "elapsed_sec": 300, "detail": ""}}):
            message = server.busy_error("Try again")
        self.assertIn("a tick is running (5m so far)", message)
        self.assertIn("Work panel", message)


if __name__ == "__main__":
    unittest.main()
