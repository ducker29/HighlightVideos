"""Unit tests for smart_crop_window.

Non-GUI tests run headless (geometry, segment lookup, decoder lifecycle).
Tkinter GUI tests require a display and are skipped when none is available.
"""
import queue
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import smart_crop_window as w
from smart_crop_storage import create_settings


# --------------------------------------------------------------------------- #
# crop_pixels
# --------------------------------------------------------------------------- #
class TestCropPixels(unittest.TestCase):
    def test_left_third_full_height_16to9(self):
        self.assertEqual(
            w.crop_pixels({"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}, 1920, 1080),
            (0, 0, 960, 1080))

    def test_centered_crop(self):
        # 0.5625*1920 = 1080 -> x=(1920-1080)/2=420
        self.assertEqual(
            w.crop_pixels({"x": 0.21875, "y": 0.0, "width": 0.5625, "height": 1.0}, 1920, 1080),
            (420, 0, 1080, 1080))

    def test_clamps_negative_and_overflow(self):
        x, y, cw, ch = w.crop_pixels({"x": -0.5, "y": -0.5, "width": 4.0, "height": 4.0}, 100, 100)
        self.assertEqual((x, y), (0, 0))
        self.assertLessEqual(x + cw, 100)
        self.assertLessEqual(y + ch, 100)

    def test_min_one_pixel(self):
        x, y, cw, ch = w.crop_pixels({"x": 0.99, "y": 0.99, "width": 0.001, "height": 0.001}, 100, 100)
        self.assertGreaterEqual(cw, 1)
        self.assertGreaterEqual(ch, 1)
        self.assertLessEqual(x + cw, 100)
        self.assertLessEqual(y + ch, 100)

    def test_integer_rounding(self):
        x, y, cw, ch = w.crop_pixels({"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.8}, 640, 360)
        self.assertEqual((x, y, cw, ch), (64, 36, 256, 288))


# --------------------------------------------------------------------------- #
# fit_size
# --------------------------------------------------------------------------- #
class TestFitSize(unittest.TestCase):
    def test_downscale_preserves_aspect(self):
        self.assertEqual(w.fit_size(1920, 1080, 640, 360), (640, 360))

    def test_fit_into_portrait_box(self):
        out_w, out_h = w.fit_size(1080, 1080, 300, 533)
        self.assertLessEqual(out_w, 300)
        self.assertLessEqual(out_h, 533)
        self.assertGreaterEqual(out_w, 1)
        self.assertGreaterEqual(out_h, 1)

    def test_centered_geometry_16_9(self):
        """A 16:9 frame into the 16:9 source canvas fills it exactly (centered, no letterbox)."""
        cx, cy, dw, dh, scale = w.display_geometry(1920, 1080, w.SOURCE_W, w.SOURCE_H)
        self.assertEqual((cx, cy), (0.0, 0.0))
        self.assertEqual((dw, dh), (w.SOURCE_W, w.SOURCE_H))

    def test_centered_geometry_widescreen_letterboxed(self):
        """A wider-than-16:9 frame is shrunk to the canvas width and centered vertically."""
        cx, cy, dw, dh, scale = w.display_geometry(2560, 1080, w.SOURCE_W, w.SOURCE_H)
        self.assertAlmostEqual(dw / dh, 2560 / 1080, places=3)
        self.assertLess(dh, w.SOURCE_H)
        self.assertAlmostEqual(cx, 0.0, places=3)
        self.assertAlmostEqual(cy, (w.SOURCE_H - dh) / 2, places=3)

    def test_centered_geometry_portrait_letterboxed_h(self):
        """A taller-than-16:9 frame is shrunk to the canvas height and centered horizontally."""
        cx, cy, dw, dh, scale = w.display_geometry(1080, 1920, w.SOURCE_W, w.SOURCE_H)
        self.assertAlmostEqual(dh, w.SOURCE_H, places=3)
        self.assertLess(dw, w.SOURCE_W)
        self.assertAlmostEqual(cy, 0.0, places=3)
        self.assertAlmostEqual(cx, (w.SOURCE_W - dw) / 2, places=3)

    def test_never_zero_even_when_tiny(self):
        # fit_size scales to fill the box; the minimum-size guarantee lives in
        # crop_pixels, but fit_size must still never return 0.
        out = w.fit_size(1, 1, 100, 100)
        self.assertTrue(all(v >= 1 for v in out))


# --------------------------------------------------------------------------- #
# portrait_fit (9:16 preview frame)
# --------------------------------------------------------------------------- #
class TestPortraitFit(unittest.TestCase):
    def test_exact_916_ratio(self):
        for target_h in (400, 800, 1600):
            pw, ph = w.portrait_fit(600, 1000, target_h)
            self.assertAlmostEqual(pw / ph, 9 / 16, places=3)

    def test_follows_target_height(self):
        pw, ph = w.portrait_fit(600, 1000, 400)
        self.assertEqual(ph, 400)
        self.assertEqual(pw, 225)

    def test_never_zero(self):
        pw, ph = w.portrait_fit(100, 100, 1)
        self.assertGreaterEqual(pw, 1)
        self.assertGreaterEqual(ph, 1)


# --------------------------------------------------------------------------- #
# segment_for
# --------------------------------------------------------------------------- #
class TestSegmentFor(unittest.TestCase):
    def test_returns_segment_at_time(self):
        s = create_settings("c1", 100.0, 130.0, 1920, 1080)
        seg = w.segment_for(s, 0.0)
        self.assertIsNotNone(seg)
        self.assertAlmostEqual(seg["crop"]["width"], (9 / 16.0) * (1080 / 1920), places=6)

    def test_at_end_boundary(self):
        s = create_settings("c1", 0.0, 30.0, 1920, 1080)
        self.assertIsNotNone(w.segment_for(s, 29.999))

    def test_portrait_source_defaults_to_full_frame(self):
        s = create_settings("c1", 0.0, 30.0, 720, 1280)
        self.assertEqual(s["segments"][0]["crop"],
                         {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0})

    def test_narrow_portrait_source_keeps_916(self):
        s = create_settings("c1", 0.0, 30.0, 480, 1280)
        c = s["segments"][0]["crop"]
        self.assertAlmostEqual((c["width"] * 480) / (c["height"] * 1280),
                               9 / 16, places=4)
        self.assertAlmostEqual(c["y"], (1 - c["height"]) / 2, places=5)


# --------------------------------------------------------------------------- #
# clamp_crop / centered_crop_916 / clamp_drag
# --------------------------------------------------------------------------- #
class TestClampCrop(unittest.TestCase):
    def test_stays_inside_and_keeps_aspect(self):
        # 16:9 source: the 9:16 portrait is checked in frame PIXELS, not in the
        # normalized width:height ratio (which equals (fh/fw) * 9/16).
        c = w.clamp_crop(0.9, 0.9, 0.5, 0.5, fw=1920, fh=1080)
        self.assertLessEqual(c["x"] + c["width"], 1.0 + 1e-9)
        self.assertLessEqual(c["y"] + c["height"], 1.0 + 1e-9)
        self.assertAlmostEqual((c["width"] * 1920) / (c["height"] * 1080), 9 / 16, places=5)

    def test_negative_input_clamped_to_origin(self):
        c = w.clamp_crop(-0.5, -0.5, 0.3, 0.3, fw=1920, fh=1080)
        self.assertEqual(c["x"], 0.0)
        self.assertEqual(c["y"], 0.0)

    def test_oversized_clamped_to_full_short_side(self):
        c = w.clamp_crop(-0.5, -0.5, 4.0, 4.0, fw=1920, fh=1080)
        # For a 16:9 source the clamped 9:16 crop fills the full height; its
        # normalized width is 9/16 * (fh/fw), not 9/16 of the normalized height.
        self.assertAlmostEqual(c["width"], (9 / 16.0) * (1080 / 1920), places=6)
        self.assertEqual(c["height"], 1.0)
        # Oversized + negative origin clamps to the top-left corner, not re-centered.
        self.assertEqual(c["x"], 0.0)
        self.assertEqual(c["y"], 0.0)

    def test_never_smaller_than_minimum(self):
        c = w.clamp_crop(0, 0, 0.0001, 0.0001, fw=1920, fh=1080)
        self.assertGreaterEqual(c["width"], w._MIN_CROP - 1e-9)
        # height = width * (16/9) * (fw/fh) for a 16:9 source
        self.assertGreaterEqual(c["height"], w._MIN_CROP * (16 / 9.0) * (1920 / 1080) - 1e-9)
        self.assertGreater(c["width"], 0)
        self.assertGreater(c["height"], 0)


class TestCenteredCrop916(unittest.TestCase):
    def test_landscape_is_centered_full_height(self):
        c = w.centered_crop_916(1920, 1080)
        # 9:16 in frame pixels: width = 9/16 * (fh/fw) of the normalized frame.
        self.assertAlmostEqual(c["width"], (9 / 16.0) * (1080 / 1920), places=6)
        self.assertEqual(c["height"], 1.0)
        self.assertEqual(c["y"], 0.0)
        self.assertAlmostEqual(c["x"], (1 - c["width"]) / 2, places=5)

    def test_portrait_covers_full_width(self):
        c = w.centered_crop_916(720, 1280)
        # Portrait source: 9:16 in pixels -> full width, height = 16/9 * (fw/fh).
        self.assertEqual(c["width"], 1.0)
        self.assertAlmostEqual(c["height"], (16 / 9.0) * (720 / 1280), places=6)
        self.assertEqual(c["x"], 0.0)

    def test_default_landscape_16_9_width_is_0_31640625(self):
        """For a 16:9 source, the 9:16 crop width is 9/16 * (9/16) = 0.31640625."""
        c = w.centered_crop_916(16, 9)
        self.assertAlmostEqual(c["width"], 9 / 16.0 * 9 / 16.0, places=9)
        self.assertAlmostEqual(c["width"], 0.31640625, places=9)
        self.assertEqual(c["height"], 1.0)


class TestClampDrag(unittest.TestCase):
    def test_clamps_left_edge(self):
        self.assertEqual(w.clamp_drag(-10, 5, 200, 355, 640, 360), (0.0, 5, 200, 355))

    def test_clamps_right_and_bottom(self):
        self.assertEqual(w.clamp_drag(630, 350, 200, 355, 640, 360), (440, 5, 200, 355))

    def test_no_change_when_inside(self):
        self.assertEqual(w.clamp_drag(100, 50, 200, 200, 640, 360), (100, 50, 200, 200))


# --------------------------------------------------------------------------- #
# local <-> absolute time conversion (per candidate)
# --------------------------------------------------------------------------- #
class TestTimeConversion(unittest.TestCase):
    """local_time ∈ [0, end-start]; absolute = start + local. No double offset."""

    def test_sc001_local_to_absolute(self):
        start, end = 857.68, 906.2
        duration = end - start
        self.assertEqual(w.local_to_absolute(0.0, start), start)
        self.assertEqual(w.local_to_absolute(duration, start), end)
        self.assertAlmostEqual(w.local_to_absolute(duration / 2, start),
                               (start + end) / 2, places=6)

    def test_second_candidate_own_timing(self):
        start, end = 1200.0, 1230.0
        self.assertEqual(w.local_to_absolute(0.0, start), 1200.0)
        self.assertEqual(w.local_to_absolute(30.0, start), 1230.0)
        self.assertEqual(w.absolute_to_local(1215.0, start), 15.0)

    def test_roundtrip(self):
        for start in (0.0, 857.68, 1200.0):
            for local in (0.0, 12.5, 47.72):
                self.assertAlmostEqual(
                    w.absolute_to_local(w.local_to_absolute(local, start), start),
                    local, places=9)

    def test_no_double_offset(self):
        """applying the start offset exactly once — local 0 maps to start, not 2*start."""
        start = 857.68
        self.assertNotEqual(w.local_to_absolute(0.0, start), 2 * start)
        self.assertEqual(w.local_to_absolute(0.0, start), start)

    def test_box_clamped_to_letterboxed_image(self):
        """A crop box is clamped to the centered (letterboxed) image, not the canvas."""
        # 21:9 frame letterboxed into the 16:9 canvas: cy > 0.
        cx, cy, dw, dh, _ = w.display_geometry(3840, 1640, w.SOURCE_W, w.SOURCE_H)
        self.assertGreater(cy, 0)
        x, y, bw, bh = w.clamp_box_in_area(w.SOURCE_W - 50, w.SOURCE_H - 50, 100, 100,
                                           (cx, cy, dw, dh, 1.0))
        self.assertLessEqual(x + bw, cx + dw + 1e-9)
        self.assertLessEqual(y + bh, cy + dh + 1e-9)
        # A box dragged above the image is pushed down to the image top.
        x2, y2, _, _ = w.clamp_box_in_area(10, -50, 100, 100, (cx, cy, dw, dh, 1.0))
        self.assertEqual(y2, cy)


# --------------------------------------------------------------------------- #
# Playback regression (no Tk display required)
# --------------------------------------------------------------------------- #
class TestPlaybackUpdates(unittest.TestCase):
    def _window_stub(self):
        from unittest.mock import Mock
        win = object.__new__(w.SmartCropWindow)
        win.duration = 10.0
        win._playing = True
        win._closed = False
        win._setting_slider = False
        win._programmatic_slider_value = None
        win.slider = Mock()
        win.slider.get.return_value = 1.0
        win.play_btn = Mock()
        win._seek_to = Mock()
        return win

    def test_play_advances_slider_and_requests_frame_and_timecode(self):
        win = self._window_stub()
        win._advance_playback()
        expected = 1.0 + w.POLL_MS / 1000.0
        win.slider.set.assert_called_once_with(expected)
        win._seek_to.assert_called_once_with(expected)
        self.assertTrue(win._playing)

    def test_programmatic_slider_callback_does_not_pause(self):
        win = self._window_stub()
        win._set_time(2.0)
        win._on_slider("2.0")
        self.assertTrue(win._playing)
        win._seek_to.assert_called_once_with(2.0)

    def test_user_slider_callback_still_seeks_and_pauses(self):
        win = self._window_stub()
        win._set_time(2.0)
        win._on_slider("3.0")
        self.assertFalse(win._playing)
        win._seek_to.assert_called_with(3.0)

    def test_step_frame_requests_new_frame(self):
        win = self._window_stub()
        win.fps = 25.0
        win.step_frame(1)
        self.assertFalse(win._playing)
        win._seek_to.assert_called_once_with(1.04)


# --------------------------------------------------------------------------- #
# FrameDecoder
# --------------------------------------------------------------------------- #
def _make_video(path, w=1280, h=720, fps=25, duration=2):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "28", str(path)],
        check=True)


@unittest.skipUnless(shutil.which("ffmpeg") and w.av is not None, "ffmpeg and PyAV required")
class TestFrameDecoder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "src.mp4"
        _make_video(self.src)
        self.decoder = w.FrameDecoder(self.src)

    def tearDown(self):
        self.decoder.close()
        self.tmp.cleanup()

    def test_open_reports_geometry_and_fps(self):
        self.assertTrue(self.decoder.open())
        self.assertEqual((self.decoder.width, self.decoder.height), (1280, 720))
        self.assertEqual(self.decoder.fps, 25.0)

    def test_request_frame_returns_rgb(self):
        self.decoder.open()
        arr = self.decoder.request_frame(0.5)
        self.assertIsNotNone(arr)
        self.assertEqual(arr.shape, (720, 1280, 3))
        self.assertEqual(arr.dtype, "uint8")

    def test_seek_to_different_times(self):
        self.decoder.open()
        a = self.decoder.request_frame(0.2)
        b = self.decoder.request_frame(1.8)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

    def test_close_is_idempotent_and_stops_requests(self):
        self.decoder.open()
        self.decoder.close()
        self.assertTrue(self.decoder.is_closed())
        self.assertIsNone(self.decoder.request_frame(0.5))
        self.decoder.close()  # second close must not raise


# --------------------------------------------------------------------------- #
# Tkinter GUI tests (require a display)
# --------------------------------------------------------------------------- #
def _display_available():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(_display_available(), "Tkinter display required")
class TestSmartCropWindowGUI(unittest.TestCase):
    def _build(self):
        import tkinter as tk
        from smart_crop_storage import create_settings
        tmp = tempfile.TemporaryDirectory()
        src = Path(tmp.name) / "src.mp4"
        _make_video(src)
        self.addCleanup(tmp.cleanup)
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        settings = create_settings("c1", 0.0, 2.0, 1280, 720)
        win = w.SmartCropWindow(root, src, 0.0, 2.0, "c1", settings)
        return root, win

    def test_window_constructs_and_closes(self):
        root, win = self._build()
        self.assertTrue(win.winfo_exists())
        # let the worker start and decode at least once
        for _ in range(30):
            root.update()
            import time
            time.sleep(0.05)
        win.close()
        self.assertTrue(win._closed)

    def _setup_display(self, win):
        """Give the window a fake display/frame so crop math can be exercised."""
        import numpy as np
        win._frame_cached = np.zeros((720, 1280, 3), dtype="uint8")
        win._frame_size = (1280, 720)
        win._source_display = win._display_geometry(1280, 720)
        win._apply_crop_from_settings()

    def test_crop_box_matches_default_segment(self):
        root, win = self._build()
        try:
            win.source_canvas.winfo_width = lambda: 640
            win.source_canvas.winfo_height = lambda: 360
            self._setup_display(win)
            win._redraw_source()
            x, y, bw, bh = win._crop_box_canvas()
            cx, cy, dw, dh, scale = win._source_display
            seg = win._current_segment()
            # Box maps the segment's normalized crop into the letterboxed display.
            self.assertAlmostEqual(x, cx + seg["crop"]["x"] * dw)
            self.assertAlmostEqual(bw, seg["crop"]["width"] * dw)
            self.assertAlmostEqual(bh, seg["crop"]["height"] * dh)
            self.assertGreater(bw, 0)
            self.assertGreater(bh, 0)
        finally:
            win.close()

    # -- regression: actual canvas size drives the drawing ---------------- #

    def test_image_centered_in_real_canvas_size(self):
        """The source image is centered in the canvas's ACTUAL (not constant) size.

        Regression for the bug where the frame was drawn with the fixed
        640x360 constants while the canvas is larger (e.g. 900x450), leaving the
        frame stuck in the top-left of a black area.
        """
        root, win = self._build()
        try:
            import numpy as np
            win._frame_cached = np.zeros((720, 1280, 3), dtype="uint8")
            win._frame_size = (1280, 720)
            # Real canvas size (window 1180x760 -> source canvas ~900x450).
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            cw, ch = 900, 450
            geo = win._display_geometry(1280, 720)
            # 1280x720 (16:9) in a 900x450 canvas:
            # scale = min(900/1280, 450/720) = min(0.703125, 0.625) = 0.625
            # dw = 1280*0.625 = 800, dh = 720*0.625 = 450
            # cx = (900-800)/2 = 50, cy = (450-450)/2 = 0
            self.assertEqual(geo[:2], (50.0, 0.0),
                             "16:9 frame in a wider-than-16:9 canvas: centered horizontally")
            self.assertEqual(geo[2:4], (800, 450),
                             "display size must be the letterboxed image, not the full canvas")
            win._source_display = geo
            import cv2
            from PIL import Image, ImageTk
            img = win._make_source_image(np.zeros((720, 1280, 3), dtype="uint8"),
                                         cv2, Image, ImageTk)
            win._source_image = img
            win._redraw_source()
            # create_image anchor=center at (cx+dw/2, cy+dh/2) => centered on the canvas.
            self.assertEqual(win._source_image.width(), 800)
            self.assertEqual(win._source_image.height(), 450)
        finally:
            win.close()

    def test_source_image_size_tracks_canvas(self):
        """_make_source_image sizes the PhotoImage to the letterboxed image, aspect kept."""
        root, win = self._build()
        try:
            import numpy as np
            import cv2
            from PIL import Image, ImageTk
            # Simulate a larger-than-640x360 canvas via winfo mock.
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            img = win._make_source_image(np.zeros((720, 1280, 3), dtype="uint8"),
                                         cv2, Image, ImageTk)
            # 1280x720 -> scale to fit 900x450: height-limited (0.625) -> 800x450
            self.assertEqual(img.width(), 800)
            self.assertEqual(img.height(), 450)
            self.assertAlmostEqual(img.width() / img.height(), 16 / 9, places=2)
        finally:
            win.close()

    def test_display_geometry_tracks_canvas(self):
        """_display_geometry must use the real canvas size, not the constants."""
        root, win = self._build()
        try:
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            cx, cy, dw, dh, scale = win._display_geometry(1280, 720)
            # 16:9 frame in 900x450 canvas: letterboxed to 800x450, offset x=50
            self.assertEqual((dw, dh), (800, 450))
            self.assertEqual((cx, cy), (50.0, 0.0))
        finally:
            win.close()

    def test_configure_recenters_after_resize(self):
        """After a canvas resize, the crop rect follows the centered image."""
        root, win = self._build()
        try:
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            self._setup_display(win)
            win._on_canvas_configure()
            x, y, bw, bh = win._crop_box_canvas()
            cx, cy, dw, dh, _ = win._source_display
            seg = win._current_segment()
            self.assertAlmostEqual(bw, seg["crop"]["width"] * dw)
            self.assertAlmostEqual(x, cx + seg["crop"]["x"] * dw)
            # 16:9 frame in a wider-than-16:9 canvas (900x450): letterboxed to 800x450.
            self.assertEqual(dw, 800)
            self.assertEqual(dh, 450)
            self.assertEqual(cx, 50.0)
        finally:
            win.close()

    def test_preview_frame_is_916_not_stretched(self):
        """The 9:16 preview frame has a true 9:16 aspect, independent of the crop
        region aspect, and is centered in the preview canvas (no stretch, no
        overflow). Regression for the bug where the crop was resized into the
        300x533 rectangle, visibly distorting the portrait frame.
        """
        root, win = self._build()
        try:
            import numpy as np
            win._frame_cached = np.zeros((720, 1280, 3), dtype="uint8")
            win._frame_size = (1280, 720)
            win._source_display = win._display_geometry(1280, 720)
            # Force the preview canvas to a concrete size.
            win.preview_canvas.winfo_width = lambda: 300
            win.preview_canvas.winfo_height = lambda: 533
            win._refresh_preview()
            self.assertIsNotNone(win._preview_image)
            pw, ph = win._preview_image.width(), win._preview_image.height()
            self.assertAlmostEqual(pw / ph, 9 / 16, places=2,
                                    msg="preview frame must be 9:16 portrait")
            self.assertLessEqual(pw, 300)
            self.assertLessEqual(ph, 533)
            self.assertGreater(ph, 0)
        finally:
            win.close()

    def test_preview_follows_real_preview_canvas(self):
        """Preview frame fits the ACTUAL preview canvas size (aspect kept)."""
        root, win = self._build()
        try:
            import numpy as np
            win._frame_cached = np.zeros((720, 1280, 3), dtype="uint8")
            win._frame_size = (1280, 720)
            win._source_display = win._display_geometry(1280, 720)
            win.preview_canvas.winfo_width = lambda: 200
            win.preview_canvas.winfo_height = lambda: 400
            win._refresh_preview()
            pw, ph = win._preview_image.width(), win._preview_image.height()
            self.assertLessEqual(pw, 200)
            self.assertLessEqual(ph, 400)
            self.assertAlmostEqual(pw / ph, 9 / 16, places=2)
        finally:
            win.close()

    def test_drag_moves_crop_and_sets_manual_override(self):
        root, win = self._build()
        try:
            self._setup_display(win)
            x, y, bw, bh = win._crop_box_canvas()
            cx, cy, dw, dh, scale = win._source_display
            win._drag = (0.01, 0.01)  # nonzero offset marks an active drag
            win._on_drag_move(type("E", (), {"x": cx + dw * 0.5, "y": cy + dh * 0.5})())
            win._on_drag_end(None)
            seg = win._current_segment()
            self.assertTrue(seg["manual_override"])
            self.assertGreater(seg["crop"]["x"], 0.341796 - 1e-5)
        finally:
            win.close()

    def test_reset_crop_restores_centered(self):
        root, win = self._build()
        try:
            self._setup_display(win)
            win.settings["segments"][0]["manual_override"] = True
            win.settings["segments"][0]["crop"]["x"] = 0.0
            win.reset_crop()
            seg = win._current_segment()
            self.assertTrue(seg["manual_override"])
            # 1280x720 source: normalized crop width = 9/16 * (720/1280).
            fw, fh = win._frame_size
            self.assertAlmostEqual(seg["crop"]["width"], (9 / 16.0) * (fh / fw), places=5)
            self.assertEqual(seg["crop"]["height"], 1.0)
            self.assertAlmostEqual(seg["crop"]["x"], (1 - seg["crop"]["width"]) / 2, places=5)
        finally:
            win.close()

    def test_commit_crop_persists_normalized_box(self):
        root, win = self._build()
        try:
            self._setup_display(win)
            cx, cy, dw, dh, scale = win._source_display
            # A box covering the full frame in canvas px maps to ~full width.
            win._commit_crop(w.clamp_crop(0.1, 0.0, 0.2, 0.8889, fw=1280, fh=720))
            seg = win._current_segment()
            self.assertAlmostEqual(seg["crop"]["width"], 0.2, places=6)
            self.assertTrue(seg["manual_override"])
        finally:
            win.close()

    # -- 9:16 geometry of the crop rect & its preview ---------------------- #

    def test_crop_box_is_916_aspect_in_canvas(self):
        """The green crop rect has an exact 9:16 width:height ratio in canvas px,
        so the 16:9 source shows a true portrait region (not stretched/squished).
        """
        root, win = self._build()
        try:
            # Simulate a real, placed canvas (window-sized, not 1x1).
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            self._setup_display(win)
            x, y, bw, bh = win._crop_box_canvas()
            self.assertGreater(bw, 0)
            self.assertGreater(bh, 0)
            self.assertAlmostEqual(bw / bh, 9 / 16, places=3,
                                   msg="crop rect must be 9:16 in canvas px")
        finally:
            win.close()

    def test_crop_box_centered_on_frame_not_canvas(self):
        """The default crop is centered on the video frame, not the canvas.

        For a 16:9 source the letterboxed frame is horizontally centered in the
        canvas (cx > 0 when the canvas is wider than 16:9); the crop box must be
        centered on that frame: x = cx + (dw - bw) / 2.
        """
        root, win = self._build()
        try:
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            self._setup_display(win)
            x, y, bw, bh = win._crop_box_canvas()
            cx, cy, dw, dh, _ = win._source_display
            self.assertAlmostEqual(x, cx + (dw - bw) / 2, places=3,
                                   msg="crop must be centered on the frame")
            self.assertAlmostEqual(y, cy + (dh - bh) / 2, places=3)
        finally:
            win.close()

    def test_default_crop_full_height_width_is_9_16_of_height(self):
        """For a 16:9 source, the default crop spans the full height and its
        width in frame pixels is exactly 9/16 of the frame height.
        """
        root, win = self._build()
        try:
            self._setup_display(win)
            seg = win._current_segment()
            c = seg["crop"]
            fw, fh = win._frame_size
            self.assertEqual(c["height"], 1.0)
            # width * fw / (height * fh) == 9/16  (pixels, not normalized ratio)
            self.assertAlmostEqual((c["width"] * fw) / (c["height"] * fh), 9 / 16, places=6)
        finally:
            win.close()

    def test_preview_matches_green_frame_content(self):
        """The preview shows exactly the pixels inside the green crop rect.

        A solid crop color is placed only inside the crop region of the cached
        frame; the preview (which crops the frame) must come out that color.
        """
        root, win = self._build()
        try:
            import numpy as np
            win._frame_cached = np.zeros((720, 1280, 3), dtype="uint8")
            win._frame_size = (1280, 720)
            win._source_display = win._display_geometry(1280, 720)
            seg = win._current_segment()
            x, y, cw, ch = w.crop_pixels(seg["crop"], 1280, 720)
            win._frame_cached[y:y + ch, x:x + cw] = 200  # mark crop region
            win.preview_canvas.winfo_width = lambda: 300
            win.preview_canvas.winfo_height = lambda: 533
            win._refresh_preview()
            from PIL import ImageTk
            preview = ImageTk.getimage(win._preview_image).convert("RGB")
            pixels = np.asarray(preview)
            self.assertGreaterEqual(int(pixels.min()), 190,
                                    "preview must not include pixels outside the crop")
            self.assertLessEqual(int(pixels.max()), 210)
        finally:
            win.close()

    def test_saved_manual_crop_not_reset_on_open(self):
        """A user crop saved in the settings is respected at open (not reset)."""
        root, win = self._build()
        try:
            # A 9:16 crop shifted left: width = 9/16 * (fh/fw) for 1280x720.
            saved = w.clamp_crop(0.0, 0.0, (9 / 16.0) * (720 / 1280), 1.0,
                                 fw=1280, fh=720)
            win.settings["segments"][0]["crop"] = dict(saved)
            win.settings["segments"][0]["manual_override"] = True
            win.source_canvas.winfo_width = lambda: 900
            win.source_canvas.winfo_height = lambda: 450
            self._setup_display(win)
            x, y, bw, bh = win._crop_box_canvas()
            # Canvas box uses the same aspect as the displayed source pixels.
            self.assertAlmostEqual(bw / bh, 9 / 16, places=2,
                                    msg="saved crop keeps its 9:16 aspect in frame pixels")
            # The box is NOT re-centered: it follows the saved x=0 (left edge),
            # so it sits at the left, not in the middle.
            cx, _, _, _, _ = win._source_display
            self.assertAlmostEqual(x, cx, places=3,
                                   msg="saved crop x=0 must remain at the frame's left edge")
        finally:
            win.close()


# --------------------------------------------------------------------------- #
# Queue separation regression (GUI -> worker vs worker -> GUI)
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_display_available(), "Tkinter display required")
class TestQueueSeparation(unittest.TestCase):
    """Verify that GUI and worker do not share the same message channel.

    The original bug: _poll() (GUI) and _worker_loop() both called
    self._events.get(), so the GUI consumed 'get_frame' messages meant for
    the worker, and the worker consumed 'ready'/'frame' messages meant for
    the GUI.  The fix introduces a dedicated _commands queue (GUI -> worker)
    and keeps _events for worker -> GUI traffic.
    """

    def test_commands_and_events_are_distinct_queues(self):
        """_commands and _events must be separate Queue instances."""
        root = _make_hidden_root()
        self.addCleanup(root.destroy)
        settings = create_settings("c1", 0.0, 2.0, 1280, 720)
        win = w.SmartCropWindow(root, "nonexistent.mp4", 0.0, 2.0, "c1", settings)
        self.addCleanup(win.close)
        self.assertIsNot(win._commands, win._events,
                         "_commands and _events must be different Queue objects")

    def test_request_frame_goes_to_commands_not_events(self):
        """_request_frame must enqueue on _commands, leaving _events empty."""
        root = _make_hidden_root()
        self.addCleanup(root.destroy)
        settings = create_settings("c1", 0.0, 2.0, 1280, 720)
        win = w.SmartCropWindow(root, "nonexistent.mp4", 0.0, 2.0, "c1", settings)
        self.addCleanup(win.close)
        # Initial seek already queued one get_frame in __init__.
        self.assertGreaterEqual(win._commands.qsize(), 1)
        self.assertEqual(win._events.qsize(), 0,
                         "_events must stay empty before the worker starts")
        # Simulate another frame request.
        win._request_frame(0.5)
        self.assertGreaterEqual(win._commands.qsize(), 2)
        self.assertEqual(win._events.qsize(), 0,
                         "_events must remain empty after _request_frame")
        # Drain the queue; the last message must be the one we just sent.
        last = None
        while True:
            try:
                last = win._commands.get_nowait()
            except queue.Empty:
                break
        self.assertEqual(last[0], "get_frame")
        self.assertIsInstance(last[1], int)
        self.assertEqual(last[2], 0.5)

    def test_worker_reads_from_commands_not_events(self):
        """The worker loop source code must read from _commands."""
        import inspect
        src = inspect.getsource(w.SmartCropWindow._worker_loop)
        self.assertIn("self._commands.get()", src,
                      "_worker_loop must read commands from self._commands")
        self.assertNotIn("self._events.get()", src,
                         "_worker_loop must NOT read from self._events")

    def test_close_sends_stop_to_commands(self):
        """close() must put None into _commands to stop the worker."""
        root = _make_hidden_root()
        self.addCleanup(root.destroy)
        settings = create_settings("c1", 0.0, 2.0, 1280, 720)
        win = w.SmartCropWindow(root, "nonexistent.mp4", 0.0, 2.0, "c1", settings)
        win.close()
        # After close, _commands should contain the stop sentinel (None)
        # (plus any unprocessed get_frame messages).
        found_stop = False
        while True:
            try:
                item = win._commands.get_nowait()
            except queue.Empty:
                break
            if item is None:
                found_stop = True
        self.assertTrue(found_stop,
                        "close() must enqueue None into _commands")

    def test_request_frame_sends_absolute_time_to_decoder(self):
        """_request_frame must hand the worker the ABSOLUTE source time.

        Regression for the bug where the decoder received the local Short time
        (0..duration) and therefore showed the beginning of the source instead of
        the candidate segment.  The worker decodes with ``start + local_time``,
        so a request for local 0 must enqueue ``start`` and local mid must enqueue
        ``start + mid`` -- never a double-added ``2*start``.
        """
        start, end = 857.68, 906.2
        duration = end - start
        root = _make_hidden_root()
        self.addCleanup(root.destroy)
        settings = create_settings("c1", start, end, 1280, 720)
        win = w.SmartCropWindow(root, "nonexistent.mp4", start, end, "c1", settings)
        self.addCleanup(win.close)

        def _drain():
            """Drop every queued command, returning the last one seen."""
            last = None
            while True:
                try:
                    last = win._commands.get_nowait()
                except queue.Empty:
                    break
            return last

        # __init__ already queued the initial seek; flush it.
        _drain()

        # Local start of the Short -> absolute source start.
        win._request_frame(0.0)
        msg = _drain()
        self.assertEqual(msg[0], "get_frame")
        self.assertAlmostEqual(msg[2], start, places=6,
                               msg="local 0 must map to absolute start")
        self.assertNotAlmostEqual(msg[2], 2 * start, places=6,
                                  msg="double addition of start detected")

        # Local end -> absolute end.
        win._request_frame(duration)
        msg = _drain()
        self.assertAlmostEqual(msg[2], end, places=6,
                               msg="local duration must map to absolute end")

        # Local midpoint -> absolute start + mid (no offset drift).
        win._request_frame(duration / 2)
        msg = _drain()
        self.assertAlmostEqual(msg[2], start + duration / 2, places=6,
                               msg="local mid must map to start + mid")


def _make_hidden_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    return root


if __name__ == "__main__":
    unittest.main()
