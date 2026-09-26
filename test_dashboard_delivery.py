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

    def test_settings_popover_sits_under_the_bar_and_keeps_save_visible(self):
        css = Path(__file__).parent.joinpath("assets", "steward.css").read_text(encoding="utf-8")
        popover = css[css.index(".popover {"):css.index("}", css.index(".popover {"))]
        self.assertIn("top: calc(var(--sys-h)", popover)
        self.assertIn("z-index: 60", popover)
        save = css[css.index(".popover .save {"):css.index("}", css.index(".popover .save {"))]
        self.assertIn("position: sticky; bottom: 0", save)

    def test_dashboard_uses_a_versioned_controls_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "steward-controls.js").write_text("// current", encoding="utf-8")
            page = server.versioned_dashboard(
                '<script src="/steward-controls.js"></script>', root,
            )
        self.assertRegex(page, r'src="/steward-controls\.js\?v=[0-9a-f]+"')

    def test_settings_writes_limits_before_repositories(self):
        source = Path(__file__).parent.joinpath("assets", "steward-shell.js").read_text(encoding="utf-8")
        self.assertLess(source.index("api('/api/limits'"),
                        source.index("save = save.then(function () { return api('/api/watch'"))

    def test_every_page_uses_the_shared_shell_and_stylesheet(self):
        root = Path(__file__).parent
        for name in ("dashboard-first-run.html", "insights.html", "evaluation.html",
                     "metrics.html", "audit.html"):
            page = root.joinpath(name).read_text(encoding="utf-8")
            self.assertIn('href="/assets/steward.css"', page, name)
            self.assertIn('src="/assets/steward-shell.js"', page, name)
            self.assertRegex(page, r'<body data-page="[a-z]+"', name)
            self.assertNotIn("--bg:", page, name)  # the palette lives only in steward.css


if __name__ == "__main__":
    unittest.main()
