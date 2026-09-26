#!/usr/bin/env python3
import fcntl
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class AnalysisJobBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "logs").mkdir()
        self.unit_dir = self.root / "units"
        self.unit_dir.mkdir()
        (self.unit_dir / "repo-steward.service").write_text(
            "[Service]\n"
            "EnvironmentFile=/home/me/.config/repo-steward/env\n"
            "Environment=STEWARD_ENGINE=claude\n"
            "Environment=STEWARD_ENGINE_BIN=/opt/bin/claude\n"
            "Environment=PATH=/opt/bin:/usr/bin\n"
            "Environment=UNRELATED=1\n"
            "ExecStart=/repo/tick.sh\n", encoding="utf-8")
        patches = [mock.patch.object(server, "UNIT_DIR", self.unit_dir),
                   mock.patch.dict(server.ANALYSIS_LAUNCHED, clear=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def log(self, job, text):
        (self.root / "logs" / server.ANALYSIS_JOBS[job]["log"]).write_text(text, encoding="utf-8")


class AnalysisJobTests(AnalysisJobBase):
    def test_jobs_are_tracked_independently_by_their_own_lock(self):
        with open(self.root / ".insights.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            status = server.analysis_status(self.root)
        self.assertTrue(status["insights"]["running"])
        self.assertIsNotNone(status["insights"]["started_at"])
        self.assertFalse(status["evaluation"]["running"])
        self.assertFalse(server.analysis_running("insights", self.root))

    def test_last_result_reads_published_rejected_and_timeout(self):
        self.log("insights", "=== insights 2026-09-01T00:00:00Z engine=claude (rc=0) ===\n"
                             "=== insights 2026-09-01T00:00:00Z published: {} ===\n"
                             "=== insights 2026-09-02T00:00:00Z skipped: already running ===\n")
        self.assertEqual(server.analysis_last_result("insights", self.root),
                         {"ts": "2026-09-01T00:00:00Z", "outcome": "published"})
        self.log("evaluation", "=== evaluation 2026-09-03T00:00:00Z failed (rc=124) ===\n")
        self.assertEqual(server.analysis_last_result("evaluation", self.root)["detail"], "timed out")
        self.log("evaluation", "=== evaluation 2026-09-03T00:00:00Z candidate rejected ===\n")
        self.assertEqual(server.analysis_last_result("evaluation", self.root)["outcome"], "rejected")

    def test_newer_published_output_overrides_an_older_logged_failure(self):
        self.log("insights", "=== insights 2026-09-01T00:00:00Z candidate rejected ===\n")
        (self.root / "insights.json").write_text(
            json.dumps({"generated_at": "2026-09-01T00:05:00Z"}), encoding="utf-8")
        self.assertEqual(server.analysis_last_result("insights", self.root),
                         {"ts": "2026-09-01T00:05:00Z", "outcome": "published"})

    def test_tick_unit_environment_passes_engine_settings_only(self):
        self.assertEqual(server.tick_unit_environment(), [
            "EnvironmentFile=/home/me/.config/repo-steward/env",
            "Environment=STEWARD_ENGINE=claude",
            "Environment=STEWARD_ENGINE_BIN=/opt/bin/claude",
            "Environment=PATH=/opt/bin:/usr/bin",
        ])

    @mock.patch.object(server.subprocess, "run")
    def test_start_launches_transient_unit_and_marks_running(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        ok, error = server.start_analysis("evaluation", self.root)
        self.assertTrue(ok, error)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["systemd-run", "--user", "--no-block"])
        self.assertIn("--property=Environment=STEWARD_ENGINE=claude", cmd)
        self.assertEqual(cmd[-2:], ["/bin/bash", str(self.root / "evaluate.sh")])
        self.assertTrue(server.analysis_running("evaluation", self.root))
        self.assertFalse(server.analysis_running("insights", self.root))

    @mock.patch.object(server.subprocess, "run")
    def test_failed_launch_is_reported_and_not_marked_running(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "no user bus")
        self.assertEqual(server.start_analysis("insights", self.root), (False, "no user bus"))
        self.assertFalse(server.analysis_running("insights", self.root))


class CancelTests(AnalysisJobBase):
    def held_lock(self, job, pid=4242):
        lock = self.root / server.ANALYSIS_JOBS[job]["lock"]
        lock.write_text(f"{pid}\n", encoding="utf-8")
        handle = open(lock, "r+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    @mock.patch.object(server.subprocess, "run")
    def test_cancel_stops_the_unit_when_the_dashboard_started_the_run(self, run):
        self.held_lock("insights")
        run.side_effect = [subprocess.CompletedProcess([], 0, "active\n", ""),
                           subprocess.CompletedProcess([], 0, "", "")]
        ok, error = server.cancel_analysis("insights", self.root)
        self.assertTrue(ok, error)
        self.assertEqual(run.call_args_list[1].args[0],
                         ["systemctl", "--user", "stop", "repo-steward-insights.service"])
        self.assertEqual(server.analysis_last_result("insights", self.root)["outcome"], "cancelled")

    @mock.patch.object(server.os, "kill")
    @mock.patch.object(server, "process_tree", return_value=[99, 4242])
    @mock.patch.object(server.subprocess, "run",
                       return_value=subprocess.CompletedProcess([], 0, "inactive\n", ""))
    def test_cancel_signals_the_process_tree_of_a_make_run(self, _run, tree, kill):
        self.held_lock("evaluation", pid=4242)
        ok, error = server.cancel_analysis("evaluation", self.root)
        self.assertTrue(ok, error)
        tree.assert_called_once_with(4242)
        self.assertEqual([c.args for c in kill.call_args_list],
                         [(99, server.signal.SIGTERM), (4242, server.signal.SIGTERM)])

    def test_cancel_is_refused_when_the_job_is_not_running(self):
        self.assertEqual(server.cancel_analysis("insights", self.root),
                         (False, "no insight sweep is running"))
        self.assertFalse((self.root / "logs" / "insights.log").exists())

    @mock.patch.object(server.subprocess, "run",
                       return_value=subprocess.CompletedProcess([], 0, "inactive\n", ""))
    def test_cancel_reports_a_run_it_cannot_address(self, _run):
        lock = self.root / ".insights.lock"
        lock.write_text("not-a-pid\n", encoding="utf-8")
        handle = open(lock, "r+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        ok, error = server.cancel_analysis("insights", self.root)
        self.assertFalse(ok)
        self.assertIn("left no pid", error)

    def test_a_stopped_run_is_reported_as_cancelled_not_failed(self):
        self.log("insights", "=== insights 2026-09-19T10:00:00Z failed (rc=143) ===\n")
        self.assertEqual(server.analysis_last_result("insights", self.root)["detail"], "stopped")
        self.log("insights", "=== insights 2026-09-19T10:00:00Z published: {} ===\n"
                             "=== insights 2026-09-19T11:00:00Z cancelled by maintainer ===\n")
        self.assertEqual(server.analysis_last_result("insights", self.root),
                         {"ts": "2026-09-19T11:00:00Z", "outcome": "cancelled"})


class ScriptLockTests(unittest.TestCase):
    def test_second_run_is_skipped_while_first_holds_the_lock(self):
        here = Path(__file__).resolve().parent
        for script, lock, log in [("insights.sh", ".insights.lock", "insights.log"),
                                  ("evaluate.sh", ".evaluation.lock", "evaluation.log")]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / script).write_text((here / script).read_text())
                (root / "logs").mkdir()
                with open(root / lock, "w") as held:
                    fcntl.flock(held, fcntl.LOCK_EX)
                    rc = subprocess.run(["bash", str(root / script)], capture_output=True).returncode
                self.assertEqual(rc, 75, script)
                self.assertIn("skipped: already running", (root / "logs" / log).read_text())

    def test_a_run_records_its_pid_in_the_lock_for_cancellation(self):
        here = Path(__file__).resolve().parent
        for script, lock in [("insights.sh", ".insights.lock"), ("evaluate.sh", ".evaluation.lock")]:
            self.assertIn('printf \'%s\\n\' "$$" >&9', (here / script).read_text(), script)
            self.assertIn(lock, (here / script).read_text(), script)


if __name__ == "__main__":
    unittest.main()
