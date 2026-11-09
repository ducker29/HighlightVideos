"""Tests for Stage 6: Shorts 9:16 rendering logic."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from renderer import (
    SHORTS_OUTPUT_H,
    SHORTS_OUTPUT_W,
    _crop_params,
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


if __name__ == "__main__":
    unittest.main()
