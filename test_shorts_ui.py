"""Tests for Stage 5: Shorts UI integration."""
import unittest
from unittest.mock import Mock, patch

from job_store import ROOT, DATA, save_json, load_json


class ShortsUIIntegrationTests(unittest.TestCase):
    """Test that Shorts UI is properly integrated with Highlights UI."""

    def setUp(self):
        self.temp_dir = DATA / "test_shorts_ui"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_notebook_has_two_tabs(self):
        """UI should have Notebook with Highlights and Shorts tabs."""
        import tkinter as tk
        from review_ui import HighlightApp

        root = tk.Tk()
        root.withdraw()  # Don't show window
        app = HighlightApp(root)

        # Check that notebook exists and has 2 tabs
        self.assertTrue(hasattr(app, "notebook"))
        tab_count = app.notebook.index("end")
        self.assertEqual(tab_count, 2)

        # Check tab titles
        tab0_text = app.notebook.tab(0, "text")
        tab1_text = app.notebook.tab(1, "text")
        self.assertIn("Хайлайты", tab0_text)
        self.assertIn("Shorts", tab1_text)

        root.destroy()

    def test_shorts_table_exists(self):
        """Shorts tab should have its own table."""
        import tkinter as tk
        from review_ui import HighlightApp

        root = tk.Tk()
        root.withdraw()
        app = HighlightApp(root)

        self.assertTrue(hasattr(app, "shorts_table"))
        self.assertTrue(hasattr(app, "table"))

        root.destroy()

    def test_shorts_settings_exist(self):
        """Shorts settings should be present in UI."""
        import tkinter as tk
        from review_ui import HighlightApp

        root = tk.Tk()
        root.withdraw()
        app = HighlightApp(root)

        self.assertIn("shorts_min_duration_sec", app.variables)
        self.assertIn("shorts_target_duration_sec", app.variables)
        self.assertIn("shorts_max_duration_sec", app.variables)
        self.assertTrue(hasattr(app, "shorts_enabled_var"))

        root.destroy()

    def test_render_shorts_button_exists(self):
        """Render Shorts button should exist."""
        import tkinter as tk
        from review_ui import HighlightApp

        root = tk.Tk()
        root.withdraw()
        app = HighlightApp(root)

        self.assertTrue(hasattr(app, "render_shorts"))

        root.destroy()

    def test_shorts_candidate_methods_exist(self):
        """Shorts candidate methods should exist."""
        import tkinter as tk
        from review_ui import HighlightApp

        root = tk.Tk()
        root.withdraw()
        app = HighlightApp(root)

        self.assertTrue(hasattr(app, "current_shorts_candidate"))
        self.assertTrue(hasattr(app, "refresh_shorts_table"))
        self.assertTrue(hasattr(app, "show_shorts_candidate"))
        self.assertTrue(hasattr(app, "edited_shorts_candidate"))
        self.assertTrue(hasattr(app, "decide_shorts"))

        root.destroy()


class ShortsReportIntegrationTests(unittest.TestCase):
    """Test that Shorts candidates are properly loaded from reports."""

    def setUp(self):
        self.temp_dir = DATA / "test_shorts_report"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_report_with_shorts_candidates(self):
        """Reports with shorts_candidates field should be loaded correctly."""
        from app_backend import open_report

        # Create a mock report with shorts candidates
        report = {
            "schema": 1,
            "video_id": "test_video",
            "signature": "test_sig",
            "settings": {},
            "profiles": {
                "highlights": {"aspect_ratio": "16:9", "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5},
                "shorts": {"enabled": True, "aspect_ratio": "9:16", "min_duration_sec": 20, "target_duration_sec": 40, "max_duration_sec": 60, "crop_mode": "center", "subtitles_enabled": True, "max_candidates": 30, "final_candidates": 10}
            },
            "candidates": [
                {"candidate_id": "c001", "start": 60, "end": 180, "score": 8, "title": "Highlight 1", "reason": "Test", "status": "pending", "format": "highlight"}
            ],
            "shorts_candidates": [
                {"candidate_id": "s001", "start": 100, "end": 140, "score": 9, "title": "Short 1", "reason": "Test short", "status": "pending", "format": "short"}
            ]
        }

        # Create manifest
        manifest = {
            "video_id": "test_video",
            "media": {"duration_sec": 300.0},
            "source_path": "test.mp4"
        }

        # Create transcript
        transcript = {"sentences": []}

        # Save all files
        report_path = self.temp_dir / "analysis" / "test_sig" / "report.json"
        save_json(report_path, report)
        save_json(self.temp_dir / "source.json", manifest)
        save_json(self.temp_dir / "analysis" / "test_sig" / "transcript.json", transcript)

        # Load report
        context = open_report(report_path)

        # Verify shorts candidates are present
        self.assertIn("shorts_candidates", context["report"])
        self.assertEqual(len(context["report"]["shorts_candidates"]), 1)
        self.assertEqual(context["report"]["shorts_candidates"][0]["candidate_id"], "s001")

    def test_legacy_report_without_shorts(self):
        """Legacy reports without shorts_candidates should still work."""
        from app_backend import open_report

        # Create a legacy report without shorts
        report = {
            "schema": 1,
            "video_id": "test_video",
            "signature": "test_sig",
            "settings": {},
            "candidates": [
                {"candidate_id": "c001", "start": 60, "end": 180, "score": 8, "title": "Highlight 1", "reason": "Test", "status": "pending", "format": "highlight"}
            ]
        }

        manifest = {
            "video_id": "test_video",
            "media": {"duration_sec": 300.0},
            "source_path": "test.mp4"
        }

        transcript = {"sentences": []}

        report_path = self.temp_dir / "analysis" / "test_sig" / "report.json"
        save_json(report_path, report)
        save_json(self.temp_dir / "source.json", manifest)
        save_json(self.temp_dir / "analysis" / "test_sig" / "transcript.json", transcript)

        # Load report
        context = open_report(report_path)

        # Should not crash, and shorts_candidates should be absent or empty
        self.assertIn("shorts_candidates", context["report"] or {})
        shorts = context["report"].get("shorts_candidates", [])
        self.assertIsInstance(shorts, list)


if __name__ == "__main__":
    unittest.main()
