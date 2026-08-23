import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


UNIT = """[Service]
Environment=STEWARD_ENGINE=claude
Environment=STEWARD_ENGINE_BIN=/opt/bin/claude
Environment=STEWARD_MODEL=claude-opus-5
Environment=PATH=/opt/bin:/usr/bin
ExecStart=/repo/tick.sh
"""


class BackendConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.unit_dir = Path(self.temp.name)
        self.service = self.unit_dir / "repo-steward.service"
        self.service.write_text(UNIT, encoding="utf-8")
        self.unit_patch = mock.patch.object(server, "UNIT_DIR", self.unit_dir)
        self.unit_patch.start()

    def tearDown(self):
        self.unit_patch.stop()
        self.temp.cleanup()

    @mock.patch.object(server.shutil, "which")
    def test_current_backend_reports_unit_and_available_options(self, which):
        which.side_effect = lambda value: f"/opt/bin/{value}" if value in {"claude", "codex"} else None

        result = server.current_backend()

        self.assertEqual(result["value"], "claude")
        self.assertEqual(result["label"], "Claude Code")
        self.assertEqual(result["model"], "claude-opus-5")
        availability = {item["value"]: item["available"] for item in result["options"]}
        self.assertEqual(availability, {
            "claude": True, "codex": True, "gemini": False, "opencode": False,
        })

    @mock.patch.object(server.subprocess, "run")
    @mock.patch.object(server.shutil, "which")
    def test_switch_updates_binary_and_clears_provider_specific_model(self, which, run):
        which.side_effect = lambda value: f"/new/bin/{value}"
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

        ok, result = server.set_backend("codex")

        self.assertTrue(ok)
        self.assertEqual(result["value"], "codex")
        text = self.service.read_text(encoding="utf-8")
        self.assertIn("Environment=STEWARD_ENGINE=codex\n", text)
        self.assertIn("Environment=STEWARD_ENGINE_BIN=/new/bin/codex\n", text)
        self.assertNotIn("STEWARD_MODEL", text)
        run.assert_called_once_with(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)

    @mock.patch.object(server.subprocess, "run")
    @mock.patch.object(server.shutil, "which", return_value="/new/bin/codex")
    def test_failed_reload_restores_the_original_unit(self, _which, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "reload failed"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]

        ok, error = server.set_backend("codex")

        self.assertFalse(ok)
        self.assertEqual(error, "reload failed")
        self.assertEqual(self.service.read_text(encoding="utf-8"), UNIT)

    @mock.patch.object(server.shutil, "which", return_value=None)
    def test_uninstalled_backend_is_refused_without_writing(self, _which):
        ok, error = server.set_backend("gemini")

        self.assertFalse(ok)
        self.assertIn("not installed", error)
        self.assertEqual(self.service.read_text(encoding="utf-8"), UNIT)

    @mock.patch.object(server.subprocess, "run")
    def test_preconfigured_custom_backend_can_be_selected(self, run):
        self.service.write_text(
            UNIT + 'Environment=STEWARD_ENGINE_CMD=my-agent --prompt "$PROMPT"\n',
            encoding="utf-8")
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

        ok, result = server.set_backend("custom")

        self.assertTrue(ok)
        self.assertEqual(result["value"], "custom")
        self.assertEqual(result["label"], "Custom command")
        text = self.service.read_text(encoding="utf-8")
        self.assertNotIn("STEWARD_ENGINE_BIN", text)
        self.assertIn("STEWARD_ENGINE_CMD", text)


if __name__ == "__main__":
    unittest.main()
