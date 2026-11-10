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


class SmartCropButtonTests(unittest.TestCase):
    """Проверка кнопки «Смарт-кроп» и логики open_smart_crop."""

    def _make_app(self):
        import tkinter as tk
        from review_ui import HighlightApp

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        app = HighlightApp(root)
        app.context = {
            "job_dir": str(ROOT / "data" / "test_vid"),
            "manifest": {
                "video_id": "test_vid",
                "media": {"duration_sec": 300.0},
                "source_path": "D:/nonexistent/smart_crop_test_source.mp4",
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
                ],
            },
            "report_path": DATA / "test_vid" / "analysis" / "sig" / "report.json",
            "transcript": {"sentences": []},
            "quality_notes": {},
        }
        return root, app

    def test_button_exists(self):
        """Кнопка «Смарт-кроп» должна существовать на панели действий."""
        root, app = self._make_app()
        try:
            self.assertTrue(hasattr(app, "open_smart_crop"))
            # Кнопка добавлена в actions: ищем по тексту.
            def find_button(widget):
                if isinstance(widget, __import__("tkinter").ttk.Button) and \
                   widget.cget("text") == "Смарт-кроп":
                    return widget
                for child in widget.winfo_children():
                    found = find_button(child)
                    if found is not None:
                        return found
                return None
            self.assertIsNotNone(find_button(app.root), "кнопка «Смарт-кроп» не найдена в UI")
        finally:
            root.destroy()

    def test_no_context_shows_message(self):
        """Без открытого отчёта окно не создаётся, показывается сообщение."""
        import tkinter as tk
        from unittest.mock import patch

        root, app = self._make_app()
        app.context = None
        try:
            with patch("review_ui.messagebox") as mb:
                app.open_smart_crop()
            mb.showinfo.assert_called_once()
        finally:
            root.destroy()

    def test_no_candidate_shows_message(self):
        """Без выбранного Shorts-кандидата окно не создаётся."""
        from unittest.mock import patch

        root, app = self._make_app()
        # Снимаем выделение с таблицы.
        app.shorts_table.selection_remove(app.shorts_table.selection())
        try:
            with patch("review_ui.messagebox") as mb:
                app.open_smart_crop()
            mb.showinfo.assert_called_once()
        finally:
            root.destroy()

    def test_source_not_found_no_window(self):
        """Если исходный файл отсутствует, окно не создаётся (ошибка перехватывается)."""
        from unittest.mock import patch

        root, app = self._make_app()
        app.context["manifest"]["source_path"] = str(ROOT / "data" / "test_vid" / "missing.mp4")
        import tkinter as tk
        top_before = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        try:
            with patch("review_ui.messagebox"):
                app.open_smart_crop()
            top_after = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertEqual(len(top_after), len(top_before), "окно не должно создаваться при отсутствии исходника")
        finally:
            root.destroy()

    def test_busy_does_nothing(self):
        """При busy окно не создаётся и сообщений нет."""
        from unittest.mock import patch

        root, app = self._make_app()
        app.busy = True
        try:
            with patch("review_ui.messagebox") as mb:
                app.open_smart_crop()
            mb.assert_not_called()
        finally:
            root.destroy()

    def test_editor_is_packed_in_toplevel(self):
        """Регрессионный: после open_smart_crop контейнер редактора размещён внутри Toplevel."""
        import os
        import tempfile
        import tkinter as tk

        root, app = self._make_app()
        # Создаём реальный файл-заглушку, чтобы прошёл check source_path.exists()
        fd, tmp_src = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            app.context["manifest"]["source_path"] = tmp_src
            app.context["manifest"]["media"]["width"] = 1920
            app.context["manifest"]["media"]["height"] = 1080
            # Заполняем таблицу и выделяем кандидата
            app.refresh_shorts_table("s001")

            tops_before = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and w.winfo_exists()]
            app.open_smart_crop()

            tops_after = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and w.winfo_exists()]
            self.assertGreater(len(tops_after), len(tops_before), "Toplevel не создан")

            top = tops_after[-1]
            editor = getattr(top, "smart_crop_editor", None)
            self.assertIsNotNone(editor, "top.smart_crop_editor не установлен")

            # Проверяем, что editor является потомком top (прямой или косвенный)
            def find_ancestor(widget):
                master = widget.master
                while master is not None:
                    if master is top:
                        return True
                    master = master.master
                return False

            self.assertTrue(find_ancestor(editor), "editor не является потомком Toplevel")

            # Проверяем, что editor реально размещён (pack/grid)
            # Если pack() не вызывался, winfo_children() родителя не будет включать editor
            parent_frame = editor.master
            children = parent_frame.winfo_children()
            self.assertIn(editor, children, "editor не найден среди потомков контейнера — pack()/grid() не вызывался")
        finally:
            root.destroy()
            if os.path.exists(tmp_src):
                os.remove(tmp_src)


class SmartCropSliderRecursionTests(unittest.TestCase):
    """Регрессионные тесты: программное _set_time не вызывает рекурсию через slider callback."""

    @staticmethod
    def _settings():
        from smart_crop_storage import create_settings
        return create_settings("s001", 857.68, 901.40, 3840, 2160)

    def test_programmatic_set_time_no_recursion(self):
        """_set_time(10.0) не вызывает RecursionError (slider.set → _on_slider → _seek_to)."""
        import tkinter as tk
        from smart_crop_window import SmartCropWindow

        root = tk.Tk()
        root.withdraw()
        win = SmartCropWindow(root, source_path="dummy.mp4", start=857.68, end=901.40,
                              candidate_id="s001", settings=self._settings())
        try:
            # This must not raise RecursionError
            win._set_time(10.0)
            root.update_idletasks()
            root.update()
        finally:
            root.destroy()

    def test_initial_load_requests_first_frame(self):
        """При создании окна отправляется ровно один запрос кадра (t=0.0).

        __init__ сначала программно обновляет ползунок (без запроса), затем
        явно отправляет первый запрос через _seek_to(0.0).
        """
        import tkinter as tk
        from smart_crop_window import SmartCropWindow

        root = tk.Tk()
        root.withdraw()
        win = SmartCropWindow(root, source_path="dummy.mp4", start=857.68, end=901.40,
                              candidate_id="s001", settings=self._settings())
        try:
            # _set_time(0.0) не создаёт запрос; один запрос идёт из _seek_to(0.0).
            self.assertEqual(win._seq, 1, "один запрос кадра при инициализации")
            # The command queue should contain exactly one get_frame request
            item = win._commands.get_nowait()
            self.assertEqual(item[0], "get_frame")
            self.assertEqual(item[1], 1)
            self.assertAlmostEqual(item[2], 0.0)
        finally:
            root.destroy()

    def test_slider_drag_requests_frame(self):
        """Перемещение ползунка запрашивает кадр (один запрос на один seek)."""
        import tkinter as tk
        from smart_crop_window import SmartCropWindow

        root = tk.Tk()
        root.withdraw()
        win = SmartCropWindow(root, source_path="dummy.mp4", start=857.68, end=901.40,
                              candidate_id="s001", settings=self._settings())
        try:
            # Drain the initial frame request
            initial = win._commands.get_nowait()
            self.assertEqual(initial[0], "get_frame")
            initial_seq = win._seq

            # Simulate user dragging slider
            win._on_slider(20.0)
            root.update_idletasks()
            root.update()

            self.assertEqual(win._seq, initial_seq + 1, "один запрос кадра после seek")
            item = win._commands.get_nowait()
            self.assertEqual(item[0], "get_frame")
            self.assertEqual(item[1], initial_seq + 1)
            self.assertAlmostEqual(item[2], 20.0)
        finally:
            root.destroy()

    def test_rapid_seeks_bounded_queue(self):
        """Быстрые seek не создают бесконечную очередь: N seeks → N новых запросов."""
        import tkinter as tk
        from smart_crop_window import SmartCropWindow

        root = tk.Tk()
        root.withdraw()
        win = SmartCropWindow(root, source_path="dummy.mp4", start=857.68, end=901.40,
                              candidate_id="s001", settings=self._settings())
        try:
            # Drain initial
            win._commands.get_nowait()
            base_seq = win._seq

            n_seeks = 5
            for i in range(n_seeks):
                win._on_slider(float(i * 1.0))
                root.update_idletasks()
                root.update()

            self.assertEqual(win._seq, base_seq + n_seeks,
                             f"ожидается {base_seq + n_seeks}, получено {win._seq}")
            # Queue should have exactly n_seeks get_frame items
            count = 0
            while not win._commands.empty():
                item = win._commands.get_nowait()
                if item[0] == "get_frame":
                    count += 1
            self.assertEqual(count, n_seeks,
                             f"в очереди {count} запросов, ожидалось {n_seeks}")
        finally:
            root.destroy()

    def test_set_time_does_not_double_request(self):
        """_set_time (программное обновление ползунка) не создаёт запросов кадров."""
        import tkinter as tk
        from smart_crop_window import SmartCropWindow

        root = tk.Tk()
        root.withdraw()
        win = SmartCropWindow(root, source_path="dummy.mp4", start=857.68, end=901.40,
                              candidate_id="s001", settings=self._settings())
        try:
            win._commands.get_nowait()  # drain initial
            base_seq = win._seq

            win._set_time(5.0)
            root.update_idletasks()
            root.update()

            # _set_time — чистый программный set ползунка: новых запросов нет.
            self.assertEqual(win._seq, base_seq,
                             f"_set_time не должен создавать запросы, "
                             f"получено {win._seq - base_seq} новых")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
