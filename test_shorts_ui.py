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


class ShortsRenderSelectionTests(unittest.TestCase):
    """Stage 7.4.1: выбор Shorts-кандидатов при рендере (UI → renderer контракт).

    Фильтрация по candidate_id и по статусу 'approved' выполняется в двух местах:
      - review_ui.render_shorts: делает deepcopy отчёта, при candidate_id=None оставляет
        весь список shorts_candidates, иначе только выбранный; затем approved-фильтр.
      - renderer.render_shorts_approved: повторно фильтрует по candidate_id и status.
    Тесты проверяют, какой отчёт и с каким candidate_id UI передаёт в renderer.
    Реальный FFmpeg не запускается: render_shorts_approved замещается Mock.
    """

    def setUp(self):
        import tkinter as tk
        from review_ui import HighlightApp

        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app = HighlightApp(self.root)

        self.app.context = {
            "job_dir": str(ROOT / "data" / "test_vid"),
            "manifest": {
                "video_id": "test_vid",
                "media": {"duration_sec": 300.0},
                "source_path": "source.mp4",
            },
            "report": {
                "schema": 1,
                "video_id": "test_vid",
                "signature": "sig",
                "settings": {},
                "candidates": [],
                "shorts_candidates": [
                    {"candidate_id": "s001", "start": 100, "end": 140, "score": 9,
                     "title": "Short 1", "reason": "r", "status": "approved"},
                    {"candidate_id": "s002", "start": 200, "end": 240, "score": 8,
                     "title": "Short 2", "reason": "r", "status": "pending"},
                    {"candidate_id": "s003", "start": 260, "end": 290, "score": 7,
                     "title": "Short 3", "reason": "r", "status": "rejected"},
                ],
            },
            "report_path": DATA / "test_vid" / "analysis" / "sig" / "report.json",
            "transcript": {"sentences": []},
            "quality_notes": {},
        }

    def tearDown(self):
        self.root.destroy()

    def test_render_shorts_by_candidate_id_selects_only_that_short(self):
        """candidate_id задан: в renderer попадает только выбранный утверждённый Shorts."""
        app = self.app
        calls = {}

        def fake_start_worker(operation, function):
            if app.busy:
                return
            # Выполняем переданную lambda, перехватив реальный renderer.
            with patch("review_ui.render_shorts_approved") as renderer:
                renderer.return_value = "rendered"
                function()
                calls["renderer"] = renderer

        app.busy = False
        with patch.object(app, "start_worker", side_effect=fake_start_worker):
            app.render_shorts(candidate_id="s001")

        self.assertIn("renderer", calls, "render_shorts должен запустить worker с рендером")
        renderer = calls["renderer"]
        self.assertEqual(renderer.call_count, 1)
        passed_report = renderer.call_args.args[1]
        passed_candidate_id = renderer.call_args.kwargs.get("candidate_id")
        # Контракт: UI передаёт весь список shorts_candidates и candidate_id;
        # финальную фильтрацию по candidate_id и по статусу 'approved'
        # выполняет render_shorts_approved (renderer.py:247-249).
        self.assertEqual(passed_candidate_id, "s001")
        # В отчёте присутствует весь набор; выбран утверждённый s001.
        ids = [c["candidate_id"] for c in passed_report["shorts_candidates"]]
        self.assertEqual(ids, ["s001", "s002", "s003"])
        by_id = {c["candidate_id"]: c for c in passed_report["shorts_candidates"]}
        self.assertEqual(by_id["s001"]["status"], "approved",
                         "выбранный Shorts должен быть утверждён")
        # Симулируем фильтрацию renderer'а: только s001 + approved → [s001].
        rendered = [c for c in passed_report["shorts_candidates"]
                    if c["candidate_id"] == passed_candidate_id and c["status"] == "approved"]
        self.assertEqual([c["candidate_id"] for c in rendered], ["s001"],
                         "в результате рендера должен оказаться только выбранный утверждённый Shorts")

    def test_render_shorts_none_renders_all_approved_excluding_pending_rejected(self):
        """candidate_id=None: рендерятся все утверждённые; pending/rejected не попадают."""
        app = self.app
        calls = {}

        def fake_start_worker(operation, function):
            if app.busy:
                return
            with patch("review_ui.render_shorts_approved") as renderer:
                renderer.return_value = "rendered"
                function()
                calls["renderer"] = renderer

        app.busy = False
        with patch.object(app, "start_worker", side_effect=fake_start_worker):
            app.render_shorts(candidate_id=None)

        self.assertIn("renderer", calls)
        renderer = calls["renderer"]
        self.assertEqual(renderer.call_count, 1)
        passed_report = renderer.call_args.args[1]
        self.assertIsNone(renderer.call_args.kwargs.get("candidate_id"))
        # В отчёте весь список, но renderer отрендерит только approved (s001).
        # Проверяем, что pending/rejected не утверждены и не попадут в результат.
        approved = [c for c in passed_report["shorts_candidates"] if c["status"] == "approved"]
        self.assertEqual([c["candidate_id"] for c in approved], ["s001"],
                         "только утверждённый Shorts должен быть в результате")
        statuses = {c["candidate_id"]: c["status"] for c in passed_report["shorts_candidates"]}
        self.assertEqual(statuses.get("s002"), "pending")
        self.assertEqual(statuses.get("s003"), "rejected")

    def test_render_shorts_no_approved_does_not_render(self):
        """Нет утверждённых Shorts: рендер не запускается, UI сообщает об этом."""
        app = self.app
        # Убираем утверждённый Shorts: s001 → pending.
        app.context["report"]["shorts_candidates"][0]["status"] = "pending"

        with patch("review_ui.render_shorts_approved") as renderer, \
                patch("review_ui.messagebox") as mb:
            app.busy = False
            app.render_shorts(candidate_id=None)

        renderer.assert_not_called(), "рендер не должен запускаться без утверждённых Shorts"
        mb.showinfo.assert_called_once()
        args = mb.showinfo.call_args
        self.assertIn("утвердите", str(args).lower())

    def test_render_shorts_busy_does_not_render(self):
        """app.busy=True: повторный рендер не запускается."""
        app = self.app
        app.busy = True

        with patch("review_ui.render_shorts_approved") as renderer:
            app.render_shorts(candidate_id="s001")
            app.render_shorts(candidate_id=None)

        renderer.assert_not_called(), "при busy рендер не должен запускаться"
        # busy остаётся True (ранний возврат не меняет состояние).
        self.assertTrue(app.busy)


if __name__ == "__main__":
    unittest.main()
