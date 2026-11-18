"""End-to-end export checks: only 1.5-second synthetic video/audio in TEMP."""
import copy
import json
import shutil
import statistics
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import renderer
from shorts_settings import ShortsSettings
from smart_crop_storage import create_settings, save_settings, settings_path
from subtitle_background import _measure_text, prepare_ass
from subtitle_settings import DEFAULTS, subtitle_area


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe unavailable")
class SyntheticCustomExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="custom_shorts_export_")
        cls.directory = Path(cls.temp.name)
        cls.source = cls.directory / "source.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=8:d=1.5",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
                        "-t", "1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.source)],
                       check=True, capture_output=True, timeout=30)
        cls.metrics = {}

    @classmethod
    def tearDownClass(cls):
        summary = {key: {"exports": len(values), "mean_export_ms": round(statistics.mean(v[0] for v in values), 1),
                         "mean_measurement_ms": round(statistics.mean(v[1] for v in values), 1)}
                   for key, values in cls.metrics.items()}
        print("\nSYNTHETIC_EXPORT_TIMINGS " + json.dumps(summary, sort_keys=True))
        cls.temp.cleanup()

    def read_video(self, path, width=180, height=320, frame=None):
        filters = []
        if frame is not None:
            filters.append(f"select=eq(n\\,{frame})")
        filters.append(f"scale={width}:{height}")
        command = ["ffmpeg", "-v", "error", "-i", str(path), "-vf", ",".join(filters)]
        if frame is not None:
            command += ["-frames:v", "1"]
        command += ["-fps_mode", "passthrough", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        return subprocess.run(command, check=True, capture_output=True, timeout=30).stdout

    def reference(self, path, srt, style, width, height, cues):
        path.write_text(prepare_ass(srt, style, width, height, cues), encoding="utf-8-sig")
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=8:d=1.5",
             "-vf", f"subtitles='{renderer._filtergraph_escape_path(path)}',scale=180:320", "-frames:v", "12",
             "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            check=True, capture_output=True, timeout=30).stdout

    def red_bounds(self, pixels, width, height):
        mask = bytes(int(red > 60 and red > green*1.8 and red > blue*1.8)
                     for red, green, blue in zip(pixels[0::3], pixels[1::3], pixels[2::3]))
        rows = [y for y in range(height) if any(mask[y*width:(y+1)*width])]
        self.assertTrue(rows, "Background is missing from decoded MP4")
        left = min(width-len(mask[y*width:(y+1)*width].lstrip(b"\0")) for y in rows)
        right = max(len(mask[y*width:(y+1)*width].rstrip(b"\0")) for y in rows)
        return (left, rows[0], right, rows[-1]+1), mask

    def test_real_mp4_layouts_resolutions_geometry_timings_and_audio(self):
        first = ["Это", "тест", "для", "фона", "и", "слов"]
        words = [{"word": word, "start": .25+i/24, "end": .25+(i+1)/24} for i, word in enumerate(first)]
        words += [{"word": "Снова", "start": 1, "end": 1.125}, {"word": "текст", "start": 1.125, "end": 1.25}]
        transcript = {"segments": [{"start": .25, "end": 1.25, "words": words}]}
        references = {}
        for resolution in ("720p", "1080p"):
            export = ShortsSettings(resolution=resolution, compression="compact")
            width, height = export.output_size(320, 180)
            for fill in (False, True):
                style = replace(DEFAULTS, font_size=12, background_enabled=True, background_geometry_mode="custom",
                                position_mode="free", x=50, y=50, area_enabled=fill, background_fill_region=fill,
                                width_percent=50, height_percent=25, background_color="#E02020",
                                background_padding_x_px=512 if fill else 24, background_padding_y_px=512 if fill else 12,
                                background_corner_radius_px=60, highlight_word_enabled=True)
                for layout in ("ordinary", "CROP", "FIT_BLUR", "mixed", "smart", "smart_mixed"):
                    with self.subTest(resolution=resolution, fill=fill, layout=layout):
                        job = self.directory / f"{resolution}_{fill}_{layout}"
                        job.mkdir()
                        clip = {"candidate_id": "sc001", "start": 0, "end": 1.5, "title": "synthetic",
                                "status": "approved", "render_settings": {"subtitles_enabled": True}}
                        if layout in ("CROP", "FIT_BLUR", "mixed", "smart_mixed"):
                            interval = {"start": 0, "end": 1.5, "mode": "CROP" if layout == "CROP" else "FIT_BLUR"}
                            if layout in ("mixed", "smart_mixed"):
                                interval.update(start=.5, end=1)
                            clip["render_settings"]["framing"] = {"schema_version": 1, "default_mode": "CROP",
                                                                 "intervals": [interval]}
                        clip["approval_key"] = renderer.shorts_approval_key(clip)
                        report = {"signature": "test", "shorts_candidates": [clip],
                                  "profiles": {"shorts": {"min_duration_sec": .1, "max_duration_sec": 3}}}
                        if layout.startswith("smart"):
                            segments = [{"start": 0, "end": 1.5, "crop": {"x": .25, "y": 0, "width": .25, "height": 1}}]
                            if layout == "smart_mixed":
                                segments = [{"start": 0, "end": .75, "crop": {"x": 0, "y": 0, "width": .25, "height": 1}},
                                            {"start": .75, "end": 1.5, "crop": {"x": .5, "y": .25, "width": .125, "height": .5}}]
                            save_settings(create_settings("sc001", 0, 1.5, 320, 180, segments), settings_path(job, "test", "sc001"))
                        before = copy.deepcopy(report)
                        measurements, commands, ass_contents = [], [], []
                        original_run = subprocess.run

                        def measure(*args):
                            started = time.perf_counter()
                            result = _measure_text(*args)
                            measurements.append((time.perf_counter()-started)*1000)
                            return result

                        def run(command, **kwargs):
                            if command[0] == "ffmpeg" and str(command[-1]).endswith(".partial.mp4"):
                                commands.append(command)
                                ass_contents.extend(path.read_text(encoding="utf-8-sig") for path in job.rglob("subs_*.ass"))
                            return original_run(command, **kwargs)

                        started = time.perf_counter()
                        with patch("subtitle_background._measure_text", side_effect=measure) as measurement, \
                                patch("renderer.subprocess.run", side_effect=run):
                            outputs = renderer.render_shorts_approved({"source_path": str(self.source)}, report, job,
                                lambda _: None, transcript=transcript, subtitle_settings=style, export_settings=export)
                        elapsed = (time.perf_counter()-started)*1000
                        self.metrics.setdefault(f"{resolution}_{'region' if fill else 'text'}", []).append((elapsed, sum(measurements)))
                        self.assertEqual(measurement.call_count, 0 if fill else 2)
                        self.assertEqual(report, before)
                        self.assertEqual(len(outputs), 1)
                        output = Path(outputs[0])
                        self.assertEqual(len(commands), 1)
                        command = commands[0]
                        graph_option = "-filter_complex" if layout in ("FIT_BLUR", "mixed", "smart_mixed") else "-vf"
                        graph = command[command.index(graph_option)+1]
                        self.assertEqual(graph.count("subtitles="), 1)
                        if graph_option == "-filter_complex":
                            self.assertIn("[framing_composed]subtitles=", graph)
                        self.assertEqual(len(ass_contents), 1)
                        shapes = [line for line in ass_contents[0].splitlines() if line.startswith("Dialogue: 0")]
                        self.assertEqual(len(shapes), 2)
                        self.assertTrue(all((" b " in shape) == fill for shape in shapes))
                        self.assertIn("0:00:00.25,0:00:00.50", shapes[0])
                        self.assertIn("0:00:01.00,0:00:01.25", shapes[1])
                        media = renderer._probe_basic(output)
                        self.assertEqual((media["width"], media["height"]), (width, height))
                        self.assertAlmostEqual(media["duration_sec"], 1.5, delta=.15)
                        self.assertTrue(media["has_audio"])
                        audio = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a:0",
                            "-frames:a", "2", "-f", "s16le", "pipe:1"], check=True, capture_output=True, timeout=20).stdout
                        self.assertTrue(any(audio), "Audio was lost or silenced")
                        decoded = self.read_video(output)
                        stride = 180*320*3
                        self.assertEqual(len(decoded), 12*stride)
                        for frame in (0, 1, 4, 5, 6, 7, 10, 11):
                            self.assertLessEqual(max(decoded[frame*stride:(frame+1)*stride]), 12,
                                                 f"Subtitle remains outside a cue at frame {frame}")
                        for frame in (2, 3, 8, 9):
                            self.red_bounds(decoded[frame*stride:(frame+1)*stride], 180, 320)
                        self.assertNotEqual(decoded[2*stride:3*stride], decoded[3*stride:4*stride])  # Highlight changes.
                        srt, cues = renderer.build_subtitles_srt(transcript, 0, 1.5, return_cues=True)
                        key = (resolution, fill)
                        if key not in references:
                            references[key] = self.reference(job / "reference.ass", srt, style, width, height, cues)
                        reference = references[key]
                        self.assertEqual(len(reference), len(decoded))
                        mean_error = sum(abs(a-b) for a, b in zip(reference, decoded))/len(decoded)
                        self.assertLess(mean_error, 4, "Encoded MP4 does not match the shared ASS geometry")
                        # Inspect full-resolution decoded bounds, not only a thumbnail.
                        pixels = self.read_video(output, width, height, frame=2)
                        bounds, mask = self.red_bounds(pixels, width, height)
                        if fill:
                            l, t, r, b = subtitle_area(style)
                            expected = (l*width/384, t*height/288, r*width/384, b*height/288)
                            self.assertLessEqual(max(abs(a-b) for a, b in zip(bounds, expected)), 4)
                            self.assertFalse(mask[(round(expected[1])+3)*width+round(expected[0])+3])
                            self.assertTrue(mask[(round(expected[1])+3)*width+round((expected[0]+expected[2])/2)])
                        else:
                            self.assertTrue(mask[(bounds[1]+4)*width+bounds[0]+4], "Text-sized background unexpectedly rounded")
                        self.assertFalse(list(job.rglob("subs_*")))
                        self.assertFalse(list(job.rglob("*.partial.mp4")))
                        metadata = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
                        self.assertEqual(metadata["subtitles"]["style"]["shorts"]["background_corner_radius_px"], 60)


if __name__ == "__main__":
    unittest.main()
