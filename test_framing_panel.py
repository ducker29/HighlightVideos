"""Framing UI tests use synthetic images and temporary reports, never video."""
import copy
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from framing_panel import FramingPanel, fit_blur_preview, framing_mode_at
from job_store import load_json, save_json
from review_ui import HighlightApp
from shorts_framing import candidate_framing
from smart_crop_storage import create_settings, load_settings
from smart_crop_window import SmartCropWindow
from test_shorts_framing import framing, short


class PreviewGeometryTests(unittest.TestCase):
    def test_half_open_boundaries_and_gaps(self):
        document = framing((2, 5, "FIT_BLUR"), (8, 10, "FIT_BLUR"))
        for time, expected in ((0, "CROP"), (1.999, "CROP"), (2, "FIT_BLUR"),
                               (4.999, "FIT_BLUR"), (5, "CROP"), (8, "FIT_BLUR"), (10, "CROP")):
            with self.subTest(time=time):
                self.assertEqual(framing_mode_at(document, time), expected)

    def test_full_frame_is_centered_and_proportional(self):
        source = Image.new("RGB", (320, 180), "red")
        # Unique markers at both edges must survive foreground fitting.
        source.paste("lime", (0, 0, 8, 180))
        source.paste("blue", (312, 0, 320, 180))
        result = fit_blur_preview(source, (144, 256))
        self.assertEqual(result.size, (144, 256))
        # Pillow contain: 144 x 81, centered at y=87.
        self.assertGreater(result.getpixel((1, 128))[1], 240)
        self.assertGreater(result.getpixel((142, 128))[2], 240)
        self.assertEqual(result.getpixel((72, 128)), (255, 0, 0))
        from PIL import ImageOps
        foreground = ImageOps.contain(source, (144, 256), Image.Resampling.LANCZOS)
        self.assertEqual(foreground.size, (144, 81))
        self.assertEqual(result.crop((0, 87, 144, 168)).tobytes(), foreground.tobytes())

    def test_background_is_blurred_but_foreground_is_sharp(self):
        arr = np.zeros((90, 160, 3), dtype=np.uint8)
        arr[:, 80:] = 255
        result = fit_blur_preview(Image.fromarray(arr), (144, 256))
        self.assertTrue(0 < result.getpixel((71, 10))[0] < 255)
        self.assertEqual(result.getpixel((10, 128)), (0, 0, 0))
        self.assertEqual(result.getpixel((130, 128)), (255, 255, 255))

    def test_dynamic_frame_background_and_source_unchanged(self):
        red, blue = Image.new("RGB", (160, 90), "red"), Image.new("RGB", (160, 90), "blue")
        original = red.tobytes()
        a, b = fit_blur_preview(red, (90, 160)), fit_blur_preview(blue, (90, 160))
        self.assertEqual(a.getpixel((45, 0)), (255, 0, 0))
        self.assertEqual(b.getpixel((45, 0)), (0, 0, 255))
        self.assertEqual(red.tobytes(), original)


class PanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.change, self.save_callback = Mock(), Mock()
        self.panel = FramingPanel(self.root, None, 30, lambda: 7.25, self.change, self.save_callback)
        self.panel.pack()

    def enter(self, start, end):
        self.panel.start_var.set(start)
        self.panel.end_var.set(end)

    def test_add_edit_delete_and_cursor(self):
        self.panel.use_cursor("start")
        self.assertEqual(self.panel.start_var.get(), "00:00:07.250")
        self.panel.use_cursor("end")
        self.assertEqual(self.panel.end_var.get(), "00:00:07.250")
        self.enter("00:00:02.500", "5,5")
        self.panel.add_interval()
        self.assertEqual(self.panel.draft, framing((2.5, 5.5, "FIT_BLUR")))
        self.assertTrue(self.panel.dirty)
        self.panel.table.selection_set("0")
        self.enter("3", "6")
        self.panel.edit_interval()
        self.assertEqual(self.panel.draft, framing((3, 6, "FIT_BLUR")))
        self.panel.table.selection_set("0")
        self.panel.delete_interval()
        self.assertEqual(self.panel.draft, framing())
        self.assertFalse(self.panel.dirty)
        self.save_callback.assert_not_called()

    def test_invalid_time_bounds_and_overlaps_do_not_modify_draft(self):
        self.enter("2", "5")
        self.panel.add_interval()
        original = copy.deepcopy(self.panel.draft)
        for a, b in (("bad", "8"), ("nan", "8"), ("-1", "2"), ("2", "31"),
                     ("2", "2"), ("3", "8"), ("inf", "8")):
            self.enter(a, b)
            with self.subTest(a=a, b=b), patch("framing_panel.messagebox.showerror") as error:
                self.panel.add_interval()
                error.assert_called_once()
                self.assertEqual(self.panel.draft, original)

    def test_adjacent_merge_and_sort(self):
        for start, end in (("8", "10"), ("2", "5"), ("5", "8")):
            self.enter(start, end)
            self.panel.add_interval()
        self.assertEqual(self.panel.draft, framing((2, 10, "FIT_BLUR")))
        self.assertEqual(len(self.panel.table.get_children()), 1)

    def test_save_and_cancel_are_framing_only(self):
        self.enter("2", "5")
        self.panel.add_interval()
        self.panel.save()
        self.save_callback.assert_called_once_with(framing((2, 5, "FIT_BLUR")))
        self.assertFalse(self.panel.dirty)
        self.enter("10", "12")
        self.panel.add_interval()
        self.panel.cancel()
        self.assertEqual(self.panel.draft, framing((2, 5, "FIT_BLUR")))
        self.assertEqual(self.save_callback.call_count, 1)

    def test_failed_save_keeps_dirty_draft(self):
        self.enter("2", "5")
        self.panel.add_interval()
        self.save_callback.side_effect = ValueError("stale bounds")
        with patch("framing_panel.messagebox.showerror") as error:
            self.panel.save()
        error.assert_called_once()
        self.assertTrue(self.panel.dirty)
        self.assertEqual(self.panel.saved, framing())

    def test_edit_delete_require_selection(self):
        with patch("framing_panel.messagebox.showerror") as error:
            self.panel.edit_interval()
            self.panel.delete_interval()
        self.assertEqual(error.call_count, 2)
        self.assertEqual(self.panel.draft, framing())


class EditorPreviewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.settings = create_settings("sc001", 100, 130, 320, 180)
        self.editor = SmartCropWindow(self.root, "unused.mp4", 100, 130, "sc001", self.settings)
        self.editor.after_cancel(self.editor._worker_after_id)
        self.editor._worker_after_id = None
        self.addCleanup(self.editor.close)
        self.editor.configure_framing(framing((2, 5, "FIT_BLUR")), Mock())
        self.editor.preview_canvas.winfo_width = lambda: 144
        self.editor.preview_canvas.winfo_height = lambda: 256

    def test_switch_uses_cached_frame_without_new_decode(self):
        self.editor._set_time(2)
        self.editor._render(np.full((180, 320, 3), 200, dtype=np.uint8))
        sequence = self.editor._seq
        self.editor._refresh_preview()
        self.assertEqual(self.editor._seq, sequence)
        self.assertEqual((self.editor._preview_image.width(), self.editor._preview_image.height()), (144, 256))
        self.editor._set_time(5)
        self.editor._render(np.full((180, 320, 3), 100, dtype=np.uint8))
        self.assertEqual(self.editor._framing_mode(), "CROP")

    def test_fit_blur_blocks_crop_edits_without_changing_segments(self):
        original = copy.deepcopy(self.editor.settings)
        self.editor._set_time(2)
        self.editor._render(np.zeros((180, 320, 3), dtype=np.uint8))
        event = Mock(x=20, y=20, delta=120)
        self.editor._on_drag_start(event)
        self.editor._on_drag_move(event)
        self.editor._on_zoom(event)
        self.editor.reset_crop()
        self.editor._commit_crop({"x": 0, "y": 0, "width": .2, "height": .8})
        self.assertEqual(self.editor.settings, original)
        self.assertEqual(self.editor.source_canvas.itemcget(self.editor._crop_rect_id, "state"), "hidden")
        self.editor._set_time(5)
        self.assertEqual(self.editor.source_canvas.itemcget(self.editor._crop_rect_id, "state"), "normal")
        self.editor._commit_crop({"x": 0, "y": 0, "width": .2, "height": .8})
        self.assertNotEqual(self.editor.settings, original)

    def test_framing_cancel_leaves_manual_crop_and_segments_intact(self):
        self.editor._commit_crop({"x": .1, "y": 0, "width": .2, "height": .8})
        original = copy.deepcopy(self.editor.settings)
        panel = self.editor.framing_panel
        panel.start_var.set("8")
        panel.end_var.set("10")
        panel.add_interval()
        panel.cancel()
        self.assertEqual(self.editor.settings, original)
        self.assertEqual(panel.draft, framing((2, 5, "FIT_BLUR")))

    def test_timeline_overlay_does_not_split_crop_segments(self):
        self.editor.timeline_canvas.winfo_width = lambda: 300
        original = copy.deepcopy(self.editor.settings)
        self.editor._draw_timeline()
        ids = self.editor.timeline_canvas.find_withtag("fit_blur")
        self.assertEqual(len(ids), 1)
        coords = self.editor.timeline_canvas.coords(ids[0])
        self.assertEqual((coords[0], coords[2]), (20, 50))
        self.assertEqual(self.editor.settings, original)

    def test_preview_uses_displayed_frame_time_during_pending_seek(self):
        self.editor._set_time(2)
        self.editor._on_event(("frame", self.editor._seq, np.zeros((180, 320, 3), dtype=np.uint8), 102.0))
        self.editor._set_time(8)
        with patch("framing_panel.fit_blur_preview", wraps=fit_blur_preview) as compose:
            self.editor._refresh_preview()
        compose.assert_called_once()  # cached frame still belongs to t=2, not requested t=8

    def test_click_framing_strip_seeks_local_time_not_crop_segment_start(self):
        from types import SimpleNamespace
        from smart_crop_window import TIMELINE_H
        self.editor.timeline_canvas.winfo_width = lambda: 300
        self.editor._on_timeline_click(SimpleNamespace(x=30, y=TIMELINE_H - 5))
        self.assertAlmostEqual(self.editor._current_time(), 3.0)
        self.assertEqual(self.editor._framing_mode(), "FIT_BLUR")
        self.assertEqual(len(self.editor.settings["segments"]), 1)

    def test_panel_transport_and_timeline_fit_normal_window(self):
        self.root.geometry("1180x760")
        self.editor.pack(fill="both", expand=True)
        self.root.deiconify()
        self.root.update()
        for widget in (self.editor.framing_panel, self.editor._transport_frame, self.editor.timeline_canvas):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), self.editor.winfo_height())
        self.assertGreater(self.editor.preview_canvas.winfo_height(), 100)


class ReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "analysis" / "sig" / "report.json"
        self.report = {"schema": 1, "signature": "sig", "settings": {},
                       "candidates": [{"candidate_id": "c001", "status": "approved", "approval_key": "hl"}],
                       "shorts_candidates": [short(), short("sc002")]}
        self.report["shorts_candidates"][0].update(title="Short", reason="test", score=80)
        self.report["shorts_candidates"][1].update(title="Other", reason="test", score=70)
        save_json(self.path, self.report)
        self.source = self.directory / "source.mp4"
        self.source.touch()  # existence only; decoder is disabled
        self.app = HighlightApp(self.root)
        self.app.context = {"report": copy.deepcopy(self.report), "report_path": self.path,
                            "job_dir": self.directory, "manifest": {"source_path": str(self.source),
                            "media": {"duration_sec": 300, "width": 320, "height": 180}},
                            "transcript": {"segments": [], "sentences": []}}
        self.app.notebook.select(1)
        self.app.refresh_shorts_table("sc001")
        self.app.show_shorts_candidate()
        self.decoder_patch = patch.object(SmartCropWindow, "_start_worker")
        self.decoder_patch.start()
        self.addCleanup(self.decoder_patch.stop)
        self.app.open_smart_crop()
        self.top = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        self.editor = self.top.smart_crop_editor
        self.addCleanup(self.editor.close)

    def add_interval(self):
        panel = self.editor.framing_panel
        panel.start_var.set("2")
        panel.end_var.set("5")
        panel.add_interval()

    def test_save_updates_only_target_status_and_reopens(self):
        self.add_interval()
        panel = self.editor.framing_panel
        panel.save()
        saved = load_json(self.path)
        self.assertEqual(saved["candidates"], self.report["candidates"])
        self.assertEqual(saved["shorts_candidates"][1], self.report["shorts_candidates"][1])
        self.assertEqual(saved["shorts_candidates"][0]["status"], "pending")
        self.assertEqual(self.app.context["report"], saved)
        self.assertEqual(self.app.shorts_table.item("sc001", "values")[1], "На проверке")
        self.assertEqual(candidate_framing(saved["shorts_candidates"][0]), framing((2, 5, "FIT_BLUR")))
        self.app._close_smart_crop(self.top, self.top.smart_crop_save_path, self.editor)
        self.app.open_smart_crop()
        top = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        editor = top.smart_crop_editor
        self.addCleanup(editor.close)
        self.assertEqual(editor.framing_panel.draft, framing((2, 5, "FIT_BLUR")))

    def test_cancel_does_not_write_report_but_smart_crop_still_saves(self):
        before = self.path.read_bytes()
        self.add_interval()
        self.editor.framing_panel.cancel()
        self.editor._commit_crop({"x": .1, "y": 0, "width": .2, "height": .8})
        expected = copy.deepcopy(self.editor.settings)
        crop_path = self.top.smart_crop_save_path
        self.app._close_smart_crop(self.top, crop_path, self.editor)
        self.assertEqual(self.path.read_bytes(), before)
        saved, _ = load_settings(crop_path)
        self.assertEqual(saved, expected)

    def test_close_discards_framing_only_after_confirmation(self):
        self.add_interval()
        before = self.path.read_bytes()
        with patch("review_ui.messagebox.askyesno", return_value=False):
            self.app._close_smart_crop(self.top, self.top.smart_crop_save_path, self.editor)
        self.assertTrue(self.top.winfo_exists())
        with patch("review_ui.messagebox.askyesno", return_value=True):
            self.app._close_smart_crop(self.top, self.top.smart_crop_save_path, self.editor)
        self.assertEqual(self.path.read_bytes(), before)

    def test_disk_boundary_change_rejects_save_without_writing(self):
        self.add_interval()
        updated = load_json(self.path)
        updated["shorts_candidates"][0].update(start=101, end=131)
        save_json(self.path, updated)
        before = self.path.read_bytes()
        with patch("framing_panel.messagebox.showerror") as error:
            self.editor.framing_panel.save()
        error.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.editor.framing_panel.dirty)

    def test_other_report_or_busy_app_rejects_save(self):
        self.add_interval()
        before = self.path.read_bytes()
        self.app.busy = True
        with patch("framing_panel.messagebox.showerror") as error:
            self.editor.framing_panel.save()
        error.assert_called_once()
        self.app.busy = False
        self.app.context["report_path"] = self.directory / "other.json"
        with patch("framing_panel.messagebox.showerror") as error:
            self.editor.framing_panel.save()
        error.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_saved_anchor_rejects_reopening(self):
        candidate = self.app.context["report"]["shorts_candidates"][0]
        candidate["render_settings"].update(framing=framing((2, 5, "FIT_BLUR")),
            framing_source_bounds={"start": 99, "end": 129})
        self.editor.close()
        self.top.destroy()
        with patch("review_ui.messagebox.showerror") as error:
            self.app.open_smart_crop()
        error.assert_called_once()
        self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))


if __name__ == "__main__":
    unittest.main()
