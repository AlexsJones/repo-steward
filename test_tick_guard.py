import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tick_guard import check_tick, recover_from_audit, repair_ledgers
from server import auto_merge_candidate
from render_dashboard import render


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


class TickGuardTest(unittest.TestCase):
    def test_repair_preserves_workflow_and_fresh_github_fields(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            before, state = root / "before", root / "state"
            before.mkdir(); state.mkdir()
            write(before / "llmfit.json", {"cursor": "old", "items": {"issue-1": {
                "status": "posted", "iterations": 2, "last_action": "asked for logs",
                "last_action_at": "2026-08-18T00:00:00Z", "verdict": None,
                "staged_actions": [], "notes": "waiting",
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
            self.assertNotIn('([.items[]', page)


if __name__ == "__main__":
    unittest.main()
