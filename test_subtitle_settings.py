import copy
import json
import shutil
import subprocess
import tempfile
import time
import tkinter as tk
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from subtitle_settings import (DEFAULTS, SubtitleSettings, ass_color, build_ass,
                               document, from_document, load_settings, save_settings, _ass_time, subtitle_area)
from subs_settings_window import SubsSettingsWindow, render_preview


class SettingsTests(unittest.TestCase):
    def test_validation(self):
        for key, value in (("font", ""), ("font", "Arial,Outline=99"), ("font", "A\nB"),
                           ("font_size", 0), ("font_size", -1), ("font_size", float("nan")),
                           ("font_size", True), ("x", -1), ("y", 101), ("x", float("inf")),
                           ("background_color", "#12345"), ("background_opacity", 101),
                           ("background_enabled", "yes"), ("position_mode", "unknown"),
                           ("outline_enabled", 1), ("outline_color", "red"),
                           ("area_enabled", "yes"), ("width_percent", 0), ("width_percent", True),
                           ("height_percent", 101), ("height_percent", float("nan"))):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                replace(DEFAULTS, **{key: value})
        self.assertEqual(replace(DEFAULTS, x=42.5).x, 42.5)

    def test_preset_roundtrip_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preset.json"
            custom = replace(DEFAULTS, font="Missing Font Family", x=12.25, background_enabled=True,
                             outline_enabled=False, outline_color="#123456", area_enabled=True,
                             position_mode="free", width_percent=42.25, height_percent=18.5)
            save_settings(path, custom)
            self.assertEqual(load_settings(path), custom)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            for content in ("{", "[]", json.dumps({"schema_version": 9, "shorts": asdict(DEFAULTS)}),
                            json.dumps({"schema_version": 1, "shorts": {"font": "Arial"}})):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_settings(path)

    def test_v1_migration_preserves_style_and_v2_is_strict(self):
        old_fields = {k: v for k, v in asdict(DEFAULTS).items()
                      if k not in {"outline_enabled", "outline_color", "area_enabled", "width_percent", "height_percent"}}
        self.assertEqual(from_document({"schema_version": 1, "shorts": old_fields}), DEFAULTS)
        old_fields.update(position_mode="free", x=40, y=65, background_enabled=True)
        migrated = from_document({"schema_version": 1, "shorts": old_fields})
        self.assertFalse(migrated.area_enabled)
        self.assertTrue(migrated.outline_enabled)
        self.assertEqual(migrated.y, 65)
        self.assertEqual(document(migrated)["schema_version"], 2)
        with self.assertRaises(ValueError):
            from_document({"schema_version": 2, "shorts": old_fields})

    def test_area_and_outline_ass_keep_font_size(self):
        settings = replace(DEFAULTS, position_mode="free", x=0, y=100, area_enabled=True,
                           width_percent=50, height_percent=20, outline_enabled=False, outline_color="#123456")
        left, top, right, bottom = subtitle_area(settings)
        self.assertEqual((left, right), (0, 192))
        self.assertAlmostEqual(bottom, 288)
        srt = "1\n00:00:00,000 --> 00:00:02,000\nПример\n"
        ass = build_ass(srt, settings)
        self.assertIn("&H00563412&", ass)
        self.assertIn(r"\clip(0.000,230.400,192.000,288.000)", ass)
        self.assertIn("Style: Text,Arial,18,", ass)
        self.assertNotIn(r"\fs", ass)

    def test_colors_defaults_and_ass(self):
        self.assertEqual(_ass_time("00:00:59,999"), "0:01:00.00")
        self.assertEqual(_ass_time("00:00:01,125"), "0:00:01.13")
        self.assertEqual(ass_color("#123456", 100), "&H00563412&")
        self.assertEqual(ass_color("#123456", 0), "&HFF563412&")
        self.assertEqual(ass_color("#123456", 50), "&H80563412&")
        self.assertEqual(DEFAULTS.font_size, 18)
        self.assertFalse(DEFAULTS.background_enabled)
        self.assertEqual(DEFAULTS.position_mode, "historical")
        srt = "1\n00:00:01,250 --> 00:00:02,500\n{literal}\nsecond line\n"
        ass = build_ass(srt, replace(DEFAULTS, position_mode="free", x=0, y=100, background_enabled=True))
        self.assertIn("0:00:01.25,0:00:02.50", ass)
        self.assertIn(r"\{literal\}\Nsecond line", ass)
        self.assertIn(r"\an5\pos(", ass)
        self.assertIn("Dialogue: 0", ass)
        self.assertIn("Dialogue: 1", ass)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.applied = []
        self.window = SubsSettingsWindow(self.root, DEFAULTS, self.applied.append)

    def tearDown(self):
        self.window.close()
        self.root.destroy()

    def test_edit_apply_defaults_and_close(self):
        self.window.variables["x"].set("70.5")
        self.assertEqual(self.window.current().position_mode, "free")
        self.window.variables["background_enabled"].set(True)
        self.window.apply()
        self.assertEqual(self.applied[-1].x, 70.5)
        self.window.populate(DEFAULTS)
        self.assertEqual(self.window.current(), DEFAULTS)
        self.assertTrue(self.applied[-1].background_enabled)  # reset is draft only
        self.window.close()
        self.assertTrue(self.window.closed)
        self.assertEqual(self.root.tk.call("after", "info"), "")

    def test_new_controls_and_color_picker(self):
        self.window.variables["width_percent"].set("45.5")
        self.window.variables["height_percent"].set("12.5")
        self.window.variables["outline_enabled"].set(False)
        with patch("subs_settings_window.colorchooser.askcolor", return_value=((18, 52, 86), "#123456")):
            self.window.choose_color("outline_color")
        self.window.apply()
        settings = self.applied[-1]
        self.assertEqual((settings.width_percent, settings.height_percent), (45.5, 12.5))
        self.assertEqual(settings.font_size, DEFAULTS.font_size)
        self.assertTrue(settings.area_enabled)
        self.assertEqual(settings.position_mode, "free")
        self.assertFalse(settings.outline_enabled)
        self.assertEqual(settings.outline_color, "#123456")
        self.window.populate(DEFAULTS)
        self.assertTrue(self.window.current().outline_enabled)
        self.assertFalse(self.window.current().area_enabled)

    def test_save_load_and_invalid_load_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "preset.json")
            self.window.variables["font_size"].set("24")
            self.window.variables["width_percent"].set("40.5")
            self.window.variables["height_percent"].set("25")
            self.window.variables["outline_color"].set("#123456")
            self.window.variables["outline_enabled"].set(False)
            exported = self.window.current()
            with patch("subs_settings_window.filedialog.asksaveasfilename", return_value=path), \
                    patch("subs_settings_window.messagebox.showinfo"):
                self.window.save()
            self.assertFalse(self.applied)
            self.window.populate(DEFAULTS)
            with patch("subs_settings_window.filedialog.askopenfilename", return_value=path):
                self.window.load()
            self.assertEqual(self.window.current().font_size, 24)
            self.assertEqual(self.window.current(), exported)
            Path(path).write_text("{}", encoding="utf-8")
            before = self.window.current()
            with patch("subs_settings_window.filedialog.askopenfilename", return_value=path), \
                    patch("subs_settings_window.messagebox.showerror") as error:
                self.window.load()
                error.assert_called_once()
            self.assertEqual(self.window.current(), before)

    def test_singleton_and_preview(self):
        from review_ui import HighlightApp
        app = HighlightApp.__new__(HighlightApp)
        app.root = self.root
        app.subtitle_settings = DEFAULTS
        app.subs_window = self.window
        app.open_subs_settings()
        self.assertIs(app.subs_window, self.window)
        with patch("subs_settings_window.render_preview", return_value=b"P6\n1 1\n255\n\xff\x00\x00"):
            self.window.start_preview()
            deadline = time.monotonic() + 2
            while not hasattr(self.window, "image") and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertTrue(hasattr(self.window, "image"))

    def test_root_shutdown_removes_callbacks(self):
        parent = tk.Toplevel(self.root)
        window = SubsSettingsWindow(parent, DEFAULTS, self.applied.append)
        parent.destroy()
        self.assertTrue(window.closed)

    def test_active_settings_survive_restart(self):
        import review_ui
        self.window.close()
        with tempfile.TemporaryDirectory() as directory, patch.object(review_ui, "DATA", Path(directory)):
            app = review_ui.HighlightApp(self.root)
            style = replace(DEFAULTS, background_enabled=True, background_opacity=42,
                            outline_enabled=False, outline_color="#AA5522", position_mode="free",
                            area_enabled=True, width_percent=45.5, height_percent=20.25)
            app.apply_subtitle_settings(style)
            self.assertEqual(load_settings(Path(directory) / "subtitle_settings.json"), style)
            second_root = tk.Toplevel(self.root)
            try:
                second = review_ui.HighlightApp(second_root)
                self.assertEqual(second.subtitle_settings, style)
            finally:
                second_root.destroy()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class FFmpegTests(unittest.TestCase):
    def render_ass_pixels(self, settings, text):
        from renderer import _filtergraph_escape_path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ass"
            path.write_text(build_ass("1\n00:00:00,000 --> 00:00:02,000\n" + text + "\n", settings), encoding="utf-8-sig")
            return subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                            "color=c=black:s=384x288:d=0.04", "-vf",
                                            f"subtitles='{_filtergraph_escape_path(path)}'", "-frames:v", "1",
                                            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], timeout=20)

    def test_real_area_clips_without_resizing_and_width_wraps(self):
        base = replace(DEFAULTS, position_mode="free", x=50, y=50, area_enabled=True,
                       width_percent=50, height_percent=75, background_enabled=True,
                       background_color="#FF0000")
        small = replace(base, height_percent=12.5)
        text = "Первая строка\nВторая строка\nТретья строка"
        large_pixels = self.render_ass_pixels(base, text)
        small_pixels = self.render_ass_pixels(small, text)
        self.assertTrue(large_pixels != small_pixels)
        left, top, right, bottom = subtitle_area(small)
        for y in range(288):
            row = small_pixels[y * 384 * 3:(y + 1) * 384 * 3]
            if top <= y < bottom:
                # Within the clip the glyph pixels must match the un-clipped rendering.
                self.assertTrue(row == large_pixels[y * 384 * 3:(y + 1) * 384 * 3])
                self.assertFalse(any(row[:left * 3]))
                self.assertFalse(any(row[right * 3:]))
            else:
                self.assertFalse(any(row), f"Subtitle pixels outside the region at row {y}")
        self.assertTrue(any(small_pixels))
        text = "Это длинная реплика для проверки переносов текста по ширине заданной области"
        wide = replace(base, background_enabled=False, width_percent=90, height_percent=100)
        narrow = replace(wide, width_percent=30)
        count_rows = lambda pixels: sum(any(pixels[y * 1152:(y + 1) * 1152]) for y in range(288))
        self.assertGreater(count_rows(self.render_ass_pixels(narrow, text)),
                           count_rows(self.render_ass_pixels(wide, text)))
        self.assertTrue(render_preview(base) != render_preview(small))

    def test_real_outline_toggle_and_color(self):
        base = replace(DEFAULTS, position_mode="free", x=50, y=50, outline_color="#FF0000")
        red = self.render_ass_pixels(base, "Обводка")
        green = self.render_ass_pixels(replace(base, outline_color="#00FF00"), "Обводка")
        disabled = self.render_ass_pixels(replace(base, outline_enabled=False), "Обводка")
        self.assertTrue(red != green)
        self.assertTrue(red != disabled)
        self.assertEqual(disabled, self.render_ass_pixels(replace(base, outline_enabled=False,
                                                                  outline_color="#00FF00"), "Обводка"))
    def test_default_pixels_match_original_filter(self):
        from renderer import _filtergraph_escape_path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:02,000\nПример субтитров\nВторая строка\n", encoding="utf-8-sig")
            original = (f"subtitles='{_filtergraph_escape_path(path)}':force_style='FontName=Arial,FontSize=18,"
                        "PrimaryColour=&HFFFFFF&,OutlineColour=&H00000000&,Outline=1,Alignment=2,"
                        "MarginV=240,MarginL=60,MarginR=60',scale=270:480")
            result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                     "color=c=0x405570:s=1080x1920:d=0.04", "-vf", original,
                                     "-frames:v", "1", "-f", "image2pipe", "-vcodec", "ppm", "pipe:1"],
                                    capture_output=True, check=True, timeout=20)
            self.assertEqual(render_preview(DEFAULTS), result.stdout)

    def test_real_pixels_font_size_position_background_alpha(self):
        base = replace(DEFAULTS, position_mode="free", x=50, y=50)
        original = render_preview(base)
        for changes in ({"font": "Courier New"}, {"font_size": 26}, {"x": 25}, {"y": 75},
                        {"background_enabled": True, "background_color": "#FF0000"}):
            with self.subTest(changes=changes):
                self.assertNotEqual(original, render_preview(replace(base, **changes)))
        red = replace(base, background_enabled=True, background_color="#FF0000")
        self.assertNotEqual(render_preview(red), render_preview(replace(red, background_opacity=50)))
        self.assertNotEqual(render_preview(red), render_preview(replace(red, background_color="#00FF00")))
        self.assertEqual(original, render_preview(replace(red, background_opacity=0)))
        self.assertEqual(render_preview(DEFAULTS), render_preview(replace(DEFAULTS, background_opacity=0)))

    def test_actual_renderer_custom_style_and_default(self):
        import renderer
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=320x180:d=0.4",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.4",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True, timeout=20)
            clip = {"candidate_id": "subs_test", "start": 0, "end": .4, "title": "synthetic",
                    "status": "approved", "render_settings": {}}
            clip["approval_key"] = renderer.shorts_approval_key(clip)
            report = {"signature": "test", "shorts_candidates": [clip],
                      "profiles": {"shorts": {"min_duration_sec": .1, "max_duration_sec": 1}}}
            transcript = {"segments": [{"start": 0, "end": .4, "text": "Пример субтитров",
                                        "words": [{"start": 0, "end": .2, "word": "Пример"},
                                                  {"start": .2, "end": .4, "word": "субтитров"}]}]}
            before = copy.deepcopy(report)
            frames = []
            for settings in (DEFAULTS, replace(DEFAULTS, position_mode="free", y=70,
                                               background_enabled=True, background_color="#FF0000",
                                               outline_color="#00FF00", area_enabled=True,
                                               width_percent=60, height_percent=15)):
                outputs = renderer.render_shorts_approved({"source_path": str(source)}, report, directory,
                                                         lambda _: None, transcript=transcript,
                                                         subtitle_settings=settings)
                self.assertTrue(Path(outputs[0]).exists())
                metadata = json.loads(Path(outputs[0]).with_suffix(".json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["subtitles"]["style"], document(settings))
                frames.append(subprocess.check_output(["ffmpeg", "-v", "error", "-i", outputs[0],
                                                       "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]))
            self.assertTrue(frames[0] != frames[1], "Custom style must change actual MP4 pixels")
            self.assertEqual(report, before)
            self.assertEqual(len(list(Path(directory).rglob("*.mp4"))), 3)


if __name__ == "__main__":
    unittest.main()
