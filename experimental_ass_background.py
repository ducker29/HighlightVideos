r"""Standalone synthetic libass experiment; never imported by the application.

Run: .venv\Scripts\python.exe experimental_ass_background.py --keep
Requires FFmpeg with libass and NumPy (already present in the development venv).
All generated files go into a new temporary directory, including with --keep.
The existing subtitle generator is only used to obtain representative text events.
No application settings, reports, sources or caches are read or written.
"""
import argparse
import json
import math
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from subtitle_settings import DEFAULTS, ass_color, build_ass, subtitle_area


@dataclass(frozen=True)
class Sample:
    name: str
    text: str
    size: float = 18
    outline: bool = True
    x: float = 50
    y: float = 50
    area: bool = False
    width: float = 68.75
    height: float = 100

    def settings(self):
        return replace(DEFAULTS, position_mode="free", x=self.x, y=self.y,
                       font_size=self.size, outline_enabled=self.outline,
                       area_enabled=self.area, width_percent=self.width,
                       height_percent=self.height)


def text_ass(sample, highlight=False):
    words = sample.text.replace("\n", " ").split()
    cues = [[{"word": word, "start": i / len(words), "end": (i + 1) / len(words)}
             for i, word in enumerate(words)]]
    settings = replace(sample.settings(), highlight_word_enabled=highlight)
    return build_ass("1\n00:00:00,000 --> 00:00:01,000\n" + sample.text + "\n", settings, cues)


def events(document):
    return [line for line in document.splitlines() if line.startswith("Dialogue:")]


def replace_events(document, lines):
    return document.split("[Events]")[0] + (
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) + "\n".join(lines) + "\n"


class Rasterizer:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.counter = 0
        self.calls = 0

    def render(self, document, width, height, frame_count=1, fps=1):
        self.counter += 1
        path = self.directory / f"raster_{self.counter:04}.ass"
        path.write_text(document, encoding="utf-8-sig")
        escaped = str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                   f"color=c=black:s={width}x{height}:r={fps}:d={max(1, frame_count / fps)}",
                   "-vf", f"subtitles=filename='{escaped}'", "-frames:v", str(frame_count),
                   "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        self.calls += 1
        result = subprocess.run(command, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        expected = width * height * 3 * frame_count
        if len(result.stdout) != expected:
            raise RuntimeError(f"Expected {expected} raw bytes, received {len(result.stdout)}")
        return np.frombuffer(result.stdout, np.uint8).reshape(frame_count, height, width, 3)


def bbox(mask):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise ValueError("Empty text/shape mask")
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def occupied(image):
    return image.max(axis=2) > 0


def measurement_ass(document, width, height):
    """White fill AND white outline, before clipping, at a translated position.

    Translate by INTEGER output pixels, retaining margins and wrapping. This
    recovers off-screen text without changing subpixel phase or font scaling.
    A boundary-touching measurement is rejected rather than reported as complete.
    """
    line = events(document)[0]
    content = line.split(",", 9)[9]
    position = re.search(r"\\pos\(([-\d.]+),([-\d.]+)\)", content)
    if position is None:
        raise ValueError("Prototype requires explicit free/area position")
    x, y = map(float, position.groups())
    dx = round(width / 2 - x * width / 384)
    dy = round(height / 2 - y * height / 288)
    content = re.sub(r"\\clip\([^)]*\)", "", content)
    content = re.sub(r"\\pos\([^)]*\)",
                     lambda _: f"\\pos({x + dx * 384 / width:.8f},{y + dy * 288 / height:.8f})", content)
    content = re.sub(r"\\1c&H[0-9a-fA-F]+&", "", content)
    content = r"{\1c&HFFFFFF&\3c&HFFFFFF&}" + content
    event = "Dialogue: 1,0:00:00.00,0:00:01.00,Text,,0,0,0,," + content
    return replace_events(document, [event]), (dx, dy)


def measure(rasterizer, document, width, height):
    started = time.perf_counter()
    diagnostic, (dx, dy) = measurement_ass(document, width, height)
    image = rasterizer.render(diagnostic, width, height)[0]
    bounds = bbox(occupied(image))
    if bounds[0] == 0 or bounds[1] == 0 or bounds[2] == width or bounds[3] == height:
        raise ValueError("Text exceeds diagnostic canvas: cannot measure full bounds")
    bounds = (bounds[0] - dx, bounds[1] - dy, bounds[2] - dx, bounds[3] - dy)
    return bounds, time.perf_counter() - started


def measure_batch(rasterizer, documents, width, height):
    """One synthetic frame per cue, sharing a single FFmpeg/libass process."""
    started = time.perf_counter()
    styles, lines, offsets = [], [], []
    for index, document in enumerate(documents):
        diagnostic, offset = measurement_ass(document, width, height)
        offsets.append(offset)
        styles.extend(line.replace("Style: Text,", f"Style: Text{index},")
                      for line in diagnostic.splitlines() if line.startswith("Style: Text,"))
        line = events(diagnostic)[0].split(",", 9)
        line[1], line[2], line[3] = f"0:00:{index:02}.00", f"0:00:{index+1:02}.00", f"Text{index}"
        lines.append(",".join(line))
    header = "\n".join(line for line in documents[0].split("[Events]")[0].splitlines()
                       if not line.startswith("Style:")) + "\n" + "\n".join(styles) + "\n"
    frames = rasterizer.render(replace_events(header, lines), width, height, frame_count=len(documents))
    results = []
    for frame, (dx, dy) in zip(frames, offsets):
        left, top, right, bottom = bbox(occupied(frame))
        require(left > 0 and top > 0 and right < width and bottom < height, "Batch measurement truncated")
        results.append((left-dx, top-dy, right-dx, bottom-dy))
    return results, time.perf_counter() - started


def rounded_path(bounds, radius, width, height):
    """Pixel-space rounded rectangle converted to p7 script-space coordinates."""
    left, top, right, bottom = bounds
    radius = min(radius, (right - left) / 2, (bottom - top) / 2)
    k = 4 * (math.sqrt(2) - 1) / 3

    def point(x, y):
        return f"{round(x * 384 / width * 64)} {round(y * 288 / height * 64)}"

    def command(kind, *points):
        return kind + " " + " ".join(point(x, y) for x, y in points)

    if radius == 0:
        return " ".join((command("m", (left, top)),
                         command("l", (right, top), (right, bottom), (left, bottom), (left, top))))
    r = radius
    return " ".join((
        command("m", (left + r, top)), command("l", (right - r, top)),
        command("b", (right - r + k*r, top), (right, top + r - k*r), (right, top + r)),
        command("l", (right, bottom - r)),
        command("b", (right, bottom - r + k*r), (right - r + k*r, bottom), (right - r, bottom)),
        command("l", (left + r, bottom)),
        command("b", (left + r - k*r, bottom), (left, bottom - r + k*r), (left, bottom - r)),
        command("l", (left, top + r)),
        command("b", (left, top + r - k*r), (left + r - k*r, top), (left + r, top)),
    ))


def compose(document, text_bounds, width, height, padding_x=24, padding_y=12,
            radius=20, color="#20A060", opacity=70, clip=None, include_text=True):
    if min(padding_x, padding_y, radius) < 0:
        raise ValueError("Padding and radius must be non-negative")
    left, top, right, bottom = text_bounds
    bounds = (left - padding_x, top - padding_y, right + padding_x, bottom + padding_y)
    alpha = round(255 * (1 - opacity / 100))
    fill = ass_color(color, 100)
    # Vector style is based on Text, but every geometrical/effect attribute is
    # explicit. Clip belongs to the box event only; text keeps its original clip.
    tags = (rf"\an7\pos(0,0)\p7\fscx100\fscy100\bord0\shad0"
            rf"\1c{fill}\1a&H{alpha:02X}&")
    if clip:
        tags += "\\clip(" + ",".join(f"{value:.8f}" for value in clip) + ")"
    shape = ("Dialogue: 0,0:00:00.00,0:00:01.00,Text,,0,0,0,,{" + tags + "}"
             + rounded_path(bounds, radius, width, height))
    return replace_events(document, [shape] + (events(document) if include_text else [])), bounds


def save_ppm(path, image):
    height, width, _ = image.shape
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + image.tobytes())


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(directory):
    rasterizer = Rasterizer(directory)
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
    samples = [
        Sample("single_cyrillic", "Это тест"),
        Sample("two_unequal_lines", "Первая\nЯ"),
        Sample("auto_wrap", "Это длинная реплика для проверки автоматических переносов текста по ширине области",
               area=True, width=45, height=90),
        Sample("no_outline", "Кириллица: ёж, Й, Д!", outline=False),
        Sample("small_font", "Мелкий текст", size=10),
        Sample("large_font", "Крупный\nтекст", size=30),
        Sample("near_edge", "У края", x=0, y=0),
        Sample("area_clipped", "Первая строка\nВторая строка\nТретья строка", area=True, width=55, height=12.5),
        Sample("area_near_edge", "Текст у края\nВторая строка", x=0, y=100, area=True, width=45, height=20),
        Sample("outline_off", "Это тест", outline=False),
        Sample("fractional_position", "Дробная позиция", x=32.7, y=67.3),
    ]
    report = {"ffmpeg": version.stdout.splitlines()[0], "directory": str(directory), "cases": [],
              "timings": [], "checks": []}
    for width, height in ((720, 1280), (1080, 1920)):
        measured = {}
        for sample in samples:
            document = text_ass(sample)
            bounds, seconds = measure(rasterizer, document, width, height)
            measured[sample.name] = bounds
            settings = sample.settings()
            clip = subtitle_area(settings) if sample.area else None
            entry = {"name": sample.name, "resolution": [width, height], "text_bounds": bounds,
                     "measure_ms": round(seconds * 1000, 2), "radii": []}
            # Independent unshifted raster validates recovered bounds at the
            # original position, including the integer-pixel recentering trick.
            original = re.sub(r"\\clip\([^)]*\)", "", document)
            original = re.sub(r"\\1c&H[0-9a-fA-F]+&", "", original)
            # Insert overrides after the fixed nine event fields, not into styles.
            original_lines = []
            for line in events(original):
                fields = line.split(",", 9)
                fields[9] = r"{\1c&HFFFFFF&\3c&HFFFFFF&}" + fields[9]
                original_lines.append(",".join(fields))
            original_pixels = rasterizer.render(replace_events(original, original_lines), width, height)[0]
            visible_bounds = bbox(occupied(original_pixels))
            predicted = (max(0, bounds[0]), max(0, bounds[1]), min(width, bounds[2]), min(height, bounds[3]))
            require(max(abs(a-b) for a, b in zip(visible_bounds, predicted)) <= 1,
                    f"Translated measurement mismatch: {sample.name}: {visible_bounds} vs {predicted}")
            entry["unshifted_text_bounds"] = visible_bounds
            row_ys = np.flatnonzero(occupied(original_pixels).any(axis=1))
            row_groups = 1 + int(np.count_nonzero(np.diff(row_ys) > 1))
            entry["visible_ink_row_bands"] = row_groups
            if sample.name in ("single_cyrillic", "two_unequal_lines"):
                require(row_groups == (1 if sample.name == "single_cyrillic" else 2),
                        f"Expected explicit line count: {sample.name}: {row_groups}")
            for radius in (0, 24):
                shape_ass, expected = compose(document, bounds, width, height, radius=radius,
                                              color="#FFFFFF", opacity=100, include_text=False)
                shape_image = rasterizer.render(shape_ass, width, height)[0]
                actual = bbox(occupied(shape_image))
                visible = (max(0, expected[0]), max(0, expected[1]),
                           min(width, expected[2]), min(height, expected[3]))
                require(max(abs(a-b) for a, b in zip(actual, visible)) <= 1,
                        f"{sample.name} {width}: vector bounds {actual} != {expected}")
                entry["radii"].append({"radius": radius, "expected_bounds": expected, "raster_bounds": actual})
            combined, _ = compose(document, bounds, width, height, clip=clip)
            combined_pixels = rasterizer.render(combined, width, height)[0]
            if clip:
                left, top, right, bottom = clip
                mask = occupied(combined_pixels).copy()
                # Rectangular ASS clips quantize in script coordinates; RGB
                # conversion of YUV420 can additionally bleed chroma at an edge.
                guard_x, guard_y = math.ceil(width/384)+1, math.ceil(height/288)+1
                mask[max(0, math.floor(top*height/288)-guard_y):min(height, math.ceil(bottom*height/288)+guard_y),
                     max(0, math.floor(left*width/384)-guard_x):min(width, math.ceil(right*width/384)+guard_x)] = False
                require(not mask.any(), f"{sample.name}: pixels outside clip {clip}; actual={bbox(occupied(combined_pixels))}")
                entry["clipped_bounds"] = bbox(occupied(combined_pixels))
            save_ppm(Path(directory) / f"{sample.name}_{width}.ppm", combined_pixels)
            report["cases"].append(entry)
        require(measured["auto_wrap"][3] - measured["auto_wrap"][1] >
                measured["single_cyrillic"][3] - measured["single_cyrillic"][1], "Automatic wrapping did not occur")
        wider = replace(samples[2], width=80)
        wider_bounds, _ = measure(rasterizer, text_ass(wider), width, height)
        narrow_bounds = measured["auto_wrap"]
        require(narrow_bounds[3]-narrow_bounds[1] > wider_bounds[3]-wider_bounds[1],
                "Narrowing area did not increase auto-wrapped block height")
        report["checks"].append({"name": "auto_wrap_width", "width": width,
                                  "narrow_bounds": narrow_bounds, "wide_bounds": wider_bounds})
        with_outline, without_outline = measured["single_cyrillic"], measured["outline_off"]
        require(with_outline[0] < without_outline[0] and with_outline[1] < without_outline[1]
                and with_outline[2] > without_outline[2] and with_outline[3] > without_outline[3],
                "Outline was not included in measurement")

        # A square-corner box permits exact, independent padding checks.
        bounds = measured["single_cyrillic"]
        for px, py in ((0, 0), (37, 4), (4, 37)):
            document, expected = compose(text_ass(samples[0]), bounds, width, height,
                                         padding_x=px, padding_y=py, radius=0,
                                         color="#FFFFFF", opacity=100, include_text=False)
            actual = bbox(occupied(rasterizer.render(document, width, height)[0]))
            require(max(abs(a-b) for a, b in zip(actual, expected)) <= 1, "Independent padding failed")
            report["checks"].append({"name": "padding", "width": width, "px": px, "py": py,
                                      "expected": expected, "actual": actual})

        # Compare a rounded isolated shape against the analytic circle, at 50%
        # coverage. Do not infer circularity merely from the generated path.
        diagnostic = text_ass(samples[0])
        square = (width//2-100, height//2-100, width//2+100, height//2+100)
        shape, _ = compose(diagnostic, square, width, height, padding_x=0, padding_y=0,
                           radius=40, color="#FFFFFF", opacity=100, include_text=False)
        image = rasterizer.render(shape, width, height)[0]
        gray = image.max(axis=2)
        l, t, r, b = square
        row_inset = int(np.flatnonzero(gray[t+1, l:r] > 127)[0])
        col_inset = int(np.flatnonzero(gray[t:b, l+1] > 127)[0])
        yy, xx = np.mgrid[t:t+40, l:l+40]
        ideal = (xx + .5 - (l+40))**2 + (yy + .5 - (t+40))**2 <= 40**2
        actual = gray[t:t+40, l:l+40] > 127
        mismatch = int(np.count_nonzero(ideal != actual))
        require(abs(row_inset-col_inset) <= 1, f"Elliptical radius: {row_inset}, {col_inset}")
        require(mismatch <= 45, f"Radius differs from circle: {mismatch} pixels")
        report["checks"].append({"name": "circular_radius", "width": width, "radius": 40,
                                  "horizontal_inset": row_inset, "vertical_inset": col_inset,
                                  "circle_mismatch_pixels": mismatch})
        narrow_box = (width//2-60, height//2-20, width//2+60, height//2+20)
        clamped, _ = compose(diagnostic, narrow_box, width, height, padding_x=0, padding_y=0,
                              radius=1000, color="#FFFFFF", opacity=100, include_text=False)
        explicit, _ = compose(diagnostic, narrow_box, width, height, padding_x=0, padding_y=0,
                               radius=20, color="#FFFFFF", opacity=100, include_text=False)
        require(np.array_equal(rasterizer.render(clamped, width, height),
                               rasterizer.render(explicit, width, height)), "Radius clamp failed")
        report["checks"].append({"name": "radius_clamp", "width": width,
                                  "requested": 1000, "effective": 20})

        # Same white outline exposes glyph coverage even on a black test frame.
        highlighted = text_ass(samples[0], highlight=True).replace("&H00000000&,", "&H00FFFFFF&,")
        frames = rasterizer.render(highlighted, width, height, frame_count=4, fps=4)
        masks = [occupied(frame) for frame in frames]
        white_variants = re.sub(r"\\1c&H[0-9a-fA-F]+&", "", highlighted)
        white_frames = rasterizer.render(white_variants, width, height, frame_count=4, fps=4)
        require(all(np.array_equal(white_frames[0], frame) for frame in white_frames[1:]),
                "Highlight event segmentation changed glyph layout")
        full, _ = compose(highlighted, bounds, width, height)
        frames = rasterizer.render(full, width, height, frame_count=4, fps=4)
        baseline = text_ass(samples[0]).replace("&H00000000&,", "&H00FFFFFF&,")
        full_baseline, _ = compose(baseline, bounds, width, height)
        reference = rasterizer.render(full_baseline, width, height)[0]
        text_mask = np.logical_or.reduce(masks)
        # Color changes can affect adjacent RGB pixels through 4:2:0 chroma.
        padded = np.pad(text_mask, 2)
        expanded = np.logical_or.reduce([padded[dy:dy+height, dx:dx+width]
                                         for dy in range(5) for dx in range(5)])
        outside = ~expanded
        require(all(np.array_equal(frame[outside], reference[outside]) for frame in frames),
                "Highlight changed background pixels")
        require(any(not np.array_equal(frames[0], frame) for frame in frames[1:]), "Highlight was not visible")
        report["checks"].append({"name": "highlight_stable", "width": width, "frames": 4})

        # Alpha is tested in the interior, away from text and antialiased edges.
        for opacity, color in ((0, "#FF0000"), (50, "#FF0000"), (100, "#FF0000"), (70, "#20A060")):
            shape, _ = compose(diagnostic, square, width, height, padding_x=0, padding_y=0,
                               radius=20, color=color, opacity=opacity, include_text=False)
            image = rasterizer.render(shape, width, height)[0]
            value = image[height//2, width//2].tolist()
            coverage = (255 - round(255 * (1-opacity/100))) / 255
            expected = [int(color[offset:offset+2], 16) * coverage for offset in (1, 3, 5)]
            require(max(abs(a-b) for a, b in zip(value, expected)) <= 3, f"Bad alpha/color: {value}")
            report["checks"].append({"name": "opacity", "width": width, "opacity": opacity, "rgb": value})

        oversized = Sample("oversized_rejected", "Очень длинный текст " * 40, size=30, area=True,
                           width=40, height=100)
        try:
            measure(rasterizer, text_ass(oversized), width, height)
        except ValueError as exc:
            require("exceeds diagnostic canvas" in str(exc), f"Unexpected measurement failure: {exc}")
        else:
            raise AssertionError("Oversized text was silently measured as complete")
        report["checks"].append({"name": "oversized_rejected", "width": width})

        # Sequential preparations: includes ASS IO, FFmpeg startup, decode and
        # bbox extraction; no source video and no persistent application cache.
        for repeat in range(2):
            start = time.perf_counter()
            for sample in samples[:5]:
                measure(rasterizer, text_ass(sample), width, height)
            report["timings"].append({"width": width, "cues": 5, "repeat": repeat,
                                      "mode": "sequential", "total_ms": round((time.perf_counter()-start)*1000, 2)})
            batch, seconds = measure_batch(rasterizer, [text_ass(sample) for sample in samples[:5]], width, height)
            require(batch == [measured[sample.name] for sample in samples[:5]], "Batch changed text bounds")
            report["timings"].append({"width": width, "cues": 5, "repeat": repeat,
                                      "mode": "batch", "total_ms": round(seconds*1000, 2)})
        started = time.perf_counter()
        bounds, _ = measure(rasterizer, text_ass(samples[0]), width, height)
        preview, _ = compose(text_ass(samples[0]), bounds, width, height)
        rasterizer.render(preview, width, height)
        report["timings"].append({"width": width, "cues": 1, "mode": "measure_plus_preview",
                                  "total_ms": round((time.perf_counter()-started)*1000, 2)})
    report["ffmpeg_raster_calls"] = rasterizer.calls
    report["status"] = "PASS"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Keep artifacts in a new TEMP directory")
    parser.add_argument("--summary", action="store_true", help="Print compact results; --keep still saves full report")
    parser.add_argument("--temp-parent", type=Path, default=None, help="Existing temporary parent directory")
    args = parser.parse_args()
    if args.temp_parent is not None and not args.temp_parent.is_dir():
        parser.error("--temp-parent must be an existing directory")
    if args.keep:
        directory = tempfile.mkdtemp(prefix="ass_vector_prototype_", dir=args.temp_parent)
        print(f"Synthetic artifacts: {directory}", flush=True)
        report = run(directory)
        (Path(directory) / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with tempfile.TemporaryDirectory(prefix="ass_vector_prototype_", dir=args.temp_parent) as directory:
            report = run(directory)
    if args.summary:
        report = {**report, "cases": [{key: value for key, value in case.items()
                                      if key not in ("radii", "unshifted_text_bounds")}
                                     for case in report["cases"]]}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
