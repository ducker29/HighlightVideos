"""Pure graph planning tests: no FFmpeg, probing, media or filesystem fixtures."""
import copy
from dataclasses import FrozenInstanceError
from fractions import Fraction
import re
import unittest
from unittest.mock import patch

from renderer import _crop_params, _crop_to_pixels, _filtergraph_escape_path, LEGACY_STYLE
from shorts_filtergraph import build_shorts_filtergraph
from shorts_settings import ShortsSettings
from smart_crop_storage import create_settings


def candidate(intervals=((2, 5),)):
    return {"candidate_id": "sc001", "start": 100.0, "end": 130.0,
            "render_settings": {"crop_x": None, "framing": {
                "schema_version": 1, "default_mode": "CROP", "intervals": [
                    {"start": start, "end": end, "mode": "FIT_BLUR"} for start, end in intervals]}}}


def smart_settings():
    crop_a = {"x": 0.0, "y": 0.0, "width": .25, "height": 1.0}
    crop_b = {"x": .5, "y": .25, "width": .125, "height": .5}
    return create_settings("sc001", 100, 130, 1920, 1080, [
        {"start": 0, "end": 10, "crop": crop_a},
        {"start": 10, "end": 20, "crop": crop_b},
        {"start": 20, "end": 30, "crop": crop_a}])


def active(expression, time):
    return bool(eval(expression, {"__builtins__": {}},
                     {"t": time, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b}))


class FramingFilterGraphTests(unittest.TestCase):
    def build(self, clip=None, **kwargs):
        return build_shorts_filtergraph(candidate() if clip is None else clip, 1920, 1080, **kwargs)

    def test_one_interval_crop_fit_crop(self):
        plan = self.build()
        expression = re.findall(r"enable='([^']+)'", plan.filter_complex)[0]
        for time, expected in ((0, False), (1.999, False), (2, True), (4.999, True), (5, False), (29, False)):
            with self.subTest(time=time):
                self.assertEqual(active(expression, time), expected)
        self.assertEqual((plan.output_width, plan.output_height), (1080, 1920))
        self.assertEqual(plan.output_label, "framing_video")

    def test_multiple_intervals_and_gaps(self):
        plan = self.build(candidate(((8, 10), (2, 5), (20, 25))))
        expression = re.findall(r"enable='([^']+)'", plan.filter_complex)[0]
        for time in (0, 2, 4, 5, 7, 8, 10, 20, 25, 29):
            expected = 2 <= time < 5 or 8 <= time < 10 or 20 <= time < 25
            self.assertEqual(active(expression, time), expected)
        self.assertNotIn("between(", expression)
        self.assertEqual(plan.filter_complex.count("gblur="), 1)

    def test_zero_and_exact_clip_end_are_half_open(self):
        plan = self.build(candidate(((0, 30),)))
        expression = re.findall(r"enable='([^']+)'", plan.filter_complex)[0]
        self.assertFalse(active(expression, -0.001))
        self.assertTrue(active(expression, 0))
        self.assertTrue(active(expression, 29.999))
        self.assertFalse(active(expression, 30))

    def test_adjacent_intervals_merge_before_graph_generation(self):
        a = self.build(candidate(((2, 5), (5, 10))))
        b = self.build(candidate(((2, 10),)))
        self.assertEqual(a, b)

    def test_decimal_precision_not_truncated_to_six_digits(self):
        plan = self.build(candidate(((10.032688123, 10.032688124), (0.000000001, 0.000000002))))
        expression = re.findall(r"enable='([^']+)'", plan.filter_complex)[0]
        self.assertIn("10.032688123", expression)
        self.assertIn("0.000000001", expression)
        self.assertTrue(active(expression, 10.032688123))
        self.assertFalse(active(expression, 10.032688124))
        self.assertTrue(active(expression, 0.000000001))
        self.assertFalse(active(expression, 0.000000002))

    def test_absent_empty_or_explicit_crop_selects_legacy_path(self):
        variants = [candidate(())]
        absent = candidate()
        absent["render_settings"].pop("framing")
        variants.append(absent)
        no_render = candidate()
        no_render.pop("render_settings")
        variants.append(no_render)
        crop = candidate(())
        crop["render_settings"]["framing"]["intervals"] = [{"start": 0, "end": 30, "mode": "CROP"}]
        variants.append(crop)
        with patch("renderer._crop_params", side_effect=AssertionError("legacy path must be untouched")):
            for clip in variants:
                with self.subTest(clip=clip):
                    self.assertIsNone(self.build(clip, smart_crop={"invalid": "unused"}))

    def test_default_crop_and_crop_x_reuse_current_geometry(self):
        for position in (None, 0, .25, 1):
            clip = candidate()
            clip["render_settings"]["crop_x"] = position
            plan = self.build(clip)
            rect = _crop_params(1920, 1080, position)
            self.assertIn("crop=" + ":".join(map(str, rect)) + ",scale=1080:1920", plan.filter_complex)

    def test_export_settings_determine_all_canvas_sizes(self):
        for source, resolution, expected in (((1920, 1080), "720p", (720, 1280)),
                                               ((1920, 1080), "1080p", (1080, 1920)),
                                               ((3840, 2160), "original", (1216, 2160)),
                                               ((1280, 720), "original", (404, 720))):
            with self.subTest(source=source, resolution=resolution):
                plan = build_shorts_filtergraph(candidate(), *source,
                                                export_settings=ShortsSettings(resolution=resolution))
                self.assertEqual((plan.output_width, plan.output_height), expected)
                self.assertIn(f"crop={expected[0]}:{expected[1]},gblur=", plan.filter_complex)
                self.assertIn(f"scale={expected[0]}:{expected[1]},setsar=1", plan.filter_complex)

    def test_full_frame_preserves_aspect_for_landscape_portrait_and_sar(self):
        cases = [((1920, 1080), (1, 1), (1080, 608)),
                 ((1440, 1080), (1, 1), (1080, 810)),
                 ((720, 576), (16, 15), (1080, 810)),
                 ((1080, 1920), (1, 1), (1080, 1920)),
                 ((1080, 2160), (1, 1), (960, 1920))]
        for (sw, sh), sar, foreground in cases:
            with self.subTest(source=(sw, sh), sar=sar):
                plan = build_shorts_filtergraph(candidate(), sw, sh, source_sar=sar)
                self.assertIn(f"scale={foreground[0]}:{foreground[1]},setsar=1,format=yuv420p[framing_fg]", plan.filter_complex)
                fg_branch = next(part for part in plan.filter_complex.split(";") if part.endswith("[framing_fg]"))
                self.assertNotIn("crop=", fg_branch)
                self.assertNotIn("gblur", fg_branch)
                aspect = Fraction(sw * sar[0], sh * sar[1])
                # Integer 4:2:0 rounding is bounded by one pixel in the fitted axis.
                error = min(abs(Fraction(foreground[0]) - foreground[1] * aspect),
                            abs(Fraction(foreground[1]) - foreground[0] / aspect))
                self.assertLessEqual(error, 1)

    def test_background_covers_canvas_and_only_it_is_blurred(self):
        plan = self.build()
        branches = plan.filter_complex.split(";")
        bg = next(part for part in branches if part.endswith("[framing_bg]"))
        self.assertIn("scale=3414:1920,setsar=1,crop=1080:1920,gblur=sigma=20", bg)
        self.assertEqual(sum("gblur=" in part for part in branches), 1)
        fit = next(part for part in branches if part.endswith("[framing_fit]"))
        self.assertIn("[framing_bg][framing_fg]overlay=x=(W-w)/2:y=(H-h)/2", fit)

    def test_smart_crop_zoom_uses_distinct_constant_branches(self):
        settings = smart_settings()
        with patch("renderer._crop_switch_exprs", side_effect=AssertionError("dynamic crop.w/h forbidden")):
            plan = self.build(smart_crop=settings)
        graph = plan.filter_complex
        self.assertIn("split=4", graph)  # 2 distinct crops + background + foreground
        for index, seg in enumerate(settings["segments"][:2]):
            rect = _crop_to_pixels(seg["crop"], 1920, 1080)
            self.assertIn("crop=" + ":".join(map(str, rect)) + ",scale=1080:1920", graph)
        self.assertNotIn("crop=w=", graph)
        self.assertNotIn("if(", graph)
        switches = re.findall(r"enable='([^']+)'", graph)
        self.assertEqual(len(switches), 2)
        for time in (0, 9.999, 10, 19.999, 20, 29.999):
            self.assertEqual(active(switches[0], time), 10 <= time < 20)

    def test_smart_crop_and_fit_boundaries_are_independent(self):
        plan = self.build(candidate(((5, 15), (25, 30))), smart_crop=smart_settings())
        crop_switch, fit_switch = re.findall(r"enable='([^']+)'", plan.filter_complex)
        for time, expected in ((0, "crop0"), (5, "fit"), (10, "fit"), (15, "crop1"),
                               (20, "crop0"), (25, "fit"), (29.999, "fit")):
            selected = "fit" if active(fit_switch, time) else "crop1" if active(crop_switch, time) else "crop0"
            self.assertEqual(selected, expected)

    def test_one_smart_segment_has_no_extra_crop_switch(self):
        settings = create_settings("sc001", 100, 130, 1920, 1080)
        plan = self.build(smart_crop=settings)
        self.assertIn("split=3", plan.filter_complex)
        self.assertEqual(plan.filter_complex.count("enable="), 1)

    def test_repeated_second_geometry_reuses_branch_and_combines_periods(self):
        settings = smart_settings()
        a, b = settings["segments"][0]["crop"], settings["segments"][1]["crop"]
        settings["segments"] = [{"start": index * 5, "end": (index + 1) * 5,
                                  "crop": copy.deepcopy(a if index % 2 == 0 else b)} for index in range(6)]
        plan = self.build(smart_crop=settings)
        expression = re.findall(r"enable='([^']+)'", plan.filter_complex)[0]
        self.assertIn("split=4", plan.filter_complex)
        for time in (0, 5, 9, 10, 15, 20, 25, 30):
            self.assertEqual(active(expression, time), 5 <= time < 10 or 15 <= time < 20 or 25 <= time < 30)

    def test_smart_crop_metadata_mismatch_rejected(self):
        for key, value in (("candidate_id", "other"), ("source_start", 99), ("source_end", 131),
                           ("source_width", 1280), ("source_height", 720)):
            settings = smart_settings()
            settings[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.build(smart_crop=settings)

    def test_invalid_framing_or_stale_anchor_rejected(self):
        for intervals in (((-1, 3),), ((1, 31),), ((2, 5), (4, 8)), ((2, float("inf")),)):
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                self.build(candidate(intervals))
        clip = candidate()
        clip["render_settings"]["framing_source_bounds"] = {"start": 99, "end": 129}
        with self.assertRaises(ValueError):
            self.build(clip)

    def test_subtitles_are_unchanged_single_final_filter(self):
        for custom in (False, True):
            path = _filtergraph_escape_path("D:/Temp/Субтитры/clip.ass" if custom else "D:/Temp/clip.srt")
            original = f"subtitles='{path}'" + ("" if custom else f":force_style='{LEGACY_STYLE}'")
            plan = self.build(subtitles_filter=original, smart_crop=smart_settings())
            self.assertTrue(plan.filter_complex.endswith(f"[framing_composed]{original}[framing_video]"))
            self.assertEqual(plan.filter_complex.count(original), 1)
            self.assertGreater(plan.filter_complex.index(original), plan.filter_complex.rindex("overlay="))

    def test_subtitles_disabled(self):
        graph = self.build().filter_complex
        self.assertNotIn("subtitles=", graph)
        self.assertTrue(graph.endswith("[framing_composed]null[framing_video]"))

    def test_common_timebase_formats_and_no_retiming_or_audio(self):
        graph = self.build(smart_crop=smart_settings()).filter_complex
        self.assertTrue(graph.startswith("[0:v:0]settb=expr=intb,format=yuv420p,split="))
        self.assertEqual(graph.count("settb="), 1)
        for forbidden in ("setpts", "asetpts", "atrim", "aresample", "concat", "trim=", "fps=",
                          "shortest=", "[0:a", "-ss", "-t ", "-map", "-c:a", "AVTB"):
            self.assertNotIn(forbidden, graph)
        for branch in graph.split(";"):
            if any(branch.endswith(f"[framing_{name}]") for name in ("crop0", "crop1", "bg", "fg", "fit", "composed")):
                self.assertIn("format=yuv420p", branch)
        self.assertEqual(graph.count("repeatlast=0"), graph.count("overlay="))

    def test_all_generated_pads_connected_exactly_once(self):
        plan = self.build(smart_crop=smart_settings())
        from collections import Counter
        labels = Counter(re.findall(r"\[([^\]]+)\]", plan.filter_complex))
        self.assertEqual(labels.pop("0:v:0"), 1)
        self.assertEqual(labels.pop(plan.output_label), 1)
        self.assertTrue(all(count == 2 for count in labels.values()), labels)

    def test_custom_labels_do_not_change_timing(self):
        plan = self.build(input_label="selected_video", label_prefix="short2")
        self.assertTrue(plan.filter_complex.startswith("[selected_video]"))
        self.assertEqual(plan.output_label, "short2_video")
        self.assertNotIn("[framing_", plan.filter_complex)

    def test_bad_labels_subtitle_fragment_and_sigma_rejected(self):
        cases = [{"input_label": "0:a:0"}, {"input_label": "[0:v:0]"},
                 {"input_label": "framing_src0"}, {"label_prefix": "bad;split"},
                 {"subtitles_filter": "null"}, {"subtitles_filter": ""},
                 *({"blur_sigma": value} for value in (True, "20", 0, -1, 1025, 10 ** 1000, float("nan"), float("inf")))]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.build(**kwargs)

    def test_invalid_source_sizes_and_sar_rejected(self):
        for size in ((0, 1080), (1920, -1), (True, 1080), (1920.0, 1080)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                build_shorts_filtergraph(candidate(), *size)
        for sar in (None, "1:1", (0, 1), (1, 0), (1, -1), (True, 1), (1.0, 1)):
            with self.subTest(sar=sar), self.assertRaises(ValueError):
                self.build(source_sar=sar)

    def test_invalid_tiny_crop_and_odd_output_rejected(self):
        from unittest.mock import Mock
        with self.assertRaises(ValueError):
            build_shorts_filtergraph(candidate(), 2, 2)
        export = Mock()
        export.output_size.return_value = (721, 1280)
        with self.assertRaises(ValueError):
            self.build(export_settings=export)

    def test_no_io_processes_or_input_mutations_and_deterministic_result(self):
        clip, settings = candidate(), smart_settings()
        before = copy.deepcopy((clip, settings))
        with patch("subprocess.run", side_effect=AssertionError("no process")), \
                patch("subprocess.Popen", side_effect=AssertionError("no process")), \
                patch("pathlib.Path.read_text", side_effect=AssertionError("no file reads")), \
                patch("pathlib.Path.write_text", side_effect=AssertionError("no writes")):
            a = self.build(clip, smart_crop=settings)
            b = self.build(clip, smart_crop=settings)
        self.assertEqual(a, b)
        self.assertEqual((clip, settings), before)
        with self.assertRaises(FrozenInstanceError):
            a.output_width = 1


if __name__ == "__main__":
    unittest.main()
