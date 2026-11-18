"""Framing model regression tests: temporary reports only, no media processes."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from job_store import load_json, save_json
from renderer import shorts_approval_key
from shorts_framing import (
    FramingError, candidate_framing, normalize_framing, save_candidate_framing,
)


def framing(*intervals):
    return {"schema_version": 1, "default_mode": "CROP", "intervals": [
        {"start": a, "end": b, "mode": mode} for a, b, mode in intervals]}


def short(cid="sc001"):
    result = {"candidate_id": cid, "start": 100.0, "end": 130.0,
              "status": "approved", "format": "short",
              "render_settings": {"crop_x": 0.25, "subtitles_enabled": True}}
    result["approval_key"] = shorts_approval_key(result)
    return result


class NormalizeFramingTests(unittest.TestCase):
    def test_missing_and_empty(self):
        self.assertEqual(normalize_framing(None, 30), framing())
        self.assertEqual(normalize_framing(framing(), 30), framing())

    def test_sort_merge_and_implicit_crop(self):
        raw = framing((9, 12, "FIT_BLUR"), (0, 2, "CROP"),
                      (4, 9, "FIT_BLUR"), (20, 30, "FIT_BLUR"))
        original = copy.deepcopy(raw)
        expected = framing((4.0, 12.0, "FIT_BLUR"), (20.0, 30.0, "FIT_BLUR"))
        self.assertEqual(normalize_framing(raw, 30), expected)
        self.assertEqual(raw, original)
        self.assertEqual(normalize_framing(expected, 30), expected)

    def test_full_duration_and_subframe_precision(self):
        self.assertEqual(normalize_framing(framing((0, 30, "FIT_BLUR")), 30),
                         framing((0.0, 30.0, "FIT_BLUR")))
        raw = framing((0, 1.00000001, "FIT_BLUR"), (1.00000002, 2, "FIT_BLUR"))
        self.assertEqual(len(normalize_framing(raw, 30)["intervals"]), 2)

    def test_invalid_numbers(self):
        for value in (True, False, "1", None, float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            for key in ("start", "end"):
                with self.subTest(value=str(value), key=key):
                    raw = framing((1, 2, "FIT_BLUR"))
                    raw["intervals"][0][key] = value
                    with self.assertRaises(FramingError):
                        normalize_framing(raw, 30)

    def test_invalid_duration(self):
        for value in (0, -1, True, "30", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(FramingError):
                normalize_framing(None, value)

    def test_invalid_bounds(self):
        for a, b in ((-1, 2), (2, 2), (3, 2), (0, 30.00000001)):
            with self.subTest(a=a, b=b), self.assertRaises(FramingError):
                normalize_framing(framing((a, b, "FIT_BLUR")), 30)

    def test_overlaps_including_crop_are_rejected(self):
        for mode in ("FIT_BLUR", "CROP"):
            with self.subTest(mode=mode), self.assertRaises(FramingError):
                normalize_framing(framing((1, 4, "FIT_BLUR"), (3, 5, mode)), 30)

    def test_invalid_documents(self):
        for raw in ([], {}, {**framing(), "schema_version": True},
                    {**framing(), "schema_version": 2},
                    {**framing(), "default_mode": "FIT_BLUR"},
                    {**framing(), "intervals": None},
                    {**framing(), "intervals": [None]},
                    framing((0, 2, "UNKNOWN"))):
            with self.subTest(raw=raw), self.assertRaises(FramingError):
                normalize_framing(raw, 30)


class FramingApprovalTests(unittest.TestCase):
    def test_legacy_key_exactly_matches_original_algorithm(self):
        for render in (None, {}, {"crop_x": 0.25, "subtitles_enabled": False}):
            c = short()
            c["render_settings"] = render
            c["transcript_edits"] = [{"text": "исправлено"}]
            r = render or {}
            payload = {"candidate_id": c["candidate_id"], "start": c["start"],
                       "end": c["end"], "format": c.get("format", "short"),
                       "crop_mode": r.get("crop_mode", "center"), "crop_x": r.get("crop_x"),
                       "subtitles_enabled": r.get("subtitles_enabled", True),
                       "output_width": r.get("output_width", 1080),
                       "output_height": r.get("output_height", 1920),
                       "end_padding": c.get("end_padding"), "transcript_edits": c["transcript_edits"]}
            expected = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                                allow_nan=False).encode("utf-8")).hexdigest()
            self.assertEqual(shorts_approval_key(c), expected)

    def test_key_uses_normalized_framing(self):
        a, b = short(), short()
        a["render_settings"]["framing"] = framing((5, 8, "FIT_BLUR"), (2, 5, "FIT_BLUR"))
        b["render_settings"]["framing"] = framing((2, 8, "FIT_BLUR"))
        self.assertEqual(shorts_approval_key(a), shorts_approval_key(b))
        self.assertNotEqual(shorts_approval_key(a), shorts_approval_key(short()))
        b["render_settings"]["framing"] = framing((2, 9, "FIT_BLUR"))
        self.assertNotEqual(shorts_approval_key(a), shorts_approval_key(b))

    def test_explicit_empty_is_new_payload(self):
        a = short()
        old = shorts_approval_key(a)
        a["render_settings"]["framing"] = framing()
        self.assertNotEqual(shorts_approval_key(a), old)

    def test_stale_anchor_rejected_even_with_same_duration(self):
        a = short()
        a["render_settings"].update(framing=framing((1, 5, "FIT_BLUR")),
                                    framing_source_bounds={"start": 99, "end": 129})
        with self.assertRaises(FramingError):
            shorts_approval_key(a)


class FramingPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "report.json"
        self.report = {"schema": 1, "video_id": "test", "signature": "sig", "settings": {},
                       "candidates": [{"candidate_id": "c001", "status": "approved", "approval_key": "hl"}],
                       "shorts_candidates": [short(), short("sc002")]}
        save_json(self.path, self.report)

    def save(self, value, **kwargs):
        return save_candidate_framing(self.path, "sc001", value,
                                      expected_start=kwargs.get("start", 100),
                                      expected_end=kwargs.get("end", 130))

    def test_explicit_save_roundtrip_and_independent_approvals(self):
        updated = self.save(framing((5, 8, "FIT_BLUR"), (2, 5, "FIT_BLUR")))
        self.assertEqual(updated, load_json(self.path))
        self.assertEqual(updated["candidates"], self.report["candidates"])
        self.assertEqual(updated["shorts_candidates"][1], self.report["shorts_candidates"][1])
        c = updated["shorts_candidates"][0]
        self.assertEqual(c["status"], "pending")
        self.assertNotIn("approval_key", c)
        self.assertEqual(c["render_settings"]["crop_x"], 0.25)
        self.assertEqual(candidate_framing(c), framing((2, 8, "FIT_BLUR")))
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_equivalent_save_preserves_approval(self):
        updated = self.save(framing((2, 8, "FIT_BLUR")))
        c = updated["shorts_candidates"][0]
        c["status"] = "approved"
        c["approval_key"] = shorts_approval_key(c)
        save_json(self.path, updated)
        saved = self.save(framing((5, 8, "FIT_BLUR"), (2, 5, "FIT_BLUR")))
        self.assertEqual(saved["shorts_candidates"][0], c)

    def test_reset_invalidates_only_target(self):
        approved = self.save(framing((2, 8, "FIT_BLUR")))
        candidate = approved["shorts_candidates"][0]
        candidate["status"] = "approved"
        candidate["approval_key"] = shorts_approval_key(candidate)
        save_json(self.path, approved)
        result = self.save(None)
        self.assertEqual(candidate_framing(result["shorts_candidates"][0]), framing())
        self.assertEqual(result["shorts_candidates"][0]["status"], "pending")
        self.assertNotIn("approval_key", result["shorts_candidates"][0])
        self.assertEqual(result["shorts_candidates"][1], self.report["shorts_candidates"][1])

    def test_changed_existing_framing_invalidates_approved_short(self):
        report = self.save(framing((2, 8, "FIT_BLUR")))
        candidate = report["shorts_candidates"][0]
        candidate["status"] = "approved"
        candidate["approval_key"] = shorts_approval_key(candidate)
        save_json(self.path, report)
        result = self.save(framing((3, 8, "FIT_BLUR")))
        self.assertEqual(result["shorts_candidates"][0]["status"], "pending")
        self.assertNotIn("approval_key", result["shorts_candidates"][0])
        self.assertEqual(result["candidates"], report["candidates"])
        self.assertEqual(result["shorts_candidates"][1], report["shorts_candidates"][1])

    def test_candidate_without_render_settings(self):
        report = copy.deepcopy(self.report)
        report["shorts_candidates"][0].pop("render_settings")
        save_json(self.path, report)
        result = self.save(framing((0, 30, "FIT_BLUR")))
        self.assertEqual(candidate_framing(result["shorts_candidates"][0]),
                         framing((0, 30, "FIT_BLUR")))

    def test_write_failure_preserves_previous_report(self):
        from unittest.mock import patch
        before = self.path.read_bytes()
        with patch("job_store.os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.save(framing((2, 8, "FIT_BLUR")))
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_or_stale_draft_does_not_write(self):
        before = self.path.read_bytes()
        for value, kwargs in ((framing((1, 31, "FIT_BLUR")), {}),
                              (framing((1, 3, "FIT_BLUR")), {"start": 101, "end": 131})):
            with self.subTest(kwargs=kwargs), self.assertRaises(FramingError):
                self.save(value, **kwargs)
            self.assertEqual(self.path.read_bytes(), before)

    def test_generic_save_rejects_changed_bounds_without_anchor(self):
        report = copy.deepcopy(self.report)
        report["shorts_candidates"][0]["render_settings"]["framing"] = framing((1, 4, "FIT_BLUR"))
        save_json(self.path, report)
        before = self.path.read_bytes()
        for a, b in ((101, 131), (100, 140)):
            changed = copy.deepcopy(report)
            changed["shorts_candidates"][0].update(start=a, end=b)
            with self.subTest(a=a, b=b), self.assertRaises(FramingError):
                save_json(self.path, changed)
            self.assertEqual(self.path.read_bytes(), before)

    def test_reset_allows_subsequent_boundary_edit(self):
        self.save(framing((1, 4, "FIT_BLUR")))
        updated = self.save(None)
        updated["shorts_candidates"][0].update(start=101, end=131)
        save_json(self.path, updated)
        self.assertEqual(candidate_framing(load_json(self.path)["shorts_candidates"][0]), framing())

    def test_explicit_reset_recovers_stale_anchor(self):
        report = self.save(framing((1, 4, "FIT_BLUR")))
        report["shorts_candidates"][0].update(start=101, end=131)
        # Simulate an externally modified report, bypassing guarded persistence.
        self.path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(FramingError):
            self.save(framing((1, 4, "FIT_BLUR")), start=101, end=131)
        reset = self.save(None, start=101, end=131)
        self.assertEqual(candidate_framing(reset["shorts_candidates"][0]), framing())

    def test_unknown_candidate_does_not_write(self):
        before = self.path.read_bytes()
        with self.assertRaises(FramingError):
            save_candidate_framing(self.path, "missing", None, expected_start=100, expected_end=130)
        self.assertEqual(self.path.read_bytes(), before)

    def test_open_legacy_report_does_not_write_or_add_framing(self):
        from app_backend import open_report
        root = Path(self.temp.name)
        folder = root / "analysis" / "sig"
        folder.mkdir(parents=True)
        path = folder / "report.json"
        save_json(path, self.report)
        save_json(root / "source.json", {"video_id": "test"})
        save_json(folder / "transcript.json", {"segments": []})
        before = path.read_bytes()
        context = open_report(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("framing", context["report"]["shorts_candidates"][0]["render_settings"])
        self.assertEqual(context["report"]["shorts_candidates"], self.report["shorts_candidates"])


if __name__ == "__main__":
    unittest.main()
