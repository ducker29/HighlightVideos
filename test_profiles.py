"""Профили выхода 16:9 / 9:16, независимые утверждения и миграция старых отчётов."""
import copy
import unittest

from job_store import (
    candidate_format,
    migrate_report,
    normalize_profiles,
    profile_for_format,
    report_profiles,
    validate_candidate_format,
)
from renderer import approval_key, shorts_approval_key
from app_backend import open_report
from job_store import load_json, save_json


class ProfileNormalizationTests(unittest.TestCase):
    def test_absent_outputs_give_legacy_16_9_and_disabled_shorts(self):
        profiles = normalize_profiles(None)
        self.assertEqual(profiles["highlights"], {
            "aspect_ratio": "16:9",
            "min_duration_sec": 120,
            "max_duration_sec": 300,
            "end_padding_sec": 0.5,
        })
        shorts = profiles["shorts"]
        self.assertFalse(shorts["enabled"])
        self.assertEqual(shorts["aspect_ratio"], "9:16")
        self.assertEqual(shorts["min_duration_sec"], 20)
        self.assertEqual(shorts["target_duration_sec"], 40)
        self.assertEqual(shorts["max_duration_sec"], 60)
        self.assertEqual(shorts["crop_mode"], "center")
        self.assertTrue(shorts["subtitles_enabled"])

    def test_outputs_section_overrides_defaults(self):
        raw = {
            "highlights": {"min_duration_sec": 60, "max_duration_sec": 120},
            "shorts": {"enabled": True, "max_duration_sec": 45, "crop_mode": "speaker"},
        }
        profiles = normalize_profiles(raw)
        self.assertEqual(profiles["highlights"]["min_duration_sec"], 60)
        self.assertEqual(profiles["highlights"]["max_duration_sec"], 120)
        self.assertTrue(profiles["shorts"]["enabled"])
        self.assertEqual(profiles["shorts"]["max_duration_sec"], 45)
        self.assertEqual(profiles["shorts"]["crop_mode"], "speaker")
        # Не тронутые значения сохраняются из дефолтов.
        self.assertEqual(profiles["highlights"]["aspect_ratio"], "16:9")
        self.assertEqual(profiles["shorts"]["aspect_ratio"], "9:16")

    def test_invalid_highlight_durations_rejected(self):
        with self.assertRaises(ValueError):
            normalize_profiles({"highlights": {"min_duration_sec": 301, "max_duration_sec": 300}})
        with self.assertRaises(ValueError):
            normalize_profiles({"highlights": {"min_duration_sec": 0, "max_duration_sec": 300}})
        with self.assertRaises(ValueError):
            normalize_profiles({"highlights": {"min_duration_sec": 120, "max_duration_sec": 500}})

    def test_shorts_must_stay_9_16(self):
        with self.assertRaises(ValueError):
            normalize_profiles({"shorts": {"aspect_ratio": "16:9"}})

    def test_shorts_target_within_range(self):
        with self.assertRaises(ValueError):
            normalize_profiles({"shorts": {"min_duration_sec": 20, "target_duration_sec": 70,
                                           "max_duration_sec": 60}})


class CandidateFormatTests(unittest.TestCase):
    def test_missing_format_means_highlight(self):
        self.assertEqual(candidate_format({}), "highlight")
        self.assertEqual(candidate_format({"format": "highlight"}), "highlight")
        self.assertEqual(candidate_format({"format": "short"}), "short")

    def test_unknown_format_rejected(self):
        with self.assertRaises(ValueError):
            candidate_format({"format": "wide"})

    def test_profile_for_disabled_shorts_raises(self):
        profiles = normalize_profiles(None)
        self.assertIs(profile_for_format(profiles, "highlight"), profiles["highlights"])
        with self.assertRaises(ValueError):
            profile_for_format(profiles, "short")

    def test_profile_for_enabled_shorts(self):
        profiles = normalize_profiles({"shorts": {"enabled": True}})
        self.assertEqual(profile_for_format(profiles, "short")["max_duration_sec"], 60)

    def test_validate_candidate_format_id_prefix_and_bounds(self):
        profiles = normalize_profiles({"shorts": {"enabled": True}})
        # Хайлайт: префикс c, длительность в пределах 120–300.
        self.assertEqual(
            validate_candidate_format(profiles, {"candidate_id": "c001", "start": 120, "end": 300}),
            "highlight")
        # Short: префикс s, длительность в пределах 20–60.
        self.assertEqual(
            validate_candidate_format(profiles, {"candidate_id": "s001", "start": 0, "end": 45,
                                                 "format": "short"}),
            "short")
        # Несоответствие префикса и формата.
        with self.assertRaises(ValueError):
            validate_candidate_format(profiles, {"candidate_id": "c001", "start": 0, "end": 45,
                                                  "format": "short"})
        with self.assertRaises(ValueError):
            validate_candidate_format(profiles, {"candidate_id": "s001", "start": 120, "end": 300})
        # Выход за пределы диапазона профиля.
        with self.assertRaises(ValueError):
            validate_candidate_format(profiles, {"candidate_id": "s001", "start": 0, "end": 70,
                                                  "format": "short"})
        with self.assertRaises(ValueError):
            validate_candidate_format(profiles, {"candidate_id": "c001", "start": 0, "end": 60})


class MigrationTests(unittest.TestCase):
    def test_legacy_report_gains_format_and_profiles(self):
        legacy = {
            "schema": 1,
            "video_id": "v",
            "settings": {},
            "candidates": [
                {"candidate_id": "c001", "start": 10, "end": 130, "title": "А", "status": "approved",
                 "approval_key": "x"},
                {"candidate_id": "c002", "start": 200, "end": 320, "title": "Б", "status": "pending"},
            ],
        }
        result = migrate_report(legacy)
        self.assertIs(result, legacy)
        for candidate in legacy["candidates"]:
            self.assertEqual(candidate["format"], "highlight")
        self.assertIn("profiles", legacy)
        self.assertEqual(legacy["profiles"]["highlights"]["aspect_ratio"], "16:9")
        # Границы и утверждения не тронуты.
        self.assertEqual(legacy["candidates"][0]["approval_key"], "x")
        self.assertEqual(legacy["candidates"][0]["start"], 10)

    def test_report_profiles_falls_back_to_settings(self):
        report = {"settings": {"outputs": {"shorts": {"enabled": True}}}, "candidates": []}
        profiles = report_profiles(report)
        self.assertTrue(profiles["shorts"]["enabled"])

    def test_report_profiles_prefers_explicit_field(self):
        report = {
            "profiles": {"highlights": {"aspect_ratio": "16:9", "min_duration_sec": 1,
                                         "max_duration_sec": 2, "end_padding_sec": 0},
                         "shorts": {"enabled": True, "aspect_ratio": "9:16", "min_duration_sec": 20,
                                    "target_duration_sec": 40, "max_duration_sec": 60,
                                    "crop_mode": "center", "subtitles_enabled": True,
                                    "max_candidates": 30, "final_candidates": 10}},
            "settings": {"outputs": {"shorts": {"enabled": False}}},
            "candidates": [],
        }
        profiles = report_profiles(report)
        self.assertTrue(profiles["shorts"]["enabled"])

    def test_migrate_preserves_existing_format_short(self):
        report = {"schema": 1, "video_id": "v", "settings": {},
                  "candidates": [{"candidate_id": "s001", "start": 0, "end": 40,
                                  "format": "short", "status": "pending"}]}
        migrate_report(report)
        self.assertEqual(report["candidates"][0]["format"], "short")


class ApprovalKeyIndependenceTests(unittest.TestCase):
    def test_shorts_key_differs_from_highlight_key(self):
        candidate = {"candidate_id": "s001", "start": 0, "end": 40, "format": "short",
                     "render_settings": {"crop_mode": "center", "subtitles_enabled": True,
                                          "output_width": 1080, "output_height": 1920}}
        highlight_key = approval_key(candidate)
        shorts_key = shorts_approval_key(candidate)
        self.assertNotEqual(highlight_key, shorts_key)

    def test_shorts_key_reacts_to_render_settings(self):
        base = {"candidate_id": "s001", "start": 0, "end": 40, "format": "short"}
        with_subtitles = dict(base, render_settings={"crop_mode": "center", "subtitles_enabled": True})
        without_subtitles = dict(base, render_settings={"crop_mode": "center", "subtitles_enabled": False})
        self.assertNotEqual(shorts_approval_key(with_subtitles), shorts_approval_key(without_subtitles))

    def test_shorts_key_reacts_to_crop(self):
        base = {"candidate_id": "s001", "start": 0, "end": 40, "format": "short",
                "render_settings": {"subtitles_enabled": True}}
        center = dict(base, render_settings={**base["render_settings"], "crop_mode": "center"})
        speaker = dict(base, render_settings={**base["render_settings"], "crop_mode": "speaker"})
        self.assertNotEqual(shorts_approval_key(center), shorts_approval_key(speaker))

    def test_shorts_key_ignores_title(self):
        a = {"candidate_id": "s001", "start": 0, "end": 40, "format": "short", "title": "Первый"}
        b = {"candidate_id": "s001", "start": 0, "end": 40, "format": "short", "title": "Второй"}
        self.assertEqual(shorts_approval_key(a), shorts_approval_key(b))

    def test_shorts_key_changes_with_bounds(self):
        a = {"candidate_id": "s001", "start": 0, "end": 40, "format": "short"}
        b = {"candidate_id": "s001", "start": 0, "end": 41, "format": "short"}
        self.assertNotEqual(shorts_approval_key(a), shorts_approval_key(b))

    def test_highlight_key_still_unchanged(self):
        # Существующее поведение approval_key: только id/start/end.
        a = {"candidate_id": "c001", "start": 10, "end": 130}
        b = dict(a, title="Любой заголовок", format="highlight")
        self.assertEqual(approval_key(a), approval_key(b))


class OpenReportMigrationTests(unittest.TestCase):
    def _make_job(self, report):
        import tempfile
        from pathlib import Path
        import unittest.mock as mock
        tmp = Path(tempfile.mkdtemp())
        job_dir = tmp / "data" / "vid" / "analysis" / "sig"
        job_dir.mkdir(parents=True)
        (job_dir.parent).mkdir(parents=True, exist_ok=True)
        # source.json лежит на уровне job_dir.parent.parent (data/vid/source.json)
        data_vid = tmp / "data" / "vid"
        save_json(data_vid / "source.json", {"video_id": "vid"})
        save_json(job_dir / "report.json", report)
        save_json(job_dir / "transcript.json", {"sentences": []})
        return job_dir / "report.json"

    def test_open_report_adds_format_and_profiles(self):
        report = {
            "schema": 1,
            "video_id": "vid",
            "settings": {},
            "candidates": [{"candidate_id": "c001", "start": 0, "end": 130, "status": "approved",
                            "approval_key": "x"}],
        }
        path = self._make_job(report)
        context = open_report(path)
        self.assertEqual(context["report"]["candidates"][0]["format"], "highlight")
        self.assertIn("profiles", context["report"])
        # Границы и утверждения не изменены.
        self.assertEqual(context["report"]["candidates"][0]["approval_key"], "x")


class LegacyOpenReportTests(unittest.TestCase):
    """Stage 7.4.2: открытие старого report.json без shorts_candidates/profiles/format."""

    def _make_job(self, report):
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        job_dir = tmp / "data" / "vid" / "analysis" / "sig"
        job_dir.mkdir(parents=True)
        data_vid = tmp / "data" / "vid"
        save_json(data_vid / "source.json", {"video_id": "vid"})
        save_json(job_dir / "report.json", report)
        save_json(job_dir / "transcript.json", {"sentences": []})
        return job_dir / "report.json"

    def test_open_legacy_report_gets_empty_shorts_candidates(self):
        """open_report добавляет пустой shorts_candidates к legacy-отчёту."""
        report = {
            "schema": 1,
            "video_id": "vid",
            "settings": {},
            "candidates": [
                {"candidate_id": "c001", "start": 10, "end": 130, "title": "А",
                 "status": "approved", "approval_key": "x"},
            ],
        }
        path = self._make_job(report)
        context = open_report(path)
        self.assertIn("shorts_candidates", context["report"])
        self.assertEqual(context["report"]["shorts_candidates"], [])

    def test_open_legacy_report_profiles_are_complete(self):
        """open_report добавляет корректные профили (highlights 16:9, shorts 9:16)."""
        report = {
            "schema": 1,
            "video_id": "vid",
            "settings": {},
            "candidates": [],
        }
        path = self._make_job(report)
        context = open_report(path)
        profiles = context["report"]["profiles"]
        self.assertEqual(profiles["highlights"]["aspect_ratio"], "16:9")
        self.assertEqual(profiles["highlights"]["min_duration_sec"], 120)
        self.assertEqual(profiles["highlights"]["max_duration_sec"], 300)
        self.assertEqual(profiles["highlights"]["end_padding_sec"], 0.5)
        self.assertFalse(profiles["shorts"]["enabled"])
        self.assertEqual(profiles["shorts"]["aspect_ratio"], "9:16")
        self.assertEqual(profiles["shorts"]["min_duration_sec"], 20)
        self.assertEqual(profiles["shorts"]["max_duration_sec"], 60)
        self.assertEqual(profiles["shorts"]["crop_mode"], "center")
        self.assertTrue(profiles["shorts"]["subtitles_enabled"])

    def test_open_legacy_report_preserves_editorial_fields(self):
        """Старые Highlights-кандидаты получают format, а редакторские поля не теряются."""
        report = {
            "schema": 1,
            "video_id": "vid",
            "settings": {},
            "candidates": [
                {"candidate_id": "c001", "start": 10, "end": 130, "title": "Тема 1",
                 "status": "approved", "approval_key": "x",
                 "score": 8, "reason": "Тестовая причина",
                 "manual_bounds": True, "duration_sec": 120,
                 "warnings": ["предупреждение 1"],
                 "end_padding": {"base_end": 130, "padded_end": 130.5,
                                  "applied_sec": 0.5, "requested_sec": 0.5}},
                {"candidate_id": "c002", "start": 200, "end": 320, "title": "Тема 2",
                 "status": "pending", "score": 6, "reason": "Другая причина"},
                {"candidate_id": "c003", "start": 400, "end": 500, "title": "Тема 3",
                 "status": "rejected", "score": 3, "reason": "Ещё причина"},
            ],
        }
        path = self._make_job(report)
        context = open_report(path)
        candidates = {c["candidate_id"]: c for c in context["report"]["candidates"]}
        # Формат добавлен каждому.
        for cid in ("c001", "c002", "c003"):
            self.assertEqual(candidates[cid]["format"], "highlight")
        # Редакторские поля c001 сохранены.
        c1 = candidates["c001"]
        self.assertEqual(c1["title"], "Тема 1")
        self.assertEqual(c1["status"], "approved")
        self.assertEqual(c1["approval_key"], "x")
        self.assertEqual(c1["score"], 8)
        self.assertEqual(c1["reason"], "Тестовая причина")
        self.assertTrue(c1["manual_bounds"])
        self.assertEqual(c1["duration_sec"], 120)
        self.assertEqual(c1["warnings"], ["предупреждение 1"])
        self.assertEqual(c1["end_padding"]["base_end"], 130)
        self.assertEqual(c1["end_padding"]["padded_end"], 130.5)
        self.assertEqual(c1["start"], 10)
        self.assertEqual(c1["end"], 130)
        # Статусы c002/c003 не изменены.
        self.assertEqual(candidates["c002"]["status"], "pending")
        self.assertEqual(candidates["c003"]["status"], "rejected")

    def test_open_report_does_not_rewrite_report_json(self):
        """Простое открытие не перезаписывает report.json на диске."""
        report = {
            "schema": 1,
            "video_id": "vid",
            "settings": {},
            "candidates": [
                {"candidate_id": "c001", "start": 10, "end": 130, "title": "А",
                 "status": "approved", "approval_key": "x"},
            ],
        }
        path = self._make_job(report)
        content_before = path.read_text(encoding="utf-8")
        context = open_report(path)
        content_after = path.read_text(encoding="utf-8")
        self.assertEqual(content_before, content_after,
                         "open_report не должен перезаписывать report.json на диске")
        # Контекст содержит смигрированный отчёт (в памяти), но файл не тронут.
        self.assertIn("shorts_candidates", context["report"])
        self.assertEqual(context["report"]["candidates"][0]["format"], "highlight")


if __name__ == "__main__":
    unittest.main()
