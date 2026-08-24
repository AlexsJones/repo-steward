#!/usr/bin/env python3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import server


class LimitsConfigTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "config.yaml"
        self.root_patch = patch.object(server, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.tempdir.cleanup()

    def write_config(self, proactive=None):
        proactive_line = ("  proactive_items_per_tick: " + str(proactive) + "\n"
                          if proactive is not None else "")
        self.config.write_text(
            "mode: draft\n"
            "limits:\n"
            "  # keep this comment\n"
            "  substantive_items_per_tick: 4  # deep work\n"
            "  light_items_per_tick: 12       # light work\n"
            + proactive_line
            + "  max_fix_prs_open: 3\n",
            encoding="utf-8",
        )

    def test_missing_work_queue_limit_defaults_to_one(self):
        self.write_config()
        self.assertEqual(server.read_limits(), {
            "substantive": 4, "light": 12, "proactive": 1,
        })

    def test_setting_limits_adds_work_queue_limit_to_older_config(self):
        self.write_config()
        ok, limits = server.set_limits(8, 24, 3)
        self.assertTrue(ok)
        self.assertEqual(limits, {"substantive": 8, "light": 24, "proactive": 3})
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("  light_items_per_tick: 24       # light work\n"
                      "  proactive_items_per_tick: 3\n", text)
        self.assertIn("# keep this comment", text)
        self.assertIn("max_fix_prs_open: 3", text)

    def test_setting_limits_updates_existing_work_queue_limit(self):
        self.write_config(proactive=1)
        ok, limits = server.set_limits(6, 18, 0)
        self.assertTrue(ok)
        self.assertEqual(limits["proactive"], 0)
        self.assertEqual(self.config.read_text().count("proactive_items_per_tick"), 1)
        self.assertIn("proactive_items_per_tick: 0", self.config.read_text())

    def test_work_queue_limit_is_bounded(self):
        self.write_config(proactive=1)
        before = self.config.read_text()
        ok, error = server.set_limits(4, 12, 21)
        self.assertFalse(ok)
        self.assertIn("work queue 0-20", error)
        self.assertEqual(self.config.read_text(), before)

    def test_concurrent_limit_and_repo_updates_do_not_corrupt_config(self):
        self.config.write_text(
            "repos:\n"
            "  - name: owner/example\n"
            "    priority: medium\n"
            "    watch: [issues, prs]\n"
            "limits:\n"
            "  substantive_items_per_tick: 4\n"
            "  light_items_per_tick: 12\n"
            "  proactive_items_per_tick: 1\n",
            encoding="utf-8",
        )
        operations = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for value in range(1, 17):
                operations.append(pool.submit(server.set_limits, value, value, value % 4))
                operations.append(pool.submit(
                    server.set_watch, "owner/example", ["issues", "discussions"], "high"))
        self.assertTrue(all(result.result()[0] for result in operations))
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("name: owner/example", text)
        self.assertIn("watch: [issues, discussions]", text)
        self.assertIn("proactive_items_per_tick:", text)


if __name__ == "__main__":
    unittest.main()
