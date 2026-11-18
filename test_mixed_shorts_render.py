"""Renderer wiring tests using temporary files and mocked media processes."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import renderer
from job_store import load_json
from shorts_settings import ShortsSettings
from smart_crop_storage import create_settings, save_settings, settings_path
from subtitle_settings import SubtitleSettings


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
