"""Renderer wiring tests using temporary files and mocked media processes."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import renderer
from job_store import load_json
from shorts_settings import ShortsSettings
from smart_crop_storage import create_settings, save_settings, settings_path
from subtitle_settings import DEFAULTS, SubtitleSettings, build_ass, document


def mixed_candidate():
    result = {"candidate_id": "sc001", "start": 1.25, "end": 5.25,
              "title": "Mixed test", "status": "approved", "format": "short",
              "render_settings": {"subtitles_enabled": True, "framing": {
                  "schema_version": 1, "default_mode": "CROP", "intervals": [
                      {"start": 2.5, "end": 3, "mode": "FIT_BLUR"},
                      {"start": 2, "end": 2.5, "mode": "FIT_BLUR"}]}},
              "transcript_edits": [{"sentence_index": 0, "start": 2, "end": 5, "text": "corrected caption"}]}
    result["approval_key"] = renderer.shorts_approval_key(result)
    return result


class MixedRenderWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name) / "Папка с пробелами"
        self.job.mkdir()
        self.clip = mixed_candidate()
        self.report = {"signature": "test", "profiles": {"shorts": {
            "min_duration_sec": 1, "max_duration_sec": 10}}, "shorts_candidates": [self.clip]}
        self.manifest = {"source_path": "source.mp4"}
        self.media = {"duration_sec": 8, "width": 640, "height": 360, "sample_aspect_ratio": "1:1"}
        self.export = ShortsSettings(resolution="720p", compression="compact")
        self.transcript = {"sentences": [{"start": 2, "end": 5, "text": "original caption"}],
                           "segments": [{"start": 2, "end": 5, "words": [
                               {"word": "original", "start": 2, "end": 3},
                               {"word": "caption", "start": 3, "end": 5}]}]}
        self.commands = []
        self.subtitle_files = []
        self.output_has_audio = True

    def reapprove(self):
        self.clip["approval_key"] = renderer.shorts_approval_key(self.clip)

    def fake_run(self, command, **kwargs):
        self.commands.append(command)
        self.subtitle_files.extend([(p, p.read_text(encoding="utf-8-sig")) for p in self.job.rglob("subs_*")])
        Path(command[-1]).write_bytes(b"mock MP4")
        return Mock(returncode=0)

    def render(self, **kwargs):
        width, height = self.export.output_size(self.media["width"], self.media["height"])
        with patch("renderer.probe", return_value=self.media), \
                patch("renderer._probe_basic", return_value={"duration_sec": 4, "width": width,
                      "height": height, "has_audio": self.output_has_audio}), \
                patch("renderer.subprocess.run", side_effect=self.fake_run):
            return renderer.render_shorts_approved(self.manifest, self.report, self.job, lambda _: None,
                transcript=self.transcript, export_settings=self.export, **kwargs)

    def test_complex_mapping_audio_duration_and_export_options(self):
        before = copy.deepcopy(self.report)
        paths = self.render()
        cmd = self.commands[0]
        self.assertNotIn("-vf", cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "[framing_video]")
        self.assertEqual([cmd[i + 1] for i, value in enumerate(cmd) if value == "-map"],
                         ["[framing_video]", "0:a:0"])
        for option, value in (("-ss", "1.25"), ("-t", "4.0"), ("-c:a", "aac"),
                              ("-b:a", "192k"), ("-fps_mode", "passthrough"),
                              ("-c:v", "libx264"), ("-crf", "28"), ("-preset", "slow")):
            self.assertEqual(cmd[cmd.index(option) + 1], value)
        for forbidden in ("-copyts", "-start_at_zero", "-shortest", "-af"):
            self.assertNotIn(forbidden, cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("setpts", graph)
        self.assertIn("gte(t,2)*lt(t,3)", graph)
        self.assertTrue(Path(paths[0]).exists())
        self.assertEqual(self.report, before)

    def test_metadata_contains_applied_normalized_framing(self):
        path = Path(self.render()[0])
        metadata = load_json(path.with_suffix(".json"))
        self.assertEqual(metadata["framing"], {"schema_version": 1, "default_mode": "CROP", "intervals": [
            {"start": 2, "end": 3, "mode": "FIT_BLUR"}]})
        self.assertEqual(metadata["approved_candidate"], self.clip)
        self.assertEqual(metadata["export_settings"]["output_width"], 720)
        self.assertEqual(metadata["export_settings"]["crf"], 28)
        self.assertTrue(metadata["media"]["has_audio"])

    def test_subtitles_windows_path_and_edits_used_once_after_composition(self):
        self.render()
        cmd = self.commands[0]
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(graph.count("subtitles="), 1)
        self.assertIn("[framing_composed]subtitles=", graph)
        self.assertEqual(len(self.subtitle_files), 1)
        path, content = self.subtitle_files[0]
        self.assertIn(renderer._filtergraph_escape_path(path), graph)
        self.assertIn("corrected caption", content)
        self.assertNotIn("original", content)
        self.assertIn("00:00:00,750", content)  # 2.0 - input seek 1.25
        self.assertIn(renderer.LEGACY_STYLE, graph)
        self.assertFalse(path.exists())

    def test_custom_subtitle_style_uses_ass(self):
        # A supported style differing from the historical default.
        custom = SubtitleSettings(font_size=42)
        self.render(subtitle_settings=custom)
        path, content = self.subtitle_files[0]
        self.assertEqual(path.suffix, ".ass")
        self.assertIn("[Script Info]", content)
        self.assertNotIn("force_style=", self.commands[0][self.commands[0].index("-filter_complex") + 1])

    def test_historical_legacy_and_vector_paths_preserve_original_settings(self):
        inactive = replace(DEFAULTS, background_geometry_mode="custom", background_fill_region=True,
                           background_padding_x_px=37, background_padding_y_px=4, background_corner_radius_px=40,
                           background_color="#123456", background_opacity=42)
        legacy = replace(DEFAULTS, background_enabled=True)
        text = replace(legacy, background_geometry_mode="custom", background_padding_x_px=37,
                       background_padding_y_px=4, background_corner_radius_px=40)
        full = replace(text, position_mode="free", area_enabled=True, background_fill_region=True)
        srt, cues = renderer.build_subtitles_srt(renderer.apply_transcript_edits(self.transcript, self.clip["transcript_edits"]),
                                               self.clip["start"], self.clip["end"], return_cues=True)
        for style, path_type in ((DEFAULTS, "srt"), (inactive, "srt"), (legacy, "legacy"),
                                 (text, "text"), (full, "full")):
            with self.subTest(path=path_type):
                self.commands.clear()
                self.subtitle_files.clear()
                before = document(style)
                with patch("renderer.prepare_ass", wraps=renderer.prepare_ass) as prepare, \
                        patch("renderer.MeasurementCache", wraps=renderer.MeasurementCache) as cache, \
                        patch("subtitle_background._measure_text", return_value=(200, 300, 400, 500)) as measure:
                    output = self.render(subtitle_settings=style)
                path, content = self.subtitle_files[0]
                graph = self.commands[0][self.commands[0].index("-filter_complex")+1]
                self.assertEqual(graph.count("subtitles="), 1)
                if path_type == "srt":
                    self.assertEqual(path.suffix, ".srt")
                    self.assertEqual(content, srt)
                    self.assertIn(renderer.LEGACY_STYLE, graph)
                    prepare.assert_not_called()
                else:
                    prepare.assert_called_once()
                    self.assertEqual(prepare.call_args.args[2:4], (720, 1280))
                    self.assertEqual(path.suffix, ".ass")
                    self.assertNotIn("force_style=", graph)
                    if path_type == "legacy":
                        self.assertEqual(content, build_ass(srt, style, cues))
                    else:
                        shape = next(line for line in content.splitlines() if line.startswith("Dialogue: 0"))
                        self.assertIn(r"\p7", shape)
                        self.assertEqual(" b " in shape, path_type == "full")
                        self.assertIn("0:00:00.75,0:00:03.75", shape)
                self.assertEqual(measure.call_count, 1 if path_type == "text" else 0)
                self.assertEqual(cache.call_count, 1 if path_type == "text" else 0)
                self.assertEqual(document(style), before)
                self.assertEqual(load_json(Path(output[0]).with_suffix(".json"))["subtitles"]["style"], before)
                self.assertFalse(path.exists())

    def test_vector_preparation_receives_actual_resolution_for_every_layout(self):
        full = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                       area_enabled=True, position_mode="free", background_fill_region=True)
        for resolution in ("720p", "1080p", "original"):
            self.export = ShortsSettings(resolution=resolution)
            expected = self.export.output_size(640, 360)
            for layout in ("ordinary", "CROP", "FIT_BLUR", "mixed", "smart", "smart_mixed"):
                with self.subTest(resolution=resolution, layout=layout):
                    self.commands.clear()
                    self.subtitle_files.clear()
                    self.clip["render_settings"].pop("framing", None)
                    if layout != "ordinary" and layout != "smart":
                        interval = {"start": 0, "end": 4, "mode": "CROP" if layout == "CROP" else "FIT_BLUR"}
                        if layout in ("mixed", "smart_mixed"):
                            interval.update(start=1, end=3)
                        self.clip["render_settings"]["framing"] = {"schema_version": 1, "default_mode": "CROP",
                                                                  "intervals": [interval]}
                    self.reapprove()
                    smart = None
                    if layout.startswith("smart"):
                        smart = settings_path(self.job, "test", "sc001")
                        save_settings(create_settings("sc001", 1.25, 5.25, 640, 360), smart)
                    try:
                        with patch("renderer.prepare_ass", wraps=renderer.prepare_ass) as prepare, \
                                patch("subtitle_background._measure_text", side_effect=AssertionError("No measurement")):
                            self.render(subtitle_settings=full)
                        self.assertEqual(prepare.call_args.args[2:4], expected)
                        command = self.commands[0]
                        option = "-filter_complex" if layout in ("FIT_BLUR", "mixed", "smart_mixed") else "-vf"
                        graph = command[command.index(option)+1]
                        self.assertEqual(graph.count("subtitles="), 1)
                        if option == "-filter_complex":
                            self.assertIn("[framing_composed]subtitles=", graph)
                        else:
                            self.assertIn(f"scale={expected[0]}:{expected[1]},subtitles=", graph)
                    finally:
                        if smart is not None:
                            smart.unlink()

    def test_repeated_cues_share_existing_measurement_cache_across_export_batch(self):
        second = copy.deepcopy(self.clip)
        second["candidate_id"] = "sc002"
        second["approval_key"] = renderer.shorts_approval_key(second)
        self.report["shorts_candidates"].append(second)
        text = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom")
        with patch("subtitle_background._measure_text", return_value=(100, 200, 300, 400)) as measure, \
                patch("renderer.prepare_ass", wraps=renderer.prepare_ass) as prepare, \
                patch("renderer.MeasurementCache", wraps=renderer.MeasurementCache) as cache:
            output = self.render(subtitle_settings=text)
        self.assertEqual(len(output), 2)
        self.assertEqual(prepare.call_count, 2)
        self.assertIs(prepare.call_args_list[0].kwargs["cache"], prepare.call_args_list[1].kwargs["cache"])
        measure.assert_called_once()
        cache.assert_called_once()

    def test_preparation_failure_is_not_replaced_with_legacy_and_cleans_ass(self):
        text = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom")
        with patch("subtitle_background._measure_text", side_effect=ValueError("measurement failure")):
            with self.assertRaisesRegex(ValueError, "Подготовка субтитров sc001.*measurement failure"):
                self.render(subtitle_settings=text)
        self.assertFalse(self.commands)
        self.assertFalse(list(self.job.rglob("subs_*")))
        self.assertFalse(list(self.job.rglob("*.mp4")))

    def test_diagnostic_failure_removes_diagnostic_directory_and_export_ass(self):
        original = tempfile.TemporaryDirectory
        directories = []

        def diagnostic_directory(*args, **kwargs):
            directory = original(*args, **kwargs)
            directories.append(Path(directory.name))
            return directory

        text = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom")
        with patch("renderer.probe", return_value=self.media), \
                patch("subtitle_background.tempfile.TemporaryDirectory", side_effect=diagnostic_directory), \
                patch("renderer.subprocess.run", return_value=Mock(returncode=1, stdout=b"", stderr=b"libass failure")):
            with self.assertRaisesRegex(ValueError, "sc001.*libass failure"):
                renderer.render_shorts_approved(self.manifest, self.report, self.job, lambda _: None,
                                               transcript=self.transcript, subtitle_settings=text)
        self.assertEqual(len(directories), 1)
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertFalse(list(self.job.rglob("subs_*")))
        self.assertFalse(list(self.job.rglob("*.mp4")))

    def test_ffmpeg_failure_removes_partial_mp4_and_ass(self):
        full = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom", background_fill_region=True,
                       area_enabled=True, position_mode="free")

        def failure(command, **kwargs):
            self.fake_run(command, **kwargs)
            return Mock(returncode=1)

        with patch("renderer.probe", return_value=self.media), patch("renderer.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "FFmpeg.*sc001"):
                renderer.render_shorts_approved(self.manifest, self.report, self.job, lambda _: None,
                                               transcript=self.transcript, subtitle_settings=full)
        self.assertFalse(list(self.job.rglob("subs_*")))
        self.assertFalse(list(self.job.rglob("*.mp4")))

    def test_disabled_or_empty_subtitles_omit_filter_and_tempfile(self):
        for disabled in (True, False):
            with self.subTest(disabled=disabled):
                self.commands.clear()
                self.subtitle_files.clear()
                self.clip["render_settings"]["subtitles_enabled"] = not disabled
                if not disabled:
                    self.transcript = {"sentences": [], "segments": []}
                    self.clip.pop("transcript_edits")
                self.reapprove()
                self.render()
                graph = self.commands[0][self.commands[0].index("-filter_complex") + 1]
                self.assertNotIn("subtitles=", graph)
                self.assertFalse(self.subtitle_files)

    def test_smart_crop_document_is_passed_and_dynamic_crop_is_not_called(self):
        settings = create_settings("sc001", 1.25, 5.25, 640, 360, [
            {"start": 0, "end": 1.5, "crop": {"x": 0, "y": 0, "width": .25, "height": 1}},
            {"start": 1.5, "end": 4, "crop": {"x": .5, "y": .25, "width": .125, "height": .5}}])
        save_settings(settings, settings_path(self.job, "test", "sc001"))
        with patch("renderer.build_shorts_filtergraph", wraps=renderer.build_shorts_filtergraph) as builder, \
                patch("renderer._crop_switch_exprs", side_effect=AssertionError("dynamic crop forbidden")):
            output = self.render()
        self.assertEqual(builder.call_args.kwargs["smart_crop"], settings)
        graph = self.commands[0][self.commands[0].index("-filter_complex") + 1]
        self.assertIn("split=4", graph)
        self.assertIn("gte(t,1.5)*lt(t,4)", graph)
        self.assertTrue(load_json(Path(output[0]).with_suffix(".json"))["crop"]["smart_crop"])

    def test_actual_sar_is_passed_to_generator(self):
        self.media["sample_aspect_ratio"] = "4:3"
        with patch("renderer.build_shorts_filtergraph", wraps=renderer.build_shorts_filtergraph) as builder:
            self.render()
        self.assertEqual(builder.call_args.kwargs["source_sar"], (4, 3))
        self.assertEqual(builder.call_args.kwargs["export_settings"], self.export)

    def test_sar_unknown_fallback_and_invalid_values(self):
        for sar in (None, "", "N/A", "0:1"):
            self.assertEqual(renderer._shorts_source_sar({"sample_aspect_ratio": sar}), (1, 1))
        for sar in ("1:0", "-1:1", "bad", "1:1:1", "1.2:1", 1):
            with self.subTest(sar=sar), self.assertRaises(ValueError):
                renderer._shorts_source_sar({"sample_aspect_ratio": sar})

    def test_legacy_crop_command_and_metadata_unchanged(self):
        self.clip["render_settings"].pop("framing")
        self.clip["render_settings"]["subtitles_enabled"] = False
        self.reapprove()
        with patch("renderer.build_shorts_filtergraph", side_effect=AssertionError("keep legacy path")):
            output = self.render()
        cmd = self.commands[0]
        expected = ["ffmpeg", "-v", "error", "-y", "-ss", "1.25", "-i", "source.mp4", "-t", "4.0",
                    "-map", "0:v:0", "-map", "0:a:0", "-vf", "crop=202:360:219:0,scale=720:1280",
                    "-c:v", "libx264", "-crf", "28", "-preset", "slow", "-fps_mode", "passthrough",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", cmd[-1]]
        self.assertEqual(cmd, expected)
        self.assertNotIn("framing", load_json(Path(output[0]).with_suffix(".json")))

    def test_explicit_empty_framing_keeps_legacy_command(self):
        self.clip["render_settings"]["framing"]["intervals"] = []
        self.reapprove()
        with patch("renderer.build_shorts_filtergraph", side_effect=AssertionError("keep legacy path")):
            self.render()
        self.assertIn("-vf", self.commands[0])

    def test_explicit_crop_framing_metadata_is_canonical_without_complex_graph(self):
        self.clip["render_settings"]["framing"]["intervals"] = [
            {"start": 0, "end": 4, "mode": "CROP"}]
        self.reapprove()
        output = self.render()
        self.assertIn("-vf", self.commands[0])
        self.assertEqual(load_json(Path(output[0]).with_suffix(".json"))["framing"],
                         {"schema_version": 1, "default_mode": "CROP", "intervals": []})

    def test_missing_output_audio_rejects_mixed_result(self):
        self.output_has_audio = False
        with self.assertRaisesRegex(RuntimeError, "без аудиопотока"):
            self.render()
        self.assertFalse([path for path in self.job.rglob("*.mp4") if ".partial." not in path.name])
        self.assertFalse(list(self.job.rglob("subs_*")))
        self.assertFalse(list(self.job.rglob("*.partial.mp4")))

    def test_stale_framing_and_changed_approval_rejected_before_ffmpeg(self):
        original = copy.deepcopy(self.clip)
        for stale_anchor in (True, False):
            self.clip.clear()
            self.clip.update(copy.deepcopy(original))
            if stale_anchor:
                self.clip["render_settings"]["framing_source_bounds"] = {"start": .25, "end": 4.25}
            else:
                self.clip["render_settings"]["framing"]["intervals"][0]["end"] = 3.5
            with self.subTest(stale_anchor=stale_anchor), self.assertRaises(ValueError):
                self.render()
        self.assertFalse(self.commands)

    def test_stale_smart_crop_file_is_not_used_or_changed(self):
        settings = create_settings("sc001", .25, 4.25, 640, 360)
        path = settings_path(self.job, "test", "sc001")
        save_settings(settings, path)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.render()
        self.assertFalse(self.commands)
        self.assertEqual(path.read_bytes(), before)

    def test_graph_error_cleans_temporary_subtitle_file(self):
        with patch("renderer.build_shorts_filtergraph", side_effect=ValueError("graph error")):
            with self.assertRaises(ValueError):
                self.render()
        self.assertFalse(list(self.job.rglob("subs_*")))
        self.assertFalse(self.commands)

    def test_selected_candidate_only_and_preserve_existing_export(self):
        before = copy.deepcopy(self.report)
        first = Path(self.render()[0])
        contents = first.read_bytes()
        other = copy.deepcopy(self.clip)
        other["candidate_id"] = "sc002"
        other["approval_key"] = "invalid but not selected"
        self.report["shorts_candidates"].append(other)
        second = Path(self.render(candidate_id="sc001")[0])
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), contents)
        self.assertEqual(self.report["shorts_candidates"][0], before["shorts_candidates"][0])


if __name__ == "__main__":
    unittest.main()
