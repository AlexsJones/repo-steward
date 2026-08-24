#!/usr/bin/env python3
import tempfile
import unittest
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from unittest.mock import Mock, patch

import server


class DashboardDeliveryTests(unittest.TestCase):
    def cache_header_for(self, path):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.send_header = Mock()
        with patch.object(SimpleHTTPRequestHandler, "end_headers"):
            handler.end_headers()
        return handler.send_header.call_args_list

    def test_dashboard_assets_are_revalidated(self):
        headers = self.cache_header_for("/steward-controls.js")
        self.assertIn(unittest.mock.call("Cache-Control", "no-cache"), headers)

    def test_api_responses_are_not_cached(self):
        headers = self.cache_header_for("/api/status")
        self.assertIn(unittest.mock.call("Cache-Control", "no-store"), headers)

    def test_settings_panel_tracks_wrapped_header_and_keeps_save_visible(self):
        source = Path(__file__).with_name("steward-controls.js").read_text(encoding="utf-8")
        self.assertIn("header.getBoundingClientRect().bottom", source)
        self.assertIn("position:sticky;bottom:0", source)
        self.assertIn('z-index:60', source)

    def test_dashboard_uses_a_versioned_controls_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "steward-controls.js").write_text("// current", encoding="utf-8")
            page = server.versioned_dashboard(
                '<script src="/steward-controls.js"></script>', root,
            )
        self.assertRegex(page, r'src="/steward-controls\.js\?v=[0-9a-f]+"')

    def test_settings_writes_limits_before_repositories(self):
        source = Path(__file__).with_name("steward-controls.js").read_text(encoding="utf-8")
        self.assertIn(
            "save = save.then(function () { return postSettings('/api/watch'", source,
        )


if __name__ == "__main__":
    unittest.main()
