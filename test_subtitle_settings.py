import copy
import json
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from subtitle_settings import (DEFAULTS, SubtitleSettings, ass_color, build_ass,
                               document, from_document, load_settings, save_settings, _ass_time, subtitle_area,
                               effective_style, uses_legacy_srt_style)
from subs_settings_window import SubsSettingsWindow, render_preview


class SettingsTests(unittest.TestCase):
    def test_shadow_toggle_persistence_and_v7_migration(self):
        enabled = replace(DEFAULTS, shadow_enabled=True, shadow_size=3, shadow_color="#123456")
        disabled = replace(enabled, shadow_enabled=False)
        self.assertEqual(from_document(document(disabled)), disabled)
        self.assertTrue(uses_legacy_srt_style(disabled))
        self.assertFalse(uses_legacy_srt_style(enabled))
        for value in (0, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(DEFAULTS, shadow_enabled=value)
        for size in (0, 3):
            old = asdict(replace(enabled, shadow_size=size))
            old.pop("shadow_enabled")
            migrated = from_document({"schema_version": 7, "shorts": old})
            self.assertEqual(migrated.shadow_enabled, size > 0)
            self.assertEqual(migrated.shadow_size, size)
            self.assertEqual(migrated.shadow_color, enabled.shadow_color)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_shadow_visible_in_synthetic_preview_without_outline(self):
        from io import BytesIO
        from PIL import Image
        settings = replace(DEFAULTS, outline_enabled=False, shadow_enabled=True, shadow_color="#FF0000", shadow_size=3)
        def red_pixels(style):
            image = Image.open(BytesIO(render_preview(style))).convert("RGB")
            pixels = image.tobytes()
            return sum(r > 120 and r > g * 1.5 and r > b * 1.5
                       for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3]))
        self.assertGreater(red_pixels(settings), 30)
        self.assertEqual(red_pixels(replace(settings, shadow_size=0)), 0)
        self.assertEqual(red_pixels(replace(settings, shadow_enabled=False)), 0)

    def test_shadow_validation_presets_and_ass(self):
        for value in (-1, 21, True, "2", None, float("nan"), float("inf")):
            with self.subTest(size=value), self.assertRaises(ValueError):
                replace(DEFAULTS, shadow_size=value)
        for color in ("black", "#12345", None):
            with self.subTest(color=color), self.assertRaises(ValueError):
                replace(DEFAULTS, shadow_color=color)
        settings = replace(DEFAULTS, shadow_enabled=True, shadow_size=2.5, shadow_color="#123456", outline_enabled=False)
        self.assertEqual(from_document(document(settings)), settings)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.json"
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)
        old = asdict(replace(DEFAULTS, text_color="#AABBCC"))
        old.pop("shadow_color")
        old.pop("shadow_size")
        old.pop("shadow_enabled")
        loaded = from_document({"schema_version": 6, "shorts": old})
        self.assertEqual(loaded.text_color, "#AABBCC")
        self.assertEqual(loaded.shadow_size, 0)
        self.assertTrue(uses_legacy_srt_style(replace(DEFAULTS, shadow_color="#ABCDEF")))
        self.assertFalse(uses_legacy_srt_style(settings))
        ass = build_ass("1\n00:00:00,000 --> 00:00:01,000\nТекст\n", settings)
        style = next(line for line in ass.splitlines() if line.startswith("Style: Text,"))
        fields = style.split(",")
        self.assertEqual(fields[6], "&H00563412&")
        self.assertEqual(fields[15:18], ["1", "0", "2.5"])

    def test_text_color_roundtrip_legacy_and_word_reset(self):
        custom = replace(DEFAULTS, text_color="#12AB34", highlight_word_enabled=True,
                         highlight_word_color="#FF0000")
        self.assertEqual(from_document(document(custom)), custom)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "colored.json"
            save_settings(path, custom)
            self.assertEqual(load_settings(path), custom)
        old = asdict(DEFAULTS)
        old.pop("text_color")
        old.pop("shadow_color")
        old.pop("shadow_size")
        old.pop("shadow_enabled")
        self.assertEqual(from_document({"schema_version": 5, "shorts": old}).text_color, "#FFFFFF")
        for color in ("red", "#12345", None, 123):
            with self.subTest(color=color), self.assertRaises(ValueError):
                replace(DEFAULTS, text_color=color)
        self.assertFalse(uses_legacy_srt_style(custom))
        self.assertFalse(uses_legacy_srt_style(replace(DEFAULTS, text_color="#12AB34")))
        srt = "1\n00:00:00,000 --> 00:00:02,000\nПервое второе\n"
        cues = [[{"word": "Первое", "start": 0, "end": 1},
                 {"word": "второе", "start": 1, "end": 2}]]
        ass = build_ass(srt, custom, cues)
        self.assertIn("Style: Text,Arial,18,&H0034AB12&", ass)
        self.assertIn(r"{\1c&H000000FF&}Первое{\1c&H0034AB12&} второе", ass)
        self.assertNotIn(r"{\1c&H00FFFFFF&}", ass)

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
                      if k not in {"outline_enabled", "outline_color", "area_enabled", "width_percent", "height_percent",
                                   "highlight_word_enabled", "highlight_word_color", "background_padding_x_px",
                                   "background_padding_y_px", "background_corner_radius_px", "background_geometry_mode",
                                    "background_fill_region", "text_color", "shadow_color", "shadow_size", "shadow_enabled"}}
        self.assertEqual(from_document({"schema_version": 1, "shorts": old_fields}), DEFAULTS)
        old_fields.update(position_mode="free", x=40, y=65, background_enabled=True)
        migrated = from_document({"schema_version": 1, "shorts": old_fields})
        self.assertFalse(migrated.area_enabled)
        self.assertTrue(migrated.outline_enabled)
        self.assertEqual(migrated.y, 65)
        self.assertEqual(document(migrated)["schema_version"], 8)
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

    def test_word_highlight_timing_gaps_and_reset(self):
        from renderer import build_subtitles_srt
        transcript = {"segments": [{"start": 9, "end": 13, "words": [
            {"word": "Первое", "start": 9.8, "end": 10.5},
            {"word": "второе", "start": 10.8, "end": 12.2}]}]}
        srt, cues = build_subtitles_srt(transcript, 10, 12, return_cues=True)
        settings = replace(DEFAULTS, highlight_word_enabled=True, highlight_word_color="#123456")
        ass = build_ass(srt, settings, cues)
        events = [line for line in ass.splitlines() if line.startswith("Dialogue: 1")]
        self.assertEqual(len(events), 3)
        self.assertIn("0:00:00.00,0:00:00.50", events[0])
        self.assertIn(r"{\1c&H00563412&}Первое{\1c&H00FFFFFF&} второе", events[0])
        self.assertIn("0:00:00.50,0:00:00.80", events[1])
        self.assertNotIn(r"\1c", events[1])
        self.assertIn(r"Первое {\1c&H00563412&}второе{\1c&H00FFFFFF&}", events[2])
        self.assertIn("0:00:00.80,0:00:02.00", events[2])
        self.assertNotIn(r"\1c", build_ass(srt, DEFAULTS, cues))

    def test_v2_migration_and_highlight_validation(self):
        fields = asdict(DEFAULTS)
        fields.pop("text_color")
        fields.pop("shadow_color")
        fields.pop("shadow_size")
        fields.pop("shadow_enabled")
        fields.pop("highlight_word_enabled")
        fields.pop("highlight_word_color")
        for key in ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px",
                    "background_geometry_mode", "background_fill_region"):
            fields.pop(key)
        self.assertEqual(from_document({"schema_version": 2, "shorts": fields}), DEFAULTS)
        custom = replace(DEFAULTS, highlight_word_enabled=True, highlight_word_color="#123456")
        self.assertEqual(from_document(document(custom)), custom)
        for values in ({"highlight_word_enabled": 1}, {"highlight_word_color": "yellow"}):
            with self.assertRaises(ValueError):
                replace(DEFAULTS, **values)

    def test_geometry_validation(self):
        for key in ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px"):
            for value in (-1, 512.01, float("nan"), float("inf"), -float("inf"), True, False,
                          "12", None, [], {}, 10**1000, -(10**1000)):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    replace(DEFAULTS, **{key: value})
            for value in (0, 0.25, 37.5, 512):
                with self.subTest(key=key, value=value):
                    self.assertEqual(getattr(replace(DEFAULTS, **{key: value}), key), value)
        for mode in ("", "LEGACY", "rounded", True, 0, None, [], {}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                replace(DEFAULTS, background_geometry_mode=mode)
        self.assertEqual(replace(DEFAULTS, background_geometry_mode="custom").background_geometry_mode, "custom")

    def test_v5_roundtrip_retains_inactive_values_and_model_defaults(self):
        custom = replace(DEFAULTS, background_geometry_mode="custom", background_padding_x_px=37.5,
                         background_padding_y_px=4.25, background_corner_radius_px=512,
                         background_color="#123456", background_opacity=42, background_fill_region=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preset.json"
            save_settings(path, custom)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored, {"schema_version": 8, "shorts": asdict(custom)})
            loaded = load_settings(path)
            self.assertEqual(loaded, custom)
            self.assertFalse(loaded.background_enabled)
            # Model equivalent of Set Default: immutable built-ins, not last preset.
            self.assertEqual(SubtitleSettings(), DEFAULTS)
            self.assertEqual(DEFAULTS.background_geometry_mode, "legacy")
            self.assertFalse(DEFAULTS.background_fill_region)
            for key in ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px"):
                self.assertEqual(getattr(DEFAULTS, key), 0)
            save_settings(path, DEFAULTS)
            self.assertEqual(load_settings(path), DEFAULTS)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 8)

    def test_all_old_versions_load_without_rewrite_and_save_as_v5(self):
        geometry = {"background_padding_x_px", "background_padding_y_px",
                    "background_corner_radius_px", "background_geometry_mode"}
        v2 = {"outline_enabled", "outline_color", "area_enabled", "width_percent", "height_percent"}
        v3 = {"highlight_word_enabled", "highlight_word_color"}
        old = replace(DEFAULTS, font_size=24, position_mode="free", x=40, y=65,
                      background_enabled=True, background_color="#123456", background_opacity=42)
        srt = "1\n00:00:00,000 --> 00:00:01,000\nПример\nВторая строка\n"
        for version, mode in ((1, "legacy"), (2, "legacy"), (3, "legacy"), (4, "legacy"), (4, "custom")):
            with self.subTest(version=version, mode=mode), tempfile.TemporaryDirectory() as directory:
                expected = old
                if version >= 2:
                    expected = replace(expected, outline_enabled=False, outline_color="#AABBCC",
                                       area_enabled=True, width_percent=45.5, height_percent=18.25)
                if version >= 3:
                    expected = replace(expected, highlight_word_enabled=True, highlight_word_color="#ABCDEF")
                if version == 4:
                    expected = replace(expected, background_geometry_mode=mode, background_padding_x_px=37.5,
                                       background_padding_y_px=4.25, background_corner_radius_px=128)
                missing = ({"background_fill_region", "text_color", "shadow_color", "shadow_size", "shadow_enabled"} | (geometry if version < 4 else set())
                           | (v3 if version < 3 else set()) | (v2 if version < 2 else set()))
                fields = {key: value for key, value in asdict(expected).items() if key not in missing}
                path = Path(directory) / "old.json"
                original = ("\ufeff" + json.dumps({"schema_version": version, "shorts": fields},
                                                  ensure_ascii=False, indent=4) + "\n").encode("utf-8")
                path.write_bytes(original)
                original_stat = path.stat()
                with patch("subtitle_settings.save_settings", side_effect=AssertionError("Automatic save")), \
                        patch("subtitle_settings.os.replace", side_effect=AssertionError("Automatic replace")):
                    loaded = load_settings(path)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
                self.assertEqual(set(Path(directory).iterdir()), {path})
                self.assertEqual(loaded, expected)
                self.assertEqual(loaded.background_geometry_mode, mode)
                self.assertFalse(loaded.background_fill_region)
                self.assertEqual(build_ass(srt, loaded), build_ass(srt, expected))
                self.assertFalse(uses_legacy_srt_style(loaded))  # Enabled legacy box still needs ASS.
                save_settings(path, loaded)  # Explicit Save/Apply, including the same old path.
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 8)
                self.assertEqual(load_settings(path), expected)

    def test_schema_field_sets_are_strict_for_every_version(self):
        geometry = {"background_padding_x_px", "background_padding_y_px",
                    "background_corner_radius_px", "background_geometry_mode"}
        added = {"outline_enabled", "outline_color", "area_enabled", "width_percent", "height_percent"}
        highlight = {"highlight_word_enabled", "highlight_word_color"}
        for version in (1, 2, 3, 4, 5, 6, 7, 8):
            missing = (geometry if version < 4 else set()) | (highlight if version < 3 else set())
            if version < 8:
                missing.add("shadow_enabled")
            if version < 7:
                missing |= {"shadow_color", "shadow_size"}
            if version < 6:
                missing.add("text_color")
            if version < 5:
                missing.add("background_fill_region")
            if version == 1:
                missing |= added
            fields = {key: value for key, value in asdict(DEFAULTS).items() if key not in missing}
            valid = {"schema_version": version, "shorts": fields}
            self.assertEqual(from_document(valid), DEFAULTS)
            invalid = [dict(valid, unknown=True), dict(valid, shorts={**fields, "unknown": 0}),
                       dict(valid, shorts={key: value for key, value in fields.items() if key != "font"})]
            if version < 5:
                invalid.append(dict(valid, shorts={**fields, "background_fill_region": False}))
            else:
                invalid.append(dict(valid, shorts={key: value for key, value in fields.items()
                                                  if key != "background_fill_region"}))
            if version < 4:
                invalid.extend(dict(valid, shorts={**fields, key: getattr(DEFAULTS, key)}) for key in geometry)
            else:
                invalid.extend(dict(valid, shorts={key: value for key, value in fields.items() if key != omitted})
                               for omitted in geometry)
                invalid.append(dict(valid, shorts={**fields, "background_padding_x_px": True}))
                invalid.append(dict(valid, shorts={**fields, "background_geometry_mode": "invalid"}))
            for value in invalid:
                with self.subTest(version=version, document=value), self.assertRaises(ValueError):
                    from_document(value)
        for version in (True, 8.0, "8", 0, 9):
            with self.subTest(version=version), self.assertRaises(ValueError):
                from_document({"schema_version": version, "shorts": asdict(DEFAULTS)})

    def test_effective_style_ignores_disabled_effects_without_losing_values(self):
        draft = replace(DEFAULTS, background_color="#123456", background_opacity=42,
                        background_geometry_mode="custom", background_padding_x_px=37,
                         background_padding_y_px=4, background_corner_radius_px=20, background_fill_region=True,
                        x=12, y=78, width_percent=20, height_percent=30, highlight_word_color="#123456")
        original_document = document(draft)
        self.assertNotEqual(draft, DEFAULTS)  # Raw equality/persistence must not discard the draft.
        self.assertEqual(effective_style(draft), effective_style(DEFAULTS))
        self.assertTrue(uses_legacy_srt_style(draft))
        self.assertEqual(document(draft), original_document)
        self.assertEqual(from_document(original_document), draft)
        self.assertEqual(effective_style(effective_style(draft)), effective_style(draft))
        custom_text = replace(draft, font_size=24, position_mode="free")
        equivalent = replace(DEFAULTS, font_size=24, position_mode="free", x=12, y=78)
        self.assertEqual(effective_style(custom_text), effective_style(equivalent))
        self.assertFalse(uses_legacy_srt_style(custom_text))
        outline_off = replace(DEFAULTS, outline_enabled=False)
        self.assertEqual(effective_style(outline_off), effective_style(replace(outline_off, outline_color="#123456")))

    def test_effective_style_preserves_active_effects_and_legacy_geometry(self):
        legacy_box = replace(DEFAULTS, background_enabled=True)
        inactive_dimensions = replace(legacy_box, background_padding_x_px=37,
                                      background_padding_y_px=4, background_corner_radius_px=20)
        self.assertEqual(effective_style(legacy_box), effective_style(inactive_dimensions))
        self.assertFalse(uses_legacy_srt_style(legacy_box))
        custom_box = replace(inactive_dimensions, background_geometry_mode="custom")
        self.assertNotEqual(effective_style(custom_box), effective_style(legacy_box))
        for key in ("background_padding_x_px", "background_padding_y_px"):
            self.assertNotEqual(effective_style(custom_box), effective_style(replace(custom_box, **{key: 0})))
        self.assertEqual(effective_style(custom_box), effective_style(replace(custom_box, background_corner_radius_px=0)))
        for changes in ({"font_size": 24}, {"outline_enabled": False}, {"outline_color": "#123456"},
                        {"position_mode": "free"}, {"highlight_word_enabled": True},
                        {"position_mode": "free", "area_enabled": True}, {"background_enabled": True}):
            with self.subTest(changes=changes):
                self.assertFalse(uses_legacy_srt_style(replace(DEFAULTS, **changes)))
        for changes in ({"background_color": "#123456"}, {"background_opacity": 42}):
            self.assertNotEqual(effective_style(legacy_box), effective_style(replace(legacy_box, **changes)))

    def test_fill_region_strict_boolean_and_legacy_validation(self):
        custom = replace(DEFAULTS, background_geometry_mode="custom")
        for value in (0, 1, "true", "false", "", None, [], {}, 1.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "boolean"):
                    from_document({"schema_version": 8,
                                   "shorts": {**asdict(custom), "background_fill_region": value}})
                with self.assertRaisesRegex(ValueError, "boolean"):
                    replace(custom, background_fill_region=value)
        for background in (False, True):
            for area in (False, True):
                values = dict(background_enabled=background, area_enabled=area,
                              position_mode="free" if area else "historical", background_fill_region=True)
                with self.subTest(background=background, area=area):
                    with self.assertRaisesRegex(ValueError, "custom"):
                        replace(DEFAULTS, **values)
                    fields = {**asdict(DEFAULTS), **values}
                    with self.assertRaisesRegex(ValueError, "custom"):
                        from_document({"schema_version": 8, "shorts": fields})
                    saved = replace(custom, **values)
                    self.assertTrue(from_document(document(saved)).background_fill_region)

    def test_effective_fill_region_dormant_values_and_active_geometry(self):
        custom = replace(DEFAULTS, background_geometry_mode="custom", background_enabled=True,
                         background_padding_x_px=37, background_padding_y_px=4,
                         background_corner_radius_px=20, background_fill_region=True)
        # Retained fill=True without an area is inactive; padding remains effective.
        before = document(custom)
        dormant = effective_style(custom)
        self.assertFalse(dormant.background_fill_region)
        self.assertEqual(dormant.background_corner_radius_px, 0)
        self.assertEqual((dormant.background_padding_x_px, dormant.background_padding_y_px), (37, 4))
        self.assertEqual(dormant, effective_style(replace(custom, background_fill_region=False)))
        active = replace(custom, position_mode="free", area_enabled=True, width_percent=45, height_percent=20)
        effective = effective_style(active)
        self.assertTrue(effective.background_fill_region)
        self.assertEqual((effective.background_padding_x_px, effective.background_padding_y_px), (0, 0))
        self.assertEqual(effective.background_corner_radius_px, 20)
        for key in ("background_padding_x_px", "background_padding_y_px"):
            self.assertEqual(effective, effective_style(replace(active, **{key: 0})))
        self.assertNotEqual(effective, effective_style(replace(active, background_corner_radius_px=0)))
        for key in ("width_percent", "height_percent"):
            without_bg = replace(active, background_enabled=False)
            self.assertNotEqual(effective_style(without_bg), effective_style(replace(without_bg, **{key: 30})))
        disabled = replace(custom, background_enabled=False)
        self.assertEqual(effective_style(disabled), effective_style(DEFAULTS))
        self.assertTrue(uses_legacy_srt_style(disabled))
        self.assertFalse(uses_legacy_srt_style(replace(active, background_enabled=False)))  # Text area still active.
        self.assertEqual(document(custom), before)
        self.assertEqual(effective_style(effective), effective)
        self.assertEqual(effective_style(dormant), dormant)


class WindowTests(unittest.TestCase):
    def test_shadow_section_is_last_and_applies(self):
        self.assertEqual(self.window.sections[-1].cget("text"), "Тень под текстом")
        self.assertIs(self.window.field_widgets["shadow_size"].master, self.window.sections[-1])
        with patch("subs_settings_window.colorchooser.askcolor", return_value=((18, 52, 86), "#123456")):
            self.window.choose_color("shadow_color")
        self.window.variables["shadow_size"].set("2.5")
        self.window.variables["shadow_enabled"].set(True)
        self.window.apply()
        self.assertTrue(self.applied[-1].shadow_enabled)
        self.assertEqual(self.applied[-1].shadow_color, "#123456")
        self.assertEqual(self.applied[-1].shadow_size, 2.5)
        self.window.populate(DEFAULTS)
        self.assertEqual(self.window.current().shadow_size, 0)

    def test_shadow_checkbox_first_and_preserves_size_when_disabled(self):
        checkbox = self.window.field_widgets["shadow_enabled"]
        self.assertEqual(checkbox.winfo_class(), "TCheckbutton")
        self.assertEqual(int(checkbox.grid_info()["row"]), 0)
        self.assertFalse(self.window.current().shadow_enabled)
        self.window.variables["shadow_enabled"].set(True)
        self.assertEqual(self.window.current().shadow_size, 2)
        self.window.variables["shadow_size"].set("4.5")
        self.window.variables["shadow_color"].set("#123456")
        self.window.variables["shadow_enabled"].set(False)
        self.window.apply()
        self.assertFalse(self.applied[-1].shadow_enabled)
        self.assertEqual(self.applied[-1].shadow_size, 4.5)
        self.assertEqual(self.applied[-1].shadow_color, "#123456")
        self.window.variables["shadow_enabled"].set(True)
        self.assertEqual(self.window.current().shadow_size, 4.5)

    def test_text_color_picker_apply_and_defaults(self):
        with patch("subs_settings_window.colorchooser.askcolor", return_value=((18, 171, 52), "#12ab34")) as picker:
            self.window.choose_color("text_color")
        self.assertEqual(picker.call_args.kwargs["title"], "Цвет текста")
        self.assertEqual(self.window.current().text_color, "#12ab34")
        self.window.apply()
        self.assertEqual(self.applied[-1].text_color, "#12ab34")
        self.window.populate(DEFAULTS)
        self.assertEqual(self.window.variables["text_color"].get(), "#FFFFFF")

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

    def test_sections_contain_all_existing_fields(self):
        expected = (
            ("Шрифт и позиционирование", ("font", "font_size", "text_color", "position_mode", "x", "y")),
            ("Подложка субтитров", ("background_enabled", "background_color", "background_opacity",
                                    "background_padding_x_px", "background_padding_y_px", "area_enabled",
                                    "width_percent", "height_percent", "background_fill_region",
                                    "background_corner_radius_px")),
            ("Обводка и дополнительные эффекты", ("outline_enabled", "outline_color",
                                                     "highlight_word_enabled", "highlight_word_color")),
            ("Тень под текстом", ("shadow_enabled", "shadow_color", "shadow_size")),
        )
        self.assertEqual(len(self.window.sections), 4)
        self.assertEqual(set(self.window.variables), set(asdict(DEFAULTS)))
        self.assertEqual(set(self.window.field_widgets), set(asdict(DEFAULTS)) - {"background_geometry_mode"})
        for section, (title, keys) in zip(self.window.sections, expected):
            self.assertEqual(section.cget("text"), title)
            self.assertEqual({key for key, widget in self.window.field_widgets.items()
                               if widget.master == section}, set(keys))
            # Inspect actual grid rows, not dictionary/widget creation order.
            ordered = sorted(((int(widget.grid_info()["row"]), key)
                              for key, widget in self.window.field_widgets.items() if widget.master == section))
            self.assertEqual(ordered, list(enumerate(keys)))
            controls = [widget for widget in section.grid_slaves(column=1)
                        if widget != self.window.geometry_notice_label]
            self.assertEqual(len(controls), len(keys))
            self.assertEqual(len(section.grid_slaves(column=0)), len(keys) + (1 if section == self.window.sections[1] else 0))
        notice = self.window.geometry_notice_label
        self.assertIs(notice.master, self.window.sections[1])
        self.assertEqual(int(notice.grid_info()["row"]), len(expected[1][1]))
        self.assertEqual(self.window.sections[1].grid_slaves(row=10), [notice])
        self.assertEqual(self.window.field_widgets["background_fill_region"].cget("text"), "")
        label = self.window.sections[1].grid_slaves(row=8, column=0)
        self.assertEqual([widget.cget("text") for widget in label], ["Во всю область"])
        self.assertEqual([button.cget("text") for button in self.window.actions.winfo_children()],
                         ["Save", "Load", "Set Default", "Применить"])

    def test_moved_area_controls_keep_handlers_when_background_is_disabled(self):
        for key in ("width_percent", "height_percent"):
            with self.subTest(key=key):
                self.window.populate(DEFAULTS)
                self.assertTrue(self.window.field_widgets[key].instate(["!disabled"]))
                self.window.variables[key].set("45.5")
                current = self.window.current()
                self.assertEqual(getattr(current, key), 45.5)
                self.assertTrue(current.area_enabled)
                self.assertEqual(current.position_mode, "free")
                self.assertFalse(current.background_enabled)
                self.assertEqual(current.background_geometry_mode, "legacy")
                self.window.variables["position_mode"].set("historical")
                self.assertFalse(self.window.current().area_enabled)
                self.assertEqual(getattr(self.window.current(), key), 45.5)
        self.window.populate(DEFAULTS)
        self.assertTrue(self.window.field_widgets["area_enabled"].instate(["!disabled"]))
        self.window.variables["area_enabled"].set(True)
        self.assertEqual(self.window.current().position_mode, "free")
        self.assertFalse(self.window.current().background_enabled)

    def test_fill_region_availability_matrix_preserves_every_value(self):
        for background in (False, True):
            for area in (False, True):
                for fill in (False, True):
                    with self.subTest(background=background, area=area, fill=fill):
                        saved = replace(DEFAULTS, background_enabled=background, area_enabled=area,
                                        position_mode="free" if area else "historical",
                                        background_geometry_mode="custom", background_fill_region=fill,
                                        background_padding_x_px=37.5, background_padding_y_px=4.25,
                                        background_corner_radius_px=20)
                        self.window.populate(saved)
                        active = background and area and fill
                        expected = {"background_fill_region": background and area,
                                    "background_padding_x_px": background and not active,
                                    "background_padding_y_px": background and not active,
                                    "background_corner_radius_px": active}
                        for key, enabled in expected.items():
                            self.assertEqual(self.window.field_widgets[key].instate(["!disabled"]), enabled)
                        for key in ("area_enabled", "width_percent", "height_percent"):
                            self.assertTrue(self.window.field_widgets[key].instate(["!disabled"]))
                        checkbox = self.window.field_widgets["background_fill_region"]
                        self.assertEqual(checkbox.instate(["selected"]), fill)
                        self.assertEqual(self.window.current(), saved)
                        if fill:
                            self.assertNotIn("пока не поддерживается", self.window.geometry_notice.get())
                            self.assertNotIn("прежнюю геометрию", self.window.geometry_notice.get())
                            if not active:
                                self.assertIn("Флажок сохранён", self.window.geometry_notice.get())
                        self.assertNotIn("не применяется", self.window.geometry_notice.get())
                        if not fill or active:
                            self.assertEqual(self.window.geometry_notice.get(), "")

    def test_fill_region_toggle_retains_custom_and_dormant_choice(self):
        fields = ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px")
        saved_values = (37.5, 4.25, 20)
        self.window.populate(replace(DEFAULTS, **dict(zip(fields, saved_values))))
        checkbox = self.window.field_widgets["background_fill_region"]
        checkbox.invoke()  # Disabled with no background/area: must not change the flag.
        self.assertFalse(self.window.current().background_fill_region)
        self.window.variables["background_enabled"].set(True)
        checkbox.invoke()  # Still disabled until an area exists.
        self.assertFalse(self.window.current().background_fill_region)
        self.window.variables["area_enabled"].set(True)
        self.assertEqual(self.window.current().background_geometry_mode, "legacy")
        checkbox.invoke()
        self.assertTrue(self.window.current().background_fill_region)
        self.assertEqual(self.window.current().background_geometry_mode, "custom")
        self.assertTrue(self.window.field_widgets["background_corner_radius_px"].instate(["!disabled"]))
        for switch in ("background_enabled", "area_enabled"):
            self.window.variables[switch].set(False)
            self.assertTrue(self.window.current().background_fill_region)
            self.assertTrue(checkbox.instate(["disabled", "selected"]))
            self.window.variables[switch].set(True)
            self.assertTrue(checkbox.instate(["!disabled", "selected"]))
            self.assertTrue(self.window.field_widgets["background_corner_radius_px"].instate(["!disabled"]))
            self.assertEqual(tuple(getattr(self.window.current(), name) for name in fields), saved_values)
        self.window.variables["position_mode"].set("historical")
        self.assertFalse(self.window.current().area_enabled)
        self.assertTrue(self.window.current().background_fill_region)
        self.assertTrue(self.window.field_widgets["background_padding_x_px"].instate(["!disabled"]))
        self.window.variables["height_percent"].set("25")  # Existing handler restores area/free.
        self.assertTrue(checkbox.instate(["!disabled", "selected"]))
        checkbox.invoke()
        self.assertFalse(self.window.current().background_fill_region)
        self.assertEqual(self.window.current().background_geometry_mode, "custom")
        for key in fields[:2]:
            self.assertTrue(self.window.field_widgets[key].instate(["!disabled"]))
        self.assertTrue(self.window.field_widgets["background_corner_radius_px"].instate(["disabled"]))
        self.assertEqual(tuple(getattr(self.window.current(), name) for name in fields), saved_values)
        for key, value in (("background_color", "#123456"), ("background_opacity", "42"),
                           ("width_percent", "45"), ("height_percent", "30")):
            self.window.populate(DEFAULTS)
            self.window.variables[key].set(value)
            self.assertEqual(self.window.current().background_geometry_mode, "legacy")
        self.window.populate(replace(DEFAULTS, background_geometry_mode="custom", background_fill_region=True,
                                     background_enabled=True, area_enabled=True, position_mode="free"))
        next(button for button in self.window.actions.winfo_children() if button.cget("text") == "Set Default").invoke()
        self.assertEqual(self.window.current(), DEFAULTS)
        self.assertFalse(self.window.current().background_fill_region)

    def test_geometry_edits_select_custom_and_disabled_controls_retain_values(self):
        fields = ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px")
        legacy = replace(DEFAULTS, background_padding_x_px=12, background_padding_y_px=6,
                         background_corner_radius_px=3)
        for key in fields:
            with self.subTest(key=key):
                self.window.populate(legacy)
                self.assertEqual(self.window.current(), legacy)
                self.assertEqual(self.window.geometry_notice.get(), "")
                for name in fields:
                    self.assertTrue(self.window.field_widgets[name].instate(["disabled"]))
                self.window.variables["background_enabled"].set(True)
                self.assertEqual(self.window.current().background_geometry_mode, "legacy")
                self.window.variables["background_color"].set("#123456")
                self.assertEqual(self.window.current().background_geometry_mode, "legacy")
                if key == "background_corner_radius_px":
                    self.assertTrue(self.window.field_widgets[key].instate(["disabled"]))
                    self.window.variables["area_enabled"].set(True)
                    self.window.field_widgets["background_fill_region"].invoke()
                self.assertTrue(self.window.field_widgets[key].instate(["!disabled"]))
                self.window.variables[key].set("37.5")
                self.assertEqual(self.window.current().background_geometry_mode, "custom")
                self.assertEqual(getattr(self.window.current(), key), 37.5)
                self.assertEqual(self.window.geometry_notice.get(), "")
                before = self.window.current()
                self.window.variables["background_enabled"].set(False)
                self.window.field_widgets[key].insert(0, "999")  # Disabled Entry rejects user edits.
                self.assertEqual(self.window.current(), replace(before, background_enabled=False))
                self.window.variables["background_enabled"].set(True)
                self.assertEqual(self.window.current(), before)
        self.window.populate(DEFAULTS)
        self.window.variables["background_enabled"].set(True)
        self.window.variables["background_corner_radius_px"].set("0.0")
        self.assertEqual(self.window.current().background_geometry_mode, "custom")

    def test_geometry_uses_model_validation_and_blocks_invalid_apply(self):
        for key in ("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px"):
            for value in ("-1", "512.1", "nan", "inf", "not a number"):
                with self.subTest(key=key, value=value):
                    self.window.populate(DEFAULTS)
                    self.window.variables["background_enabled"].set(True)
                    self.window.variables[key].set(value)
                    with self.assertRaises(ValueError):
                        self.window.current()
                    with patch("subs_settings_window.messagebox.showerror") as error:
                        self.window.apply()
                    error.assert_called_once()
                    self.assertEqual(self.applied, [])

    def test_old_presets_load_as_legacy_until_geometry_is_edited(self):
        geometry = {"background_padding_x_px", "background_padding_y_px", "background_corner_radius_px",
                    "background_geometry_mode"}
        added = {"outline_enabled", "outline_color", "area_enabled", "width_percent", "height_percent"}
        highlight = {"highlight_word_enabled", "highlight_word_color"}
        style = replace(DEFAULTS, background_enabled=True, font_size=24)
        with tempfile.TemporaryDirectory() as directory:
            for version in (1, 2, 3, 4):
                with self.subTest(version=version):
                    missing = ({"background_fill_region", "text_color", "shadow_color", "shadow_size", "shadow_enabled"} | (geometry if version < 4 else set())
                               | (highlight if version < 3 else set()) | (added if version < 2 else set()))
                    fields = {key: value for key, value in asdict(style).items() if key not in missing}
                    path = Path(directory) / f"v{version}.json"
                    original = json.dumps({"schema_version": version, "shorts": fields}).encode("utf-8")
                    path.write_bytes(original)
                    self.window.populate(replace(style, background_geometry_mode="custom", background_padding_x_px=20))
                    with patch("subs_settings_window.filedialog.askopenfilename", return_value=str(path)):
                        self.window.load()
                    self.assertEqual(self.window.current(), style)
                    self.assertEqual(self.window.geometry_notice.get(), "")
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(self.applied, [])
                    self.window.variables["background_opacity"].set("42")
                    self.assertEqual(self.window.current().background_geometry_mode, "legacy")
                    self.window.variables["background_padding_y_px"].set("4.25")
                    self.assertEqual(self.window.current().background_geometry_mode, "custom")
        defaults_button = next(button for button in self.window.actions.winfo_children()
                               if button.cget("text") == "Set Default")
        defaults_button.invoke()
        self.assertEqual(self.window.current(), DEFAULTS)
        self.assertEqual(self.window.geometry_notice.get(), "")
        self.assertEqual(self.applied, [])

    def test_scroll_resize_and_footer_at_windows_scalings(self):
        original = float(self.root.tk.call("tk", "scaling"))
        self.window.close()
        self.root.deiconify()
        self.root.update()
        try:
            for percent in (100, 125, 150):
                with self.subTest(scaling=percent):
                    self.root.tk.call("tk", "scaling", percent / 100 * 96 / 72)
                    active = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                                     background_padding_x_px=24, background_padding_y_px=12,
                                     background_corner_radius_px=20, background_fill_region=True,
                                     area_enabled=True, position_mode="free")
                    self.window = SubsSettingsWindow(self.root, active, self.applied.append)
                    self.assertEqual(self.window.geometry_notice.get(), "")
                    # Keep layout tests independent of external FFmpeg availability.
                    self.window.top.after_cancel(self.window.preview_timer)
                    self.window.preview_timer = None
                    self.window.top.deiconify()
                    self.root.update_idletasks()
                    self.window.top.geometry(f"{self.window.top.winfo_width()}x420")
                    self.root.update()
                    radius = self.window.field_widgets["background_corner_radius_px"]
                    self.assertGreaterEqual(self.window.geometry_notice_label.winfo_rooty(),
                                            radius.winfo_rooty() + radius.winfo_height())
                    canvas = self.window.settings_canvas
                    self.assertLess(canvas.yview()[1], 1)
                    canvas.yview_moveto(1)
                    self.root.update_idletasks()
                    last = self.window.field_widgets["shadow_size"]
                    self.assertGreaterEqual(last.winfo_rooty(), canvas.winfo_rooty())
                    self.assertLess(last.winfo_rooty() + last.winfo_height(),
                                    canvas.winfo_rooty() + canvas.winfo_height())
                    before = canvas.yview()[0]
                    last.event_generate("<MouseWheel>", delta=120)
                    self.assertLess(canvas.yview()[0], before)
                    first = self.window.field_widgets["font"]
                    first.event_generate("<FocusIn>")
                    self.root.update_idletasks()
                    self.assertGreaterEqual(first.winfo_rooty(), canvas.winfo_rooty())
                    for widget in self.window.field_widgets.values():
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                             canvas.winfo_rootx() + canvas.winfo_width())
                    for button in self.window.actions.winfo_children():
                        self.assertTrue(button.winfo_ismapped())
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                             self.window.top.winfo_rooty() + self.window.top.winfo_height())
                    self.assertAlmostEqual(float(canvas.itemcget(self.window.settings_item, "width")),
                                           canvas.winfo_width(), delta=1)
                    self.window.image = tk.PhotoImage(width=270, height=480, master=self.window.top)
                    self.window._display_preview()
                    self.assertLessEqual(self.window.display_image.height(), self.window.preview_canvas.winfo_height())
                    self.assertEqual(self.window.display_image.width() * 16,
                                     self.window.display_image.height() * 9)
                    old_height = canvas.winfo_height()
                    self.window.top.geometry(f"{self.window.top.winfo_width() + 80}x650")
                    self.root.update()
                    self.assertGreater(canvas.winfo_height(), old_height)
                    self.assertAlmostEqual(float(canvas.itemcget(self.window.settings_item, "width")),
                                           canvas.winfo_width(), delta=1)
                    self.window.close()
        finally:
            self.root.tk.call("tk", "scaling", original)

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
            self.window.variables["background_enabled"].set(True)
            self.window.variables["background_padding_x_px"].set("37.5")
            self.window.variables["background_padding_y_px"].set("4.25")
            self.window.variables["background_corner_radius_px"].set("20")
            self.window.field_widgets["background_fill_region"].invoke()
            exported = self.window.current()
            self.assertTrue(exported.background_fill_region)
            with patch("subs_settings_window.filedialog.asksaveasfilename", return_value=path), \
                    patch("subs_settings_window.messagebox.showinfo"):
                self.window.save()
            self.assertFalse(self.applied)
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["schema_version"], 8)
            self.window.populate(DEFAULTS)
            with patch("subs_settings_window.filedialog.askopenfilename", return_value=path):
                self.window.load()
            self.assertEqual(self.window.current().font_size, 24)
            self.assertEqual(self.window.current(), exported)
            self.assertEqual(self.window.current().background_geometry_mode, "custom")
            self.window.apply()
            self.assertEqual(self.applied, [exported])
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

    def test_measurement_failure_is_visible_without_changing_settings_or_fallback(self):
        style = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                        background_padding_x_px=24, background_corner_radius_px=20)
        self.window.populate(style)
        self.assertEqual(self.window.geometry_notice.get(), "")
        self.window.top.after_cancel(self.window.preview_timer)
        self.window.preview_timer = None
        with patch("subtitle_background._measure_text", side_effect=ValueError("Не удалось измерить текст")), \
                patch("subs_settings_window.build_ass") as legacy_fallback:
            self.window.start_preview()
            deadline = time.monotonic() + 2
            while self.window.running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(self.window.running)
        self.assertIn("Предпросмотр недоступен", self.window.status.get())
        self.assertIn("Не удалось измерить текст", self.window.status.get())
        self.assertEqual(self.window.current(), style)
        self.assertFalse(self.applied)
        legacy_fallback.assert_not_called()

    def test_close_during_measurement_does_not_block_or_touch_destroyed_widgets(self):
        entered, release = threading.Event(), threading.Event()

        def slow_measure(*args):
            entered.set()
            if not release.wait(2):
                raise ValueError("Test measurement timed out")
            return (100, 200, 400, 500)

        self.window.populate(replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom"))
        self.window.top.after_cancel(self.window.preview_timer)
        self.window.preview_timer = None
        with patch("subtitle_background._measure_text", side_effect=slow_measure), \
                patch("subs_settings_window.subprocess.run") as final_render:
            try:
                self.window.start_preview()
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                self.window.close()
                self.assertLess(time.monotonic()-started, .5)
                self.assertFalse(self.window.top.winfo_exists())
            finally:
                release.set()
            _, image, error = self.window.results.get(timeout=2)
        self.assertIsNone(image)
        self.assertIn("окно закрыто", error)
        final_render.assert_not_called()
        self.assertEqual(self.root.tk.call("after", "info"), "")

    def test_stale_measurement_result_is_discarded(self):
        self.window.status.set("current status")
        self.window.results.put((self.window.revision-1, None, "stale error"))
        self.window.top.after_cancel(self.window.timer)
        self.window.timer = None
        with patch.object(self.window, "start_preview") as restart:
            self.window.poll()
        restart.assert_called_once()
        self.assertEqual(self.window.status.get(), "current status")

    def test_full_region_preview_error_keeps_settings_and_shows_error(self):
        settings = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                           background_fill_region=True, area_enabled=True, position_mode="free",
                           background_corner_radius_px=40)
        self.window.populate(settings)
        self.window.top.after_cancel(self.window.preview_timer)
        self.window.preview_timer = None
        result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"synthetic libass failure")
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")), \
                patch("subs_settings_window.subprocess.run", return_value=result) as run:
            self.window.start_preview()
            deadline = time.monotonic()+2
            while self.window.running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(self.window.running)
        run.assert_called_once()
        self.assertIn("Предпросмотр недоступен", self.window.status.get())
        self.assertIn("synthetic libass failure", self.window.status.get())
        self.assertEqual(self.window.current(), settings)
        self.assertFalse(self.applied)

    def test_close_during_full_region_final_render_does_not_wait_or_display_late_image(self):
        entered, release = threading.Event(), threading.Event()
        ppm = b"P6\n1 1\n255\n\xff\x00\x00"

        def slow_render(command, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Test render timed out")
            return subprocess.CompletedProcess(command, 0, stdout=ppm, stderr=b"")

        self.window.populate(replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                                     background_fill_region=True, area_enabled=True, position_mode="free"))
        self.window.top.after_cancel(self.window.preview_timer)
        self.window.preview_timer = None
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")), \
                patch("subs_settings_window.subprocess.run", side_effect=slow_render) as run:
            try:
                self.window.start_preview()
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                self.window.close()
                self.assertLess(time.monotonic()-started, .5)
                self.assertFalse(self.window.top.winfo_exists())
            finally:
                release.set()
            _, image, error = self.window.results.get(timeout=2)
        run.assert_called_once()
        self.assertEqual(image, ppm)
        self.assertIsNone(error)
        self.assertFalse(hasattr(self.window, "image"))
        self.assertEqual(self.root.tk.call("after", "info"), "")

    def test_active_settings_survive_restart(self):
        import review_ui
        self.window.close()
        with tempfile.TemporaryDirectory() as directory, patch.object(review_ui, "DATA", Path(directory)):
            app = review_ui.HighlightApp(self.root)
            style = replace(DEFAULTS, background_enabled=True, background_opacity=42,
                            outline_enabled=False, outline_color="#AA5522", position_mode="free",
                            area_enabled=True, width_percent=45.5, height_percent=20.25,
                            background_padding_x_px=37.5, background_padding_y_px=4.25,
                            background_corner_radius_px=20, background_geometry_mode="custom", background_fill_region=True)
            app.apply_subtitle_settings(style)
            self.assertEqual(load_settings(Path(directory) / "subtitle_settings.json"), style)
            second_root = tk.Toplevel(self.root)
            try:
                second = review_ui.HighlightApp(second_root)
                self.assertEqual(second.subtitle_settings, style)
                second.open_subs_settings()
                self.assertEqual(second.subs_window.current(), style)
                self.assertEqual(second.subs_window.geometry_notice.get(), "")
            finally:
                second_root.destroy()


class PreviewSelectionTests(unittest.TestCase):
    def test_fill_region_preview_changes_background_but_preserves_text(self):
        from subtitle_background import prepare_ass
        settings = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                           background_padding_x_px=37, background_padding_y_px=4, background_corner_radius_px=20,
                           area_enabled=True, position_mode="free")
        contents = []

        def prepare(*args, **kwargs):
            result = prepare_ass(*args, **kwargs)
            contents.append(result)
            return result

        result = subprocess.CompletedProcess([], 0, stdout=b"preview", stderr=b"")
        with patch("subtitle_background._measure_text", return_value=(100, 200, 300, 400)), \
                patch("subs_settings_window.prepare_ass", side_effect=prepare), \
                patch("subs_settings_window.subprocess.run", return_value=result):
            render_preview(settings)
            render_preview(replace(settings, background_fill_region=True))
        self.assertEqual(len(contents), 2)
        self.assertNotEqual(contents[0], contents[1])
        events = [[line for line in content.splitlines() if line.startswith("Dialogue: 1")] for content in contents]
        self.assertEqual(events[0], events[1])
        text_box = next(line for line in contents[0].splitlines() if line.startswith("Dialogue: 0"))
        area_box = next(line for line in contents[1].splitlines() if line.startswith("Dialogue: 0"))
        self.assertNotIn(" b ", text_box)
        self.assertIn(r"\clip(", text_box)
        self.assertIn(" b ", area_box)
        self.assertNotIn(r"\clip(", area_box)

    def test_full_region_preview_runs_only_final_ffmpeg_without_measurement_or_cache(self):
        from subtitle_background import MeasurementCache
        style = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom",
                        background_fill_region=True, area_enabled=True, position_mode="free")
        cache = MeasurementCache()
        result = subprocess.CompletedProcess([], 0, stdout=b"preview", stderr=b"")
        with patch("subtitle_background._measure_text", side_effect=AssertionError("Unexpected measurement")), \
                patch.object(cache, "get_or_measure", side_effect=AssertionError("Unexpected cache lookup")), \
                patch("subtitle_background.MeasurementCache", side_effect=AssertionError("Unexpected cache creation")), \
                patch("subs_settings_window.subprocess.run", return_value=result) as run:
            self.assertEqual(render_preview(style, cache), b"preview")
        run.assert_called_once()
        self.assertIn("image2pipe", run.call_args.args[0])
        self.assertNotIn("rawvideo", run.call_args.args[0])

    def test_inactive_geometry_keeps_historical_srt_path(self):
        inactive = replace(DEFAULTS, background_padding_x_px=37.5, background_padding_y_px=4.25,
                           background_corner_radius_px=20, background_geometry_mode="custom",
                           background_color="#123456", background_opacity=42)
        result = subprocess.CompletedProcess([], 0, stdout=b"preview", stderr=b"")
        with patch("subs_settings_window.subprocess.run", return_value=result) as run, \
                patch("subs_settings_window.build_ass") as ass:
            self.assertEqual(render_preview(inactive), b"preview")
        graph = run.call_args.args[0][run.call_args.args[0].index("-vf") + 1]
        self.assertIn("preview.srt", graph)
        self.assertIn("force_style=", graph)
        ass.assert_not_called()

    def test_legacy_background_keeps_existing_ass_geometry(self):
        legacy = replace(DEFAULTS, background_enabled=True)
        inactive = replace(legacy, background_padding_x_px=37.5,
                         background_padding_y_px=4.25, background_corner_radius_px=20)
        contents = []

        def capture(command, **kwargs):
            graph = command[command.index("-vf") + 1]
            self.assertIn("preview.ass", graph)
            self.assertNotIn("force_style=", graph)
            return subprocess.CompletedProcess(command, 0, stdout=b"preview", stderr=b"")

        original = build_ass

        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            contents.append(result)
            return result

        with patch("subs_settings_window.subprocess.run", side_effect=capture), \
                patch("subs_settings_window.build_ass", side_effect=build):
            render_preview(legacy)
            render_preview(inactive)
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[0], contents[1])
        self.assertIn("Style: Box,", contents[0])

    def test_custom_preview_uses_preparation_and_shared_cache(self):
        from subtitle_background import MeasurementCache
        style = replace(DEFAULTS, background_enabled=True, background_geometry_mode="custom")
        cache = MeasurementCache()
        result = subprocess.CompletedProcess([], 0, stdout=b"preview", stderr=b"")
        with patch("subs_settings_window.prepare_ass", return_value="vector ASS") as prepare, \
                patch("subs_settings_window.subprocess.run", return_value=result) as run, \
                patch("subs_settings_window.build_ass") as legacy:
            self.assertEqual(render_preview(style, measurement_cache=cache), b"preview")
        self.assertEqual(prepare.call_args.args[1:4], (style, 1080, 1920))
        self.assertIs(prepare.call_args.kwargs["cache"], cache)
        graph = run.call_args.args[0][run.call_args.args[0].index("-vf")+1]
        self.assertIn("preview.ass", graph)
        self.assertNotIn("force_style=", graph)
        legacy.assert_not_called()

    def test_custom_disabled_preview_does_not_prepare_or_measure(self):
        style = replace(DEFAULTS, font_size=24, background_geometry_mode="custom")
        result = subprocess.CompletedProcess([], 0, stdout=b"preview", stderr=b"")
        with patch("subs_settings_window.prepare_ass") as prepare, \
                patch("subs_settings_window.subprocess.run", return_value=result), \
                patch("subtitle_background._measure_text") as measure:
            render_preview(style)
        prepare.assert_not_called()
        measure.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class FFmpegTests(unittest.TestCase):
    def test_custom_live_preview_changes_geometry_and_reuses_measurements(self):
        from subtitle_background import MeasurementCache, _measure_text
        settings = replace(DEFAULTS, position_mode="free", x=50, y=50, background_enabled=True,
                           background_geometry_mode="custom", background_padding_x_px=24,
                           background_padding_y_px=12, background_color="#00AA44")
        cache = MeasurementCache()
        with patch("subtitle_background._measure_text", wraps=_measure_text) as measure:
            original = render_preview(settings, cache)
            for changes in ({"background_padding_x_px": 100}, {"background_padding_y_px": 100},
                            {"background_color": "#AA0044"},
                            {"background_opacity": 30}):
                with self.subTest(changes=changes):
                    self.assertNotEqual(original, render_preview(replace(settings, **changes), cache))
            self.assertEqual(original, render_preview(replace(settings, background_corner_radius_px=60), cache))
            full = replace(settings, area_enabled=True, background_fill_region=True, width_percent=50,
                           height_percent=25, background_corner_radius_px=60)
            self.assertNotEqual(render_preview(full, cache),
                                render_preview(replace(full, background_corner_radius_px=0), cache))
            self.assertEqual(measure.call_count, 1)
            render_preview(replace(settings, font_size=24), cache)
            self.assertEqual(measure.call_count, 2)
        inactive = replace(settings, background_enabled=False, position_mode="historical",
                           background_padding_x_px=100, background_padding_y_px=100,
                           background_corner_radius_px=60)
        self.assertEqual(render_preview(DEFAULTS), render_preview(inactive))

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
