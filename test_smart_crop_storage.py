"""Unit tests for smart_crop_storage."""
import json
import tempfile
import unittest
from pathlib import Path

from smart_crop_storage import (
    EPS,
    SCHEMA_VERSION,
    SHOT_TYPES,
    TARGETS,
    SmartCropError,
    clear_manual_override,
    create_settings,
    load_settings,
    merge_segments,
    save_settings,
    segment_at,
    set_segment_crop,
    set_segment_target,
    settings_path,
    split_segment,
    validate_segments,
    validate_settings,
)


def make_segments():
    return [
        {
            "start": 0.0,
            "end": 8.0,
            "shot_type": "HOST_MEDIUM",
            "target": "HOST",
            "crop": {"x": 0.0, "y": 0.0, "width": 0.5625, "height": 1.0},
            "manual_override": False,
        },
        {
            "start": 8.0,
            "end": 15.0,
            "shot_type": "SPLIT",
            "target": "GUEST",
            "crop": {"x": 0.4375, "y": 0.0, "width": 0.5625, "height": 1.0},
            "manual_override": True,
        },
    ]


class TestCreateSettings(unittest.TestCase):
    def test_create_default(self):
        settings = create_settings(
            "sc001", 1450.0, 1505.0, 1920, 1080)
        self.assertEqual(settings["schema_version"], SCHEMA_VERSION)
        self.assertEqual(settings["candidate_id"], "sc001")
        self.assertEqual(settings["source_start"], 1450.0)
        self.assertEqual(settings["source_end"], 1505.0)
        self.assertEqual(settings["source_width"], 1920)
        self.assertEqual(settings["source_height"], 1080)
        self.assertEqual(len(settings["segments"]), 1)
        segment = settings["segments"][0]
        self.assertEqual(segment["start"], 0.0)
        self.assertEqual(segment["end"], 55.0)
        self.assertEqual(segment["shot_type"], "UNKNOWN")
        self.assertEqual(segment["target"], "AUTO")
        self.assertFalse(segment["manual_override"])
        self.assertIn(segment["crop"]["width"], (9 / 16.0, 0.5625))

    def test_create_with_segments(self):
        settings = create_settings(
            "sc002", 0.0, 15.0, 1920, 1080, make_segments())
        self.assertEqual(len(settings["segments"]), 2)
        self.assertEqual(settings["segments"][1]["target"], "GUEST")
        self.assertTrue(settings["segments"][1]["manual_override"])

    def test_create_rejects_negative_start(self):
        with self.assertRaises(SmartCropError):
            create_settings("sc001", -1.0, 10.0, 1920, 1080)

    def test_create_rejects_bad_dimensions(self):
        with self.assertRaises(SmartCropError):
            create_settings("sc001", 0.0, 10.0, 0, 1080)

    def test_create_rejects_zero_duration(self):
        with self.assertRaises(SmartCropError):
            create_settings("sc001", 10.0, 10.0, 1920, 1080)

    def test_create_rejects_bad_segments(self):
        with self.assertRaises(SmartCropError):
            create_settings(
                "sc001", 0.0, 10.0, 1920, 1080,
                [{"start": 0.0, "end": 5.0, "shot_type": "HOST_MEDIUM",
                  "target": "HOST",
                  "crop": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
                  "manual_override": False}])


class TestValidateSegments(unittest.TestCase):
    def test_valid(self):
        segments = validate_segments(make_segments(), 15.0)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[-1]["end"], 15.0)

    def test_rejects_gap(self):
        segments = [
            {"start": 0.0, "end": 5.0, "shot_type": "HOST_MEDIUM", "target": "HOST",
             "crop": {"x": 0, "y": 0, "width": 0.5, "height": 1}, "manual_override": False},
            {"start": 6.0, "end": 15.0, "shot_type": "GUEST_MEDIUM", "target": "GUEST",
             "crop": {"x": 0.5, "y": 0, "width": 0.5, "height": 1}, "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_rejects_overlap(self):
        segments = [
            {"start": 0.0, "end": 8.0, "shot_type": "HOST_MEDIUM", "target": "HOST",
             "crop": {"x": 0, "y": 0, "width": 0.5, "height": 1}, "manual_override": False},
            {"start": 7.0, "end": 15.0, "shot_type": "GUEST_MEDIUM", "target": "GUEST",
             "crop": {"x": 0.5, "y": 0, "width": 0.5, "height": 1}, "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_rejects_not_starting_at_zero(self):
        segments = [
            {"start": 1.0, "end": 15.0, "shot_type": "HOST_MEDIUM", "target": "HOST",
             "crop": {"x": 0, "y": 0, "width": 0.5, "height": 1}, "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_rejects_not_covering_end(self):
        segments = [
            {"start": 0.0, "end": 14.0, "shot_type": "HOST_MEDIUM", "target": "HOST",
             "crop": {"x": 0, "y": 0, "width": 0.5, "height": 1}, "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_rejects_empty(self):
        with self.assertRaises(SmartCropError):
            validate_segments([], 15.0)

    def test_rejects_bad_crop(self):
        segments = [
            {"start": 0.0, "end": 15.0, "shot_type": "HOST_MEDIUM", "target": "HOST",
             "crop": {"x": 0.6, "y": 0.0, "width": 0.5, "height": 1.0},
             "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_rejects_bad_target(self):
        segments = [
            {"start": 0.0, "end": 15.0, "shot_type": "HOST_MEDIUM", "target": "BAD",
             "crop": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
             "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_rejects_bad_shot_type(self):
        segments = [
            {"start": 0.0, "end": 15.0, "shot_type": "BAD_SHOT", "target": "HOST",
             "crop": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
             "manual_override": False},
        ]
        with self.assertRaises(SmartCropError):
            validate_segments(segments, 15.0)

    def test_snap_end_to_duration(self):
        segments = [
            {"start": 0.0, "end": 14.9999999, "shot_type": "HOST_MEDIUM", "target": "HOST",
             "crop": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
             "manual_override": False},
        ]
        result = validate_segments(segments, 15.0)
        self.assertEqual(result[-1]["end"], 15.0)


class TestSaveLoad(unittest.TestCase):
    def test_roundtrip(self):
        settings = create_settings("sc001", 1450.0, 1465.0, 1920, 1080, make_segments())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis" / "sig" / "smart_crop_sc001.json"
            save_settings(settings, path)
            self.assertTrue(path.exists())
            loaded, warnings = load_settings(path)
            self.assertEqual(loaded["candidate_id"], "sc001")
            self.assertEqual(loaded["source_end"], 1465.0)
            self.assertEqual(loaded["segments"][1]["target"], "GUEST")
            self.assertTrue(loaded["segments"][1]["manual_override"])
            self.assertFalse(loaded["segments"][0]["manual_override"])
            self.assertEqual(warnings, [])

    def test_atomic_no_tmp_left(self):
        settings = create_settings("sc001", 0.0, 10.0, 1920, 1080)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "smart_crop_sc001.json"
            save_settings(settings, path)
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_load_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "smart_crop_missing.json"
            with self.assertRaises(SmartCropError):
                load_settings(path)

    def test_load_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "smart_crop_bad.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(SmartCropError):
                load_settings(path)

    def test_load_old_schema_warns(self):
        settings = create_settings("sc001", 0.0, 10.0, 1920, 1080)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "smart_crop_old.json"
            old = json.loads(json.dumps(settings))
            old["schema_version"] = 0
            path.write_text(json.dumps(old), encoding="utf-8")
            loaded, warnings = load_settings(path)
            self.assertEqual(loaded["schema_version"], SCHEMA_VERSION)
            self.assertTrue(any("schema_version" in w for w in warnings))


class TestSplitMerge(unittest.TestCase):
    def setUp(self):
        self.settings = create_settings("sc001", 0.0, 15.0, 1920, 1080, make_segments())

    def test_split_creates_two(self):
        result = split_segment(self.settings, 4.0)
        self.assertEqual(len(result["segments"]), 3)
        self.assertEqual(result["segments"][0]["end"], 4.0)
        self.assertEqual(result["segments"][1]["start"], 4.0)
        self.assertEqual(result["segments"][1]["end"], 8.0)
        self.assertFalse(result["segments"][1]["manual_override"])
        self.assertTrue(result["segments"][2]["manual_override"])

    def test_split_outside_rejected(self):
        with self.assertRaises(SmartCropError):
            split_segment(self.settings, 0.0)
        with self.assertRaises(SmartCropError):
            split_segment(self.settings, 15.0)
        with self.assertRaises(SmartCropError):
            split_segment(self.settings, 8.0)  # exactly on boundary

    def test_merge(self):
        result = merge_segments(self.settings, 0)
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["end"], 15.0)
        self.assertTrue(result["segments"][0]["manual_override"])

    def test_merge_out_of_range(self):
        with self.assertRaises(SmartCropError):
            merge_segments(self.settings, 5)
        with self.assertRaises(SmartCropError):
            merge_segments(self.settings, -1)


class TestMutations(unittest.TestCase):
    def setUp(self):
        self.settings = create_settings("sc001", 0.0, 15.0, 1920, 1080, make_segments())

    def test_set_target_marks_manual(self):
        result = set_segment_target(self.settings, 0, "GUEST")
        self.assertEqual(result["segments"][0]["target"], "GUEST")
        self.assertTrue(result["segments"][0]["manual_override"])

    def test_set_target_rejects_bad(self):
        with self.assertRaises(SmartCropError):
            set_segment_target(self.settings, 0, "NOPE")

    def test_clear_override(self):
        result = clear_manual_override(self.settings, 1)
        self.assertFalse(result["segments"][1]["manual_override"])
        self.assertTrue(self.settings["segments"][1]["manual_override"])

    def test_set_crop(self):
        result = set_segment_crop(
            self.settings, 0, {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.8})
        self.assertEqual(result["segments"][0]["crop"]["x"], 0.1)
        self.assertTrue(result["segments"][0]["manual_override"])

    def test_set_crop_rejects_out_of_bounds(self):
        with self.assertRaises(SmartCropError):
            set_segment_crop(
                self.settings, 0, {"x": 0.9, "y": 0.0, "width": 0.3, "height": 1.0})


class TestSegmentAt(unittest.TestCase):
    def setUp(self):
        self.settings = create_settings("sc001", 0.0, 15.0, 1920, 1080, make_segments())

    def test_inside_first(self):
        self.assertEqual(segment_at(self.settings, 0.0), 0)
        self.assertEqual(segment_at(self.settings, 4.0), 0)

    def test_inside_second(self):
        self.assertEqual(segment_at(self.settings, 8.0), 1)
        self.assertEqual(segment_at(self.settings, 14.0), 1)

    def test_at_end(self):
        self.assertEqual(segment_at(self.settings, 15.0), 1)

    def test_outside(self):
        self.assertEqual(segment_at(self.settings, -1.0), -1)
        self.assertEqual(segment_at(self.settings, 100.0), -1)


class TestSettingsPath(unittest.TestCase):
    def test_path(self):
        path = settings_path("/data/video", "sig123", "sc001")
        self.assertEqual(
            path, Path("/data/video/analysis/sig123/smart_crop_sc001.json"))


if __name__ == "__main__":
    unittest.main()
