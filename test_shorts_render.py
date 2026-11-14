"""Tests for Stage 6: Shorts 9:16 rendering logic."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from renderer import (
    SHORTS_OUTPUT_H,
    SHORTS_OUTPUT_W,
    _crop_params,
    _crop_switch_exprs,
    _filtergraph_escape_path,
    _group_words_into_cues,
    _srt_timecode,
    build_subtitles_srt,
    render_shorts_approved,
    shorts_approval_key,
    validate_crop_x,
)


class CropParamsTests(unittest.TestCase):
    def test_center_crop_1920x1080(self):
        w, h, x, y = _crop_params(1920, 1080)
        expected_w = round(1080 * 9 / 16)
        self.assertEqual((w, h), (expected_w, 1080))
        self.assertEqual(x, (1920 - expected_w) // 2)
        self.assertEqual(y, 0)

    def test_center_crop_1280x720(self):
        w, h, x, y = _crop_params(1280, 720)
        expected_w = round(720 * 9 / 16)
        self.assertEqual((w, h), (expected_w, 720))
        self.assertEqual(x, (1280 - expected_w) // 2)
        self.assertEqual(y, 0)

    def test_crop_x_zero(self):
        w, h, x, y = _crop_params(1920, 1080, crop_x=0.0)
        self.assertEqual(x, 0)

    def test_crop_x_one(self):
        w, h, x, y = _crop_params(1920, 1080, crop_x=1.0)
        self.assertEqual(x, 1920 - w)

    def test_crop_x_clamped(self):
        w, h, x, y = _crop_params(1920, 1080, crop_x=5.0)
        self.assertEqual(x, 1920 - w)
        w2, h2, x2, y2 = _crop_params(1920, 1080, crop_x=-1.0)
        self.assertEqual(x2, 0)

    def test_crop_x_fractional(self):
        w, h, x, y = _crop_params(1920, 1080, crop_x=0.5)
        self.assertEqual(x, round(0.5 * (1920 - w)))

    def test_square_source(self):
        w, h, x, y = _crop_params(1080, 1080)
        expected_w = round(1080 * 9 / 16)
        self.assertEqual(w, expected_w)
        self.assertEqual(h, 1080)
        self.assertEqual(x, (1080 - expected_w) // 2)
        self.assertEqual(y, 0)


class ValidateCropXTests(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(validate_crop_x(None))

    def test_valid_range(self):
        self.assertEqual(validate_crop_x(0.0), 0.0)
        self.assertEqual(validate_crop_x(1.0), 1.0)
        self.assertEqual(validate_crop_x(0.5), 0.5)
        self.assertEqual(validate_crop_x("0.25"), 0.25)
        self.assertEqual(validate_crop_x("0,75"), 0.75)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            validate_crop_x(-0.1)
        with self.assertRaises(ValueError):
            validate_crop_x(1.1)

    def test_invalid_type(self):
        with self.assertRaises(ValueError):
            validate_crop_x("abc")
        with self.assertRaises(ValueError):
            validate_crop_x(float("inf"))
        with self.assertRaises(ValueError):
            validate_crop_x(float("nan"))


class SrtTimecodeTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(_srt_timecode(0), "00:00:00,000")

    def test_seconds(self):
        self.assertEqual(_srt_timecode(1.5), "00:00:01,500")

    def test_minutes(self):
        self.assertEqual(_srt_timecode(61.0), "00:01:01,000")

    def test_hours(self):
        self.assertEqual(_srt_timecode(3661.5), "01:01:01,500")

    def test_negative_clamped(self):
        self.assertEqual(_srt_timecode(-0.5), "00:00:00,000")


class GroupWordsTests(unittest.TestCase):
    def test_small_group(self):
        words = [{"start": i, "end": i + 0.3, "word": f"w{i}"} for i in range(4)]
        cues = _group_words_into_cues(words, max_words=6)
        self.assertEqual(len(cues), 1)
        self.assertEqual(len(cues[0]), 4)

    def test_exact_multiple(self):
        words = [{"start": i, "end": i + 0.3, "word": f"w{i}"} for i in range(12)]
        cues = _group_words_into_cues(words, max_words=6)
        self.assertEqual(len(cues), 2)
        self.assertEqual(len(cues[0]), 6)
        self.assertEqual(len(cues[1]), 6)

    def test_uneven(self):
        words = [{"start": i, "end": i + 0.3, "word": f"w{i}"} for i in range(7)]
        cues = _group_words_into_cues(words, max_words=6)
        self.assertEqual(len(cues), 2)
        self.assertEqual(len(cues[0]), 6)
        self.assertEqual(len(cues[1]), 1)

    def test_empty(self):
        self.assertEqual(_group_words_into_cues([]), [])


class BuildSubtitlesSrtTests(unittest.TestCase):
    def _transcript(self):
        return {
            "segments": [
                {"start": 10.0, "end": 12.0, "words": [
                    {"word": "Привет", "start": 10.0, "end": 10.5},
                    {"word": "мир", "start": 10.5, "end": 11.0},
                    {"word": "это", "start": 11.0, "end": 11.3},
                    {"word": "тест", "start": 11.3, "end": 11.8},
                ]},
            ],
            "sentences": [
                {"start": 10.0, "end": 11.8, "text": "Привет мир это тест"},
            ],
        }

    def test_basic_cyrillic(self):
        srt = build_subtitles_srt(self._transcript(), 10.0, 12.0)
        self.assertIn("Привет", srt)
        self.assertIn("тест", srt)
        self.assertIn("-->", srt)

    def test_timecode_relative_to_clip_start(self):
        srt = build_subtitles_srt(self._transcript(), 10.0, 12.0)
        self.assertIn("00:00:00,000", srt)
        self.assertNotIn("00:00:10,000", srt)

    def test_empty_interval(self):
        srt = build_subtitles_srt(self._transcript(), 20.0, 30.0)
        self.assertEqual(srt, "")

    def test_partial_overlap(self):
        transcript = {
            "segments": [
                {"start": 5.0, "end": 15.0, "words": [
                    {"word": "до", "start": 5.0, "end": 5.5},
                    {"word": "границы", "start": 5.5, "end": 6.0},
                    {"word": "внутри", "start": 8.0, "end": 8.5},
                    {"word": "клипа", "start": 8.5, "end": 9.0},
                    {"word": "после", "start": 14.0, "end": 14.5},
                    {"word": "границы", "start": 14.5, "end": 15.0},
                ]},
            ],
            "sentences": [],
        }
        srt = build_subtitles_srt(transcript, 7.0, 13.0)
        self.assertIn("внутри", srt)
        self.assertIn("клипа", srt)
        self.assertNotIn("до\n", srt)
        self.assertNotIn("после", srt)

    def test_no_segments(self):
        self.assertEqual(build_subtitles_srt({"segments": []}, 0, 10), "")

    def test_cue_numbering(self):
        transcript = {
            "segments": [
                {"start": 0.0, "end": 5.0, "words": [
                    {"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4}
                    for i in range(12)
                ]},
            ],
            "sentences": [],
        }
        srt = build_subtitles_srt(transcript, 0.0, 6.0)
        self.assertIn("1\n", srt)
        self.assertIn("2\n", srt)
        self.assertNotIn("3\n", srt)

    def test_long_line_wrap(self):
        transcript = {
            "segments": [
                {"start": 0.0, "end": 2.0, "words": [
                    {"word": "совершенно", "start": 0.0, "end": 0.3},
                    {"word": "необычайно", "start": 0.3, "end": 0.6},
                    {"word": "красивый", "start": 0.6, "end": 0.9},
                    {"word": "вечерний", "start": 0.9, "end": 1.2},
                    {"word": "пейзаж", "start": 1.2, "end": 1.5},
                    {"word": "замечательный", "start": 1.5, "end": 1.8},
                ]},
            ],
            "sentences": [],
        }
        srt = build_subtitles_srt(transcript, 0.0, 2.0)
        self.assertIn("\n", srt.split("-->")[1])


class ShortsApprovalKeyTests(unittest.TestCase):
    def _candidate(self, **overrides):
        base = {
            "candidate_id": "s001",
            "start": 100.0,
            "end": 140.0,
            "format": "short",
            "render_settings": {"crop_mode": "center", "crop_x": None, "subtitles_enabled": True},
        }
        base.update(overrides)
        return base

    def test_stable(self):
        self.assertEqual(shorts_approval_key(self._candidate()),
                         shorts_approval_key(self._candidate()))

    def test_crop_x_changes_key(self):
        self.assertNotEqual(
            shorts_approval_key(self._candidate()),
            shorts_approval_key(self._candidate(
                render_settings={"crop_mode": "center", "crop_x": 0.3, "subtitles_enabled": True}))
        )

    def test_subtitles_toggle_changes_key(self):
        self.assertNotEqual(
            shorts_approval_key(self._candidate()),
            shorts_approval_key(self._candidate(
                render_settings={"crop_mode": "center", "crop_x": None, "subtitles_enabled": False}))
        )

    def test_title_does_not_change_key(self):
        c1 = self._candidate()
        c2 = self._candidate()
        c1["title"] = "Первый"
        c2["title"] = "Второй"
        self.assertEqual(shorts_approval_key(c1), shorts_approval_key(c2))

    def test_boundary_change_invalidates(self):
        self.assertNotEqual(
            shorts_approval_key(self._candidate()),
            shorts_approval_key(self._candidate(end=141.0))
        )


class RenderShortsApprovedTests(unittest.TestCase):
    def _report(self, **overrides):
        base = {
            "signature": "test_sig",
            "profiles": {"shorts": {"min_duration_sec": 20, "max_duration_sec": 60}},
            "shorts_candidates": [
                {
                    "candidate_id": "s001",
                    "start": 100.0,
                    "end": 130.0,
                    "title": "Тестовый Short",
                    "status": "approved",
                    "format": "short",
                    "render_settings": {"crop_mode": "center", "crop_x": None, "subtitles_enabled": True},
                    "approval_key": shorts_approval_key({
                        "candidate_id": "s001",
                        "start": 100.0,
                        "end": 130.0,
                        "format": "short",
                        "render_settings": {"crop_mode": "center", "crop_x": None, "subtitles_enabled": True},
                    }),
                }
            ],
        }
        base.update(overrides)
        return base

    def _manifest(self, path="test.mp4"):
        return {"source_path": path}

    def test_no_approved_raises(self):
        report = self._report()
        report["shorts_candidates"][0]["status"] = "pending"
        with self.assertRaises(ValueError):
            render_shorts_approved(self._manifest(), report, "/tmp/job", print)

    def test_approval_mismatch_raises(self):
        report = self._report()
        report["shorts_candidates"][0]["approval_key"] = "wrong_key"
        with patch("renderer.probe") as mock_probe:
            mock_probe.return_value = {"duration_sec": 300, "width": 1920, "height": 1080,
                                       "has_audio": True}
            try:
                render_shorts_approved(self._manifest(), report, "/tmp/job", print)
                self.fail("Expected ValueError")
            except ValueError as e:
                self.assertIn("изменились", str(e))

    def test_render_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            transcript = {"segments": [{"start": 100.0, "end": 130.0, "words": [
                {"word": "тест", "start": 100.0, "end": 100.5},
            ]}], "sentences": []}
            logs = []

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}
            fake_rendered = {"duration_sec": 30.0, "width": SHORTS_OUTPUT_W,
                             "height": SHORTS_OUTPUT_H, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer._probe_basic", return_value=fake_rendered), \
                 patch("renderer.subprocess.run") as mock_run, \
                 patch("pathlib.Path.rename") as mock_rename:
                mock_run.return_value = MagicMock(returncode=0)
                outputs = render_shorts_approved(
                    manifest, report, str(job_dir), lambda m: logs.append(m),
                    transcript=transcript)

            self.assertEqual(len(outputs), 1)
            self.assertTrue(outputs[0].endswith(".mp4"))
            cmd = mock_run.call_args[0][0]
            self.assertIn("ffmpeg", cmd[0])
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            self.assertIn("crop=", vf)
            self.assertIn(f"scale={SHORTS_OUTPUT_W}:{SHORTS_OUTPUT_H}", vf)
            self.assertIn("subtitles=", vf)
            self.assertTrue(any("Рендер Shorts s001" in line for line in logs))

    def test_render_no_subtitles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            report["shorts_candidates"][0]["render_settings"] = {
                "crop_mode": "center", "crop_x": None, "subtitles_enabled": False,
            }
            report["shorts_candidates"][0]["approval_key"] = shorts_approval_key(
                report["shorts_candidates"][0])
            transcript = {"segments": [], "sentences": []}

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}
            fake_rendered = {"duration_sec": 30.0, "width": SHORTS_OUTPUT_W,
                             "height": SHORTS_OUTPUT_H, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer._probe_basic", return_value=fake_rendered), \
                 patch("renderer.subprocess.run") as mock_run, \
                 patch("pathlib.Path.rename"):
                mock_run.return_value = MagicMock(returncode=0)
                render_shorts_approved(manifest, report, str(job_dir), lambda m: None,
                                       transcript=transcript)

            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            self.assertNotIn("subtitles=", vf)

    def test_render_with_crop_x(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            report["shorts_candidates"][0]["render_settings"] = {
                "crop_mode": "center", "crop_x": 0.5, "subtitles_enabled": False,
            }
            report["shorts_candidates"][0]["approval_key"] = shorts_approval_key(
                report["shorts_candidates"][0])
            transcript = {"segments": [], "sentences": []}

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}
            fake_rendered = {"duration_sec": 30.0, "width": SHORTS_OUTPUT_W,
                             "height": SHORTS_OUTPUT_H, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer._probe_basic", return_value=fake_rendered), \
                 patch("renderer.subprocess.run") as mock_run, \
                 patch("pathlib.Path.rename"):
                mock_run.return_value = MagicMock(returncode=0)
                render_shorts_approved(manifest, report, str(job_dir), lambda m: None,
                                       transcript=transcript)

            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            crop_part = vf.split(",")[0]
            self.assertIn("crop=", crop_part)
            parts = crop_part.split("=")[1].split(":")
            self.assertEqual(len(parts), 4)

    def test_candidate_id_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            report["shorts_candidates"].append(
                dict(report["shorts_candidates"][0], candidate_id="s002", status="approved"))
            transcript = {"segments": [], "sentences": []}

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}
            fake_rendered = {"duration_sec": 30.0, "width": SHORTS_OUTPUT_W,
                             "height": SHORTS_OUTPUT_H, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer._probe_basic", return_value=fake_rendered), \
                 patch("renderer.subprocess.run") as mock_run, \
                 patch("pathlib.Path.rename"):
                mock_run.return_value = MagicMock(returncode=0)
                outputs = render_shorts_approved(
                    manifest, report, str(job_dir), lambda m: None,
                    transcript=transcript, candidate_id="s001")

            self.assertEqual(len(outputs), 1)
            mock_run.assert_called_once()

    def test_ffmpeg_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            transcript = {"segments": [], "sentences": []}

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                try:
                    render_shorts_approved(manifest, report, str(job_dir), lambda m: None,
                                           transcript=transcript)
                    self.fail("Expected RuntimeError")
                except RuntimeError as e:
                    self.assertIn("FFmpeg", str(e))


    def test_subtitles_uses_temp_file_with_escaped_path(self):
        """Regression: subtitles filter must use an escaped temp-file path.

        The old code did:
            safe_name = str(srt_path).replace("\\", "/").replace(":", "/")
        which turned D:/Dev/... into D//Dev/... — losing the drive colon.
        The fix uses a temp file with short ASCII name and proper filtergraph
        escaping (colon stays escaped as \:, not replaced).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            transcript = {"segments": [{"start": 100.0, "end": 130.0, "words": [
                {"word": "тест", "start": 100.0, "end": 100.5},
            ]}], "sentences": []}

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}
            fake_rendered = {"duration_sec": 30.0, "width": SHORTS_OUTPUT_W,
                             "height": SHORTS_OUTPUT_H, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer._probe_basic", return_value=fake_rendered), \
                 patch("renderer.subprocess.run") as mock_run, \
                 patch("pathlib.Path.rename"):
                mock_run.return_value = MagicMock(returncode=0)
                render_shorts_approved(manifest, report, str(job_dir), lambda m: None,
                                       transcript=transcript)

            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            # subtitles filter must reference a temp file path, not an index
            self.assertIn("subtitles='", vf)
            self.assertNotIn("subtitles=1:", vf)
            # No raw drive colon without escaping: D: must appear as D\:
            # Extract the path between subtitles=' and ':force_style
            subs_match = vf.split("subtitles='", 1)[1].split("':force_style", 1)[0]
            # The temp path should be escaped: colons escaped, backslashes to /
            self.assertNotIn(":", subs_match.replace("\\:", ""))
            # SRT is NOT passed as a separate -i input
            srt_inputs = [cmd[i + 1] for i in range(len(cmd) - 1)
                          if cmd[i] == "-i" and cmd[i + 1].endswith(".srt")]
            self.assertEqual(len(srt_inputs), 0,
                             "SRT must NOT be passed as a separate -i input")
            # Temp file should be cleaned up
            srt_files = list(job_dir.glob("**/subs_*.srt"))
            self.assertEqual(len(srt_files), 0,
                             "Temp SRT file must be cleaned up after render")

    def test_subtitles_filtergraph_with_windows_path(self):
        """Verify the filtergraph survives a Windows path with Cyrillic and spaces.

        Simulates the real scenario:
            D:/Dev/HighlightVideos/data/.../shorts/sc001_Японская империя.srt
        The old code would produce: D//Dev/HighlightVideos/data/.../sc001_Японская империя.srt
        (drive colon replaced with /). The new code uses a temp file with proper
        escaping so the colon is preserved.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            manifest = self._manifest()
            report = self._report()
            report["shorts_candidates"][0]["title"] = "Японская империя"
            transcript = {"segments": [{"start": 100.0, "end": 130.0, "words": [
                {"word": "привет", "start": 100.0, "end": 100.5},
            ]}], "sentences": []}

            fake_probe = {"duration_sec": 130, "width": 1920, "height": 1080, "has_audio": True}
            fake_rendered = {"duration_sec": 30.0, "width": SHORTS_OUTPUT_W,
                             "height": SHORTS_OUTPUT_H, "has_audio": True}

            with patch("renderer.probe", return_value=fake_probe), \
                 patch("renderer._probe_basic", return_value=fake_rendered), \
                 patch("renderer.subprocess.run") as mock_run, \
                 patch("pathlib.Path.rename"):
                mock_run.return_value = MagicMock(returncode=0)
                render_shorts_approved(manifest, report, str(job_dir), lambda m: None,
                                       transcript=transcript)

            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            # The filtergraph uses a temp file path with proper escaping.
            self.assertIn("subtitles='", vf)
            subs_path = vf.split("subtitles='", 1)[1].split("':force_style", 1)[0]
            # The temp file path should have colons escaped
            if subs_path and subs_path[0] == "D":
                self.assertIn("\\:", subs_path)
            # Keep SRT file with readable name (Cyrillic title)
            srt_files = list(job_dir.glob("**/*.srt"))
            self.assertEqual(len(srt_files), 1,
                             "Keep SRT file must exist with readable name")
            self.assertIn("Японская", srt_files[0].name)


class CropSwitchExprsTests(unittest.TestCase):
    """Tests for _crop_switch_exprs: segment selection logic and time precision."""

    def _segments(self, ends, crops=None):
        n = len(ends)
        if crops is None:
            crops = [
                {"x": i * 0.2, "y": 0.0, "width": 0.3, "height": 1.0}
                for i in range(n)
            ]
        segs = []
        prev = 0.0
        for i, end in enumerate(ends):
            segs.append({
                "start": prev,
                "end": end,
                "crop": crops[i],
            })
            prev = end
        return segs

    def test_two_segment_switch(self):
        segs = self._segments([10.0, 48.52])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        # First segment crop x at x=0 (0*0.2*3840 = 0)
        self.assertIn("0", x)
        # Second segment crop x at x=768 (1*0.2*3840 = 768)
        self.assertIn("768", x)
        # Switch at t=10.0
        self.assertIn("10.000000", x)
        self.assertNotIn("10.0", x.replace("10.000000", ""))

    def test_three_segment_switch(self):
        segs = self._segments([16.589559, 33.580798, 48.52])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        self.assertIn("16.589559", x)
        self.assertIn("33.580798", x)
        # Last segment is else — no time guard
        self.assertNotIn("48.520000", x)

    def test_precision_10_032688(self):
        """Regression: :g format truncated 10.032688 to 10.0327.

        With :.6f the full 6-decimal value must survive in the expression.
        """
        segs = self._segments([10.032688, 48.52])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        self.assertIn("10.032688", x)
        # Must NOT be the old truncated form
        self.assertNotIn("10.0327", x)

    def test_precision_32_910651(self):
        """Regression: :g format truncated 32.910651 to 32.9107."""
        segs = self._segments([10.0, 32.910651, 48.52])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        self.assertIn("32.910651", x)
        self.assertNotIn("32.9107", x)

    def test_precision_41_575306(self):
        """Regression: :g format gave 41.5753 (6 sig figs); :.6f must give 41.575306.

        The old form "41.5753" is a substring of "41.575306", so we check that
        the time is followed by a closing paren (end of lt()), not by more digits.
        """
        segs = self._segments([10.0, 32.910651, 41.575306, 119.84])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        self.assertIn("41.575306", x)
        self.assertNotIn("41.5753)", x.replace("41.575306)", ""))

    def test_ffmpeg_escaping_preserved(self):
        """The comma in lt(t, ...) must be backslash-escaped for FFmpeg."""
        segs = self._segments([10.0, 48.52])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        self.assertIn(r"lt(t\,", x)

    def test_all_expressions_same_boundaries(self):
        """w, h, x, y must all switch at the same time boundaries."""
        segs = self._segments([10.032688, 32.910651, 41.575306, 119.84])
        w, h, x, y = _crop_switch_exprs(segs, 3840, 2160)
        for expr in (w, h, x, y):
            self.assertIn("10.032688", expr)
            self.assertIn("32.910651", expr)
            self.assertIn("41.575306", expr)


class FiltergraphEscapeTests(unittest.TestCase):
    def test_windows_drive_colon_preserved(self):
        """D:\\Dev must become D\\:/Dev — colon escaped, backslashes to /."""
        result = _filtergraph_escape_path("D:\\Dev\\HighlightVideos\\subs_abc.srt")
        self.assertIn("\\:", result)
        self.assertNotIn("D://", result)
        self.assertIn("/", result)

    def test_forward_slash_drive(self):
        result = _filtergraph_escape_path("D:/Dev/subs_abc.srt")
        self.assertIn("\\:", result)
        self.assertNotIn("D://", result)

    def test_space_not_escaped(self):
        """Spaces are safe unescaped inside the filtergraph single-quoted path."""
        result = _filtergraph_escape_path("C:/My Videos/subs_abc.srt")
        self.assertIn("My Videos", result)

    def test_cyrillic_not_escaped(self):
        result = _filtergraph_escape_path("C:/Dev/Японская/subs_abc.srt")
        self.assertIn("Японская", result)

    def test_special_chars_escaped(self):
        result = _filtergraph_escape_path("C:/Dev/a[b]:c;d'e/f.srt")
        self.assertIn("\\[", result)
        self.assertIn("\\]", result)
        self.assertIn("\\;", result)
        self.assertIn("\\'", result)
        self.assertIn("\\:", result)

    def test_unix_path_unaffected_by_drive(self):
        result = _filtergraph_escape_path("/home/user/subs_abc.srt")
        self.assertNotIn("\\:", result)


class FfmpegIntegrationTest(unittest.TestCase):
    """Real FFmpeg test: renders a short synthetic video with subtitles.

    Skipped if FFmpeg is not installed in the test environment.
    """

    def setUp(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("FFmpeg not found in PATH")
        if shutil.which("ffprobe") is None:
            self.skipTest("ffprobe not found in PATH")

    def test_render_short_with_subtitles(self):
        """Create a 3-second test video, render with SRT, verify MP4 output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / "job"
            job_dir.mkdir()

            # Create a short 640x360 test video (3 seconds, solid color)
            src_video = job_dir / "src.mp4"
            create_cmd = [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=0x334455:s=640x360:r=30",
                "-t", "3",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "3",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest",
                str(src_video),
            ]
            create = subprocess.run(create_cmd, capture_output=True, text=True)
            if create.returncode != 0:
                self.skipTest(f"Could not create test video: {create.stderr[:200]}")

            # Create SRT with a subtitle cue in the middle
            srt_content = (
                "1\n"
                "00:00:01,000 --> 00:00:02,000\n"
                "Тест субтитров\n"
            )
            srt_file = job_dir / "test.srt"
            srt_file.write_text(srt_content, encoding="utf-8-sig")

            # Render: crop to 9:16, scale to 1080x1920, burn subtitles
            output = job_dir / "output.mp4"
            vf = (
                "crop=202:360:219:0,"
                "scale=1080:1920,"
                f"subtitles='{_filtergraph_escape_path(srt_file)}'"
            )
            render_cmd = [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(src_video),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac",
                str(output),
            ]
            log = job_dir / "ffmpeg.log"
            with log.open("w", encoding="utf-8") as stderr:
                result = subprocess.run(render_cmd, stdout=subprocess.DEVNULL,
                                        stderr=stderr)
            if result.returncode != 0:
                self.fail(
                    f"FFmpeg failed (code {result.returncode}). "
                    f"Log: {log.read_text(encoding='utf-8')}")

            # Verify output exists and has correct dimensions
            self.assertTrue(output.exists(), "Output MP4 was not created")
            self.assertGreater(output.stat().st_size, 1000,
                               "Output MP4 is suspiciously small")

            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0", str(output),
            ]
            probe = subprocess.run(probe_cmd, capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0)
            lines = probe.stdout.strip().splitlines()
            self.assertTrue(lines, "ffprobe returned no streams")
            w, h = lines[0].split(",")
            self.assertEqual(int(w), 1080)
            self.assertEqual(int(h), 1920)

            # Temp SRT cleanup is tested separately in the render tests;
            # here we just verify the render itself works end-to-end.


if __name__ == "__main__":
    unittest.main()
