import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from math import floor as math_floor, ceil as math_ceil
from pathlib import Path
from unittest.mock import Mock, patch

from subtitle_settings import DEFAULTS, build_ass, subtitle_area
from subtitle_background import (MeasurementCache, prepare_ass, rounded_rectangle_path,
                                 _diagnostic_ass, _events, _measure_text, _with_events, _escape_path)


SRT = "1\n00:00:00,000 --> 00:00:02,000\nПервая\nЯ\n"
CUSTOM = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                 background_padding_x_px=24, background_padding_y_px=12, background_corner_radius_px=20)


class PreparationTests(unittest.TestCase):
    def test_legacy_and_disabled_never_measure(self):
        for settings in (DEFAULTS, replace(CUSTOM, background_enabled=False),
                         replace(CUSTOM, background_geometry_mode="legacy")):
            with self.subTest(settings=settings), patch("subtitle_background._measure_text") as measure:
                self.assertEqual(prepare_ass(SRT, settings, 720, 1280), build_ass(SRT, settings))
                measure.assert_not_called()

    def test_one_vector_per_cue_preserves_text_events_and_highlight(self):
        srt = SRT + "\n2\n00:00:03,000 --> 00:00:05,000\nПервая\nЯ\n"
        words = [[{"word": "Первая", "start": start, "end": start+1},
                  {"word": "Я", "start": start+1, "end": start+2}] for start in (0, 3)]
        settings = replace(CUSTOM, highlight_word_enabled=True)
        with patch("subtitle_background._measure_text", return_value=(100, 200, 300, 400)) as measure:
            result = prepare_ass(srt, settings, 720, 1280, words)
        events = _events(result)
        shapes = [event for event in events if event[0] == "Dialogue: 0"]
        self.assertEqual(len(shapes), 2)
        self.assertEqual([event[1:3] for event in shapes], [["0:00:00.00", "0:00:02.00"], ["0:00:03.00", "0:00:05.00"]])
        expected_text = _events(build_ass(srt, replace(settings, background_enabled=False), words))
        self.assertEqual([event for event in events if event[0] == "Dialogue: 1"], expected_text)
        self.assertTrue(all(r"\p7" in event[9] and " b " not in event[9] for event in shapes))
        self.assertEqual(settings.background_corner_radius_px, 20)
        measure.assert_called_once()  # Equal text/layout, different cue timings.

    def test_full_region_uses_area_not_text_and_never_touches_measurement_cache(self):
        settings = replace(CUSTOM, position_mode="free", area_enabled=True, background_fill_region=True,
                           width_percent=50, height_percent=17, x=0, y=100)
        documents = []
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")), \
                patch("subtitle_background.MeasurementCache", side_effect=AssertionError("Unexpected cache")):
            for width, height in ((720, 1280), (1080, 1920)):
                left, top, right, bottom = subtitle_area(settings)
                bounds = (left*width/384, top*height/288, right*width/384, bottom*height/288)
                expected = rounded_rectangle_path(bounds, 20, width, height)
                paths = []
                for text in ("Я", "Очень длинная реплика " * 5, "Первая\nВторая\nТретья"):
                    srt = "1\n00:00:00,000 --> 00:00:02,000\n" + text + "\n"
                    doc = prepare_ass(srt, settings, width, height)
                    documents.append(doc)
                    shapes = [event for event in _events(doc) if event[0] == "Dialogue: 0"]
                    self.assertEqual(len(shapes), 1)
                    self.assertNotIn(r"\clip", shapes[0][9])
                    paths.append(shapes[0][9].split("}", 1)[1])
                    self.assertEqual(paths[-1], expected)
                    self.assertEqual([event for event in _events(doc) if event[0] == "Dialogue: 1"],
                                     _events(build_ass(srt, replace(settings, background_enabled=False))))
                self.assertEqual(paths, [expected]*3)
        self.assertEqual(len(documents), 6)

    def test_region_padding_ignored_radius_active_and_text_highlight_preserved(self):
        cache = Mock()
        cache.get_or_measure.side_effect = AssertionError("Unexpected cache lookup")
        settings = replace(CUSTOM, position_mode="free", area_enabled=True, background_fill_region=True,
                           width_percent=50, height_percent=25, highlight_word_enabled=True)
        words = [[{"word": "Первая", "start": 0, "end": .5}, {"word": "Я", "start": .5, "end": 2}]]
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")):
            doc = prepare_ass(SRT, settings, 1080, 1920, words, cache=cache)
            changed = replace(settings, background_padding_x_px=512, background_padding_y_px=0)
            self.assertEqual(doc, prepare_ass(SRT, changed, 1080, 1920, words, cache=cache))
            square = prepare_ass(SRT, replace(settings, background_corner_radius_px=0), 1080, 1920, words, cache=cache)
            small = replace(settings, height_percent=1, background_corner_radius_px=512)
            limited = prepare_ass(SRT, small, 1080, 1920, words, cache=cache)
            expected_radius = (subtitle_area(small)[3]-subtitle_area(small)[1])*1920/288/2
            self.assertEqual(limited, prepare_ass(SRT, replace(small, background_corner_radius_px=expected_radius),
                                                  1080, 1920, words, cache=cache))
        cache.get_or_measure.assert_not_called()
        shapes = [event for event in _events(doc) if event[0] == "Dialogue: 0"]
        square_shapes = [event for event in _events(square) if event[0] == "Dialogue: 0"]
        self.assertEqual(len(shapes), 1)
        self.assertIn(" b ", shapes[0][9])
        self.assertNotIn(" b ", square_shapes[0][9])
        self.assertEqual(shapes[0][1:3], ["0:00:00.00", "0:00:02.00"])
        texts = [event for event in _events(doc) if event[0] == "Dialogue: 1"]
        self.assertEqual(texts, _events(build_ass(SRT, replace(settings, background_enabled=False), words)))
        self.assertTrue(all(r"\clip(" in event[9] for event in texts))

    def test_dormant_fill_and_mode_switch_preserve_radius_and_cached_text_bounds(self):
        cache = MeasurementCache()
        settings = replace(CUSTOM, position_mode="free", area_enabled=True)
        with patch("subtitle_background._measure_text", return_value=(100, 200, 300, 400)) as measure:
            text = prepare_ass(SRT, settings, 720, 1280, cache=cache)
            self.assertEqual(text, prepare_ass(SRT, replace(settings, background_corner_radius_px=512),
                                               720, 1280, cache=cache))
            prepare_ass(SRT, replace(settings, background_fill_region=True), 720, 1280, cache=cache)
            self.assertEqual(prepare_ass(SRT, settings, 720, 1280, cache=cache), text)
            measure.assert_called_once()
            dormant = replace(settings, area_enabled=False, background_fill_region=True)
            doc = prepare_ass(SRT, dormant, 720, 1280, cache=cache)
            self.assertEqual(doc, prepare_ass(SRT, replace(dormant, background_fill_region=False), 720, 1280, cache=cache))
            self.assertEqual(measure.call_count, 2)
            disabled = replace(dormant, background_enabled=False)
            self.assertEqual(prepare_ass(SRT, disabled, 720, 1280), build_ass(SRT, disabled))
            self.assertEqual(measure.call_count, 2)
        self.assertEqual(settings.background_corner_radius_px, 20)
        self.assertEqual(dormant.background_corner_radius_px, 20)
        shape = next(event for event in _events(text) if event[0] == "Dialogue: 0")
        self.assertIn(r"\clip(", shape[9])
        self.assertNotIn(" b ", shape[9])

    def test_cache_reuses_background_edits_and_invalidates_text_geometry(self):
        cache = MeasurementCache()
        with patch("subtitle_background._measure_text", return_value=(100, 200, 300, 400)) as measure:
            prepare_ass(SRT, CUSTOM, 720, 1280, cache=cache)
            for change in ({"background_padding_x_px": 37}, {"background_padding_y_px": 4},
                           {"background_corner_radius_px": 0}, {"background_color": "#123456"},
                           {"background_opacity": 42}, {"highlight_word_enabled": True},
                           {"highlight_word_color": "#123456"}):
                prepare_ass(SRT, replace(CUSTOM, **change), 720, 1280, cache=cache)
            self.assertEqual(measure.call_count, 1)
            changes = ({"font": "Tahoma"}, {"font_size": 24}, {"x": 40}, {"y": 60},
                       {"position_mode": "free"}, {"width_percent": 45}, {"height_percent": 30},
                       {"position_mode": "free", "area_enabled": True},
                       {"outline_enabled": False}, {"outline_color": "#123456"})
            for index, change in enumerate(changes, 2):
                prepare_ass(SRT, replace(CUSTOM, **change), 720, 1280, cache=cache)
                self.assertEqual(measure.call_count, index)
            prepare_ass(SRT, CUSTOM, 1080, 1920, cache=cache)
            self.assertEqual(measure.call_count, len(changes)+2)
            prepare_ass(SRT.replace("Первая", "Вторая"), CUSTOM, 720, 1280, cache=cache)
            self.assertEqual(measure.call_count, len(changes)+3)

    def test_cache_lru_errors_and_close(self):
        cache = MeasurementCache(max_entries=1)
        calls = []

        def calculate():
            calls.append(1)
            return (1, 2, 3, 4)

        cache.get_or_measure("a", calculate)
        cache.get_or_measure("a", calculate)
        cache.get_or_measure("b", calculate)
        cache.get_or_measure("a", calculate)
        self.assertEqual(len(calls), 3)
        with self.assertRaisesRegex(ValueError, "failure"):
            cache.get_or_measure("error", lambda: (_ for _ in ()).throw(ValueError("failure")))
        cache.get_or_measure("error", calculate)
        self.assertEqual(len(calls), 4)
        cache.close()
        with self.assertRaisesRegex(ValueError, "закрыто"):
            cache.get_or_measure("a", calculate)

    def test_radius_zero_clamp_and_nonuniform_coordinate_scaling(self):
        for width, height in ((720, 1280), (1080, 1920)):
            bounds = (100, 200, 300, 240)
            path = rounded_rectangle_path(bounds, 0, width, height)
            self.assertNotIn(" b ", path)
            self.assertTrue(path.startswith(f"m {round(100*384/width*64)} {round(200*288/height*64)}"))
            self.assertEqual(rounded_rectangle_path(bounds, 512, width, height),
                             rounded_rectangle_path(bounds, 20, width, height))

    def test_measurement_errors_are_explicit(self):
        document = build_ass(SRT, replace(CUSTOM, background_enabled=False))
        event = _events(document)[0]
        failures = (
            (subprocess.CompletedProcess([], 1, b"", b"libass failed"), "libass failed"),
            (subprocess.CompletedProcess([], 0, b"short", b""), "неполный кадр"),
            (subprocess.CompletedProcess([], 0, bytes(20*40), b""), "видимых символов"),
            (subprocess.CompletedProcess([], 0, b"\xff"*(20*40), b""), "превышает"),
        )
        for result, message in failures:
            with self.subTest(message=message), patch("subtitle_background.subprocess.run", return_value=result):
                with self.assertRaisesRegex(ValueError, message):
                    _measure_text(document, event, 20, 40)
        for error, message in ((FileNotFoundError("ffmpeg"), "запустить FFmpeg"),
                               (subprocess.TimeoutExpired("ffmpeg", 15), "15 секунд")):
            with patch("subtitle_background.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(ValueError, message):
                    _measure_text(document, event, 20, 40)

    def test_diagnostic_does_not_edit_escaped_literal_tags(self):
        srt = SRT.replace("Первая", r"{\pos(1,2)\clip(1,2,3,4)}")
        settings = replace(CUSTOM, position_mode="free", area_enabled=True)
        document = build_ass(srt, replace(settings, background_enabled=False))
        diagnostic, _ = _diagnostic_ass(document, _events(document)[0], 720, 1280)
        self.assertIn(r"\{\\pos(1,2)\\clip(1,2,3,4)\}", diagnostic)
        self.assertNotIn(r"\clip(60.000", diagnostic)


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class SyntheticFFmpegTests(unittest.TestCase):
    def raster(self, document, width, height, frames=1, rgb=False):
        with tempfile.TemporaryDirectory(prefix="test_subtitle_vector_") as directory:
            path = Path(directory) / "test.ass"
            path.write_text(document, encoding="utf-8-sig")
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 f"color=c=black:s={width}x{height}:r=2:d=1", "-vf", f"subtitles='{_escape_path(path)}'",
                 "-frames:v", str(frames), "-threads", "1", "-f", "rawvideo", "-pix_fmt",
                 "rgb24" if rgb else "gray", "pipe:1"], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(len(result.stdout), width*height*(3 if rgb else 1)*frames)
        return result.stdout

    def bounds(self, pixels, width, height):
        rows = [y for y in range(height) if any(pixels[y*width:(y+1)*width])]
        self.assertTrue(rows)
        left = min(width - len(pixels[y*width:(y+1)*width].lstrip(b"\0")) for y in rows)
        right = max(len(pixels[y*width:(y+1)*width].rstrip(b"\0")) for y in rows)
        return left, rows[0], right, rows[-1]+1

    def test_measurement_matches_original_historical_free_and_edge_text(self):
        for width, height in ((720, 1280), (1080, 1920)):
            for settings in (CUSTOM, replace(CUSTOM, position_mode="free", x=35.3, y=60.7),
                             replace(CUSTOM, position_mode="free", x=0, y=0)):
                with self.subTest(width=width, position=settings.position_mode, x=settings.x):
                    white = replace(settings, background_enabled=False, outline_color="#FFFFFF")
                    document = build_ass(SRT, white)
                    measured = _measure_text(document, _events(document)[0], width, height)
                    actual = self.bounds(self.raster(document, width, height), width, height)
                    expected = (max(0, measured[0]), max(0, measured[1]), min(width, measured[2]), min(height, measured[3]))
                    self.assertLessEqual(max(abs(a-b) for a, b in zip(actual, expected)), 1)

    def test_padding_radius_color_alpha_and_highlight_both_resolutions(self):
        for width, height in ((720, 1280), (1080, 1920)):
            with self.subTest(width=width):
                settings = replace(CUSTOM, position_mode="free", x=50, y=50,
                                   background_color="#FFFFFF", background_opacity=100)
                cache = MeasurementCache()
                plain = build_ass(SRT, replace(settings, background_enabled=False))
                text_bounds = _measure_text(plain, _events(plain)[0], width, height)
                for px, py, radius in ((37, 4, 0), (4, 37, 0), (24, 12, 20)):
                    style = replace(settings, background_padding_x_px=px, background_padding_y_px=py,
                                    background_corner_radius_px=radius)
                    document = prepare_ass(SRT, style, width, height, cache=cache)
                    shapes = [event for event in _events(document) if event[0] == "Dialogue: 0"]
                    self.assertNotIn(" b ", shapes[0][9])  # Stored radius does not round a text-sized box.
                    pixels = self.raster(_with_events(document, shapes), width, height)
                    actual = self.bounds(pixels, width, height)
                    expected = (text_bounds[0]-px, text_bounds[1]-py, text_bounds[2]+px, text_bounds[3]+py)
                    self.assertLessEqual(max(abs(a-b) for a, b in zip(actual, expected)), 1)
                # Circularity check against a 40px analytic quarter-circle at 50% coverage.
                shape = shapes[0].copy()
                box = (width//2-100, height//2-100, width//2+100, height//2+100)
                shape[9] = shape[9].split("}", 1)[0] + "}" + rounded_rectangle_path(box, 40, width, height)
                pixels = self.raster(_with_events(document, [shape]), width, height)
                l, t, _, _ = box
                row_inset = next(x for x in range(40) if pixels[(t+1)*width+l+x] > 127)
                col_inset = next(y for y in range(40) if pixels[(t+y)*width+l+1] > 127)
                self.assertLessEqual(abs(row_inset-col_inset), 1)
                mismatch = sum((pixels[(t+y)*width+l+x] > 127) != ((x+.5-40)**2+(y+.5-40)**2 <= 1600)
                               for y in range(40) for x in range(40))
                self.assertLessEqual(mismatch, 45)
                for opacity in (0, 50, 100):
                    style = replace(settings, background_color="#FF0000", background_opacity=opacity)
                    doc = prepare_ass(SRT, style, width, height, cache=cache)
                    shapes = [event for event in _events(doc) if event[0] == "Dialogue: 0"]
                    rgb = self.raster(_with_events(doc, shapes), width, height, rgb=True)
                    index = ((text_bounds[1]+text_bounds[3])//2*width + (text_bounds[0]+text_bounds[2])//2)*3
                    red, green, blue = rgb[index:index+3]
                    self.assertLessEqual(abs(red - (255-round(255*(1-opacity/100)))), 3)
                    self.assertLessEqual(max(green, blue), 3)
                highlighted = replace(settings, highlight_word_enabled=True)
                words = [[{"word": "Первая", "start": 0, "end": .5}, {"word": "Я", "start": .5, "end": 1}]]
                doc = prepare_ass(SRT, highlighted, width, height, words, cache=cache)
                shapes = [event for event in _events(doc) if event[0] == "Dialogue: 0"]
                self.assertEqual(len(shapes), 1)
                pixels = self.raster(doc, width, height, frames=2, rgb=True)
                self.assertNotEqual(pixels[:width*height*3], pixels[width*height*3:])
                bg = self.raster(_with_events(doc, shapes), width, height, frames=2, rgb=True)
                self.assertEqual(bg[:width*height*3], bg[width*height*3:])

    def test_wrap_outline_and_area_clipping(self):
        srt = SRT.replace("Первая\nЯ", "Это длинная реплика для проверки автоматических переносов текста по ширине области")
        for width, height in ((720, 1280), (1080, 1920)):
            base = replace(CUSTOM, position_mode="free", x=50, y=50, area_enabled=True, height_percent=100)
            wide = build_ass(srt, replace(base, background_enabled=False, width_percent=80))
            narrow = build_ass(srt, replace(base, background_enabled=False, width_percent=45))
            a = _measure_text(wide, _events(wide)[0], width, height)
            b = _measure_text(narrow, _events(narrow)[0], width, height)
            self.assertGreater(b[3]-b[1], a[3]-a[1])
            no_outline = build_ass(srt, replace(base, background_enabled=False, width_percent=45, outline_enabled=False))
            c = _measure_text(no_outline, _events(no_outline)[0], width, height)
            self.assertLess(c[3]-c[1], b[3]-b[1])
            clipped = replace(base, height_percent=12.5)
            doc = prepare_ass(SRT, clipped, width, height)
            bounds = self.bounds(self.raster(doc, width, height), width, height)
            self.assertGreaterEqual(bounds[1], math_floor(height*.4375)-math_ceil(height/288)-1)
            self.assertLessEqual(bounds[3], math_ceil(height*.5625)+math_ceil(height/288)+1)

    def test_full_region_raster_bounds_circles_and_frame_edges(self):
        settings = replace(CUSTOM, position_mode="free", area_enabled=True, background_fill_region=True,
                           width_percent=50, height_percent=25, background_color="#FFFFFF", background_opacity=100)
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")):
            for width, height in ((720, 1280), (1080, 1920)):
                for x, y, percent in ((50, 50, 25), (0, 0, 17), (100, 100, 17)):
                    for radius in (0, 40):
                        with self.subTest(width=width, x=x, y=y, radius=radius):
                            style = replace(settings, x=x, y=y, height_percent=percent, background_corner_radius_px=radius)
                            document = prepare_ass(SRT, style, width, height)
                            shapes = [event for event in _events(document) if event[0] == "Dialogue: 0"]
                            self.assertEqual(len(shapes), 1)
                            self.assertNotIn(r"\clip", shapes[0][9])
                            pixels = self.raster(_with_events(document, shapes), width, height)
                            actual = self.bounds(pixels, width, height)
                            l, t, r, b = subtitle_area(style)
                            expected = (l*width/384, t*height/288, r*width/384, b*height/288)
                            self.assertLessEqual(max(abs(a-b) for a, b in zip(actual, expected)), 1)
                            if radius and x == 50:
                                left, top = round(expected[0]), round(expected[1])
                                mismatch = sum((pixels[(top+dy)*width+left+dx] > 127) !=
                                               ((dx+.5-40)**2+(dy+.5-40)**2 <= 1600)
                                               for dy in range(40) for dx in range(40))
                                self.assertLessEqual(mismatch, 45)
                                row = next(dx for dx in range(40) if pixels[(top+1)*width+left+dx] > 127)
                                col = next(dy for dy in range(40) if pixels[(top+dy)*width+left+1] > 127)
                                self.assertLessEqual(abs(row-col), 1)

    def test_full_region_pixels_independent_of_text_padding_and_font(self):
        style = replace(CUSTOM, position_mode="free", area_enabled=True, background_fill_region=True,
                        x=50, y=50, width_percent=50, height_percent=25, background_color="#20A060", background_opacity=70)
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")):
            for width, height in ((720, 1280), (1080, 1920)):
                images = []
                for text, font_size, px, py in (("Я", 10, 0, 512), ("Длинная строка с текстом", 30, 512, 0),
                                               ("Одна\nВторая\nТретья строка", 18, 37, 4)):
                    document = prepare_ass("1\n00:00:00,000 --> 00:00:02,000\n" + text + "\n",
                                           replace(style, font_size=font_size, background_padding_x_px=px,
                                                   background_padding_y_px=py), width, height)
                    shapes = [event for event in _events(document) if event[0] == "Dialogue: 0"]
                    images.append(self.raster(_with_events(document, shapes), width, height, rgb=True))
                self.assertEqual(images[0], images[1])
                self.assertEqual(images[1], images[2])
                center = (height//2*width+width//2)*3
                self.assertTrue(all(abs(actual-expected) <= 3 for actual, expected in
                                    zip(images[0][center:center+3], (32*.7, 160*.7, 96*.7))))

    def test_full_region_alpha_highlight_and_text_clip(self):
        style = replace(CUSTOM, position_mode="free", area_enabled=True, background_fill_region=True,
                        x=50, y=50, width_percent=50, height_percent=17, background_color="#FF0000", highlight_word_enabled=True)
        words = [[{"word": "Первая", "start": 0, "end": .5}, {"word": "Я", "start": .5, "end": 2}]]
        for width, height in ((720, 1280), (1080, 1920)):
            for opacity in (0, 50, 100):
                doc = prepare_ass(SRT, replace(style, background_opacity=opacity), width, height, words)
                shapes = [event for event in _events(doc) if event[0] == "Dialogue: 0"]
                rgb = self.raster(_with_events(doc, shapes), width, height, rgb=True)
                center = (height//2*width+width//2)*3
                red, green, blue = rgb[center:center+3]
                self.assertLessEqual(abs(red-(255-round(255*(1-opacity/100)))), 3)
                self.assertLessEqual(max(green, blue), 3)
            text = [event for event in _events(doc) if event[0] == "Dialogue: 1"]
            self.assertEqual(text, _events(build_ass(SRT, replace(style, background_enabled=False), words)))
            self.assertTrue(all(r"\clip(" in event[9] for event in text))
            pixels = self.raster(doc, width, height, frames=2, rgb=True)
            self.assertNotEqual(pixels[:width*height*3], pixels[width*height*3:])
            pixels = self.raster(_with_events(doc, shapes), width, height, frames=2, rgb=True)
            self.assertEqual(pixels[:width*height*3], pixels[width*height*3:])


if __name__ == "__main__":
    unittest.main()
