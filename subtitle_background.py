"""Reusable, synthetic libass measurement and pixel-sized ASS background geometry.

No GUI, application data, persistent cache or dependency on the experiment.
The caller supplies the final output dimensions; historical PlayRes stays 384x288.
"""
import math
import re
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from subtitle_settings import ass_color, build_ass, subtitle_area


class MeasurementCache:
    """Small per-editor LRU. Never hold the lock during FFmpeg or Tk shutdown."""

    def __init__(self, max_entries=64):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    def get_or_measure(self, key, measure):
        with self._lock:
            if self._closed:
                raise ValueError("Подготовка предпросмотра отменена: окно закрыто.")
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
        bounds = measure()  # Exceptions are not cached.
        with self._lock:
            if self._closed:
                raise ValueError("Подготовка предпросмотра отменена: окно закрыто.")
            self._entries[key] = bounds
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return bounds

    def close(self):
        with self._lock:
            self._closed = True
            self._entries.clear()


def _events(document):
    return [line.split(",", 9) for line in document.splitlines() if line.startswith("Dialogue:")]


def _with_events(document, events):
    return document.split("[Events]")[0] + (
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) + "\n".join(",".join(event) for event in events) + "\n"


def _escape_path(path):
    result = str(path).replace("\\", "/")
    for char in ("\\", "'", ":", ";", "[", "]"):
        result = result.replace(char, "\\" + char)
    return result


def _diagnostic_ass(document, event, width, height):
    content = event[9]
    # Only inspect generated override blocks, never escaped literal ASS-like text.
    blocks = re.compile(r"(?<!\\)\{([^{}]*)\}")
    position = next((re.search(r"\\pos\(([-\d.]+),([-\d.]+)\)", match[1])
                     for match in blocks.finditer(content) if r"\pos(" in match[1]), None)
    if position:
        x, y = map(float, position.groups())
    else:
        # build_ass historical style: bottom-centre, margins 60/60/240.
        # Explicit positioning preserves that anchor; it does not change alignment.
        x, y = 192, 48
        content = r"{\pos(192,48)}" + content
    dx = round(width / 2 - x * width / 384)
    dy = round(height / 2 - y * height / 288)

    def translate(match):
        tags = re.sub(r"\\clip\([^)]*\)", "", match[1])
        tags = re.sub(r"\\pos\([^)]*\)",
                      lambda _: f"\\pos({x + dx * 384 / width:.8f},{y + dy * 288 / height:.8f})", tags)
        return "{" + tags + "}"

    content = r"{\1c&HFFFFFF&\3c&HFFFFFF&}" + blocks.sub(translate, content)
    diagnostic = ["Dialogue: 1", "0:00:00.00", "0:00:01.00", "Text", "", "0", "0", "0", "", content]
    return _with_events(document, [diagnostic]), (dx, dy)


def _measure_text(document, event, width, height):
    diagnostic, (dx, dy) = _diagnostic_ass(document, event, width, height)
    with tempfile.TemporaryDirectory(prefix="subtitle_measure_") as directory:
        path = Path(directory) / "text.ass"
        path.write_text(diagnostic, encoding="utf-8-sig")
        try:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 f"color=c=black:s={width}x{height}:d=0.04",
                 "-vf", f"subtitles='{_escape_path(path)}'", "-frames:v", "1", "-threads", "1",
                 "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Измерение текста FFmpeg превысило 15 секунд.") from exc
        except OSError as exc:
            raise ValueError(f"Не удалось запустить FFmpeg для измерения текста: {exc}") from exc
    if result.returncode:
        raise ValueError("FFmpeg не смог измерить текст: " + result.stderr.decode("utf-8", errors="replace")[-1500:])
    if len(result.stdout) != width * height:
        raise ValueError("FFmpeg вернул неполный кадр при измерении текста.")
    left, top, right, bottom = width, height, 0, 0
    for y in range(height):
        row = result.stdout[y * width:(y + 1) * width]
        end = len(row.rstrip(b"\0"))
        if end:
            left = min(left, width - len(row.lstrip(b"\0")))
            right = max(right, end)
            top, bottom = min(top, y), y + 1
    if not right:
        raise ValueError("Не удалось измерить текст: libass не нарисовал видимых символов.")
    if left == 0 or top == 0 or right == width or bottom == height:
        raise ValueError("Текст превышает измерительный кадр. Уменьшите шрифт или расширьте область субтитров.")
    return left - dx, top - dy, right - dx, bottom - dy


def rounded_rectangle_path(bounds, radius, width, height):
    """Output pixel geometry -> independent X/Y script scaling in p7 (1/64)."""
    left, top, right, bottom = bounds
    radius = min(radius, (right - left) / 2, (bottom - top) / 2)
    k = 4 * (math.sqrt(2) - 1) / 3

    def command(kind, *points):
        return kind + " " + " ".join(f"{round(x * 384 / width * 64)} {round(y * 288 / height * 64)}"
                                       for x, y in points)

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


def prepare_ass(srt, settings, width, height, word_cues=None, cache=None):
    """Preserve generated text events; add one vector per full cue, before clipping.

    Cache identity includes Text style (font/outline/margins), complete unhighlighted
    event content (text/position/area) and output size. It excludes cue times, all
    background options, and highlight colours/timings, which cannot change layout.
    Legacy and disabled backgrounds never measure or create a vector.
    Full-region geometry bypasses measurement/cache entirely. Only that geometry
    uses the saved radius; text-sized backgrounds always have square corners.
    """
    if type(width) is not int or type(height) is not int or min(width, height) < 2 or width % 2 or height % 2:
        raise ValueError("Требуются положительные чётные размеры выходного кадра.")
    if not settings.background_enabled or settings.background_geometry_mode == "legacy":
        return build_ass(srt, settings, word_cues)
    text_settings = replace(settings, background_enabled=False)
    document = build_ass(srt, text_settings, word_cues)
    plain = build_ass(srt, replace(text_settings, highlight_word_enabled=False))
    fill_region = settings.area_enabled and settings.background_fill_region
    area = subtitle_area(settings) if settings.area_enabled else None
    if fill_region:
        left, top, right, bottom = area
        region_bounds = (left * width / 384, top * height / 288,
                         right * width / 384, bottom * height / 288)
    else:
        cache = cache if cache is not None else MeasurementCache()
        text_style = next(line for line in plain.splitlines() if line.startswith("Style: Text,"))
        geometry = tuple(getattr(settings, name) for name in (
            "font", "font_size", "position_mode", "x", "y", "area_enabled", "width_percent", "height_percent",
            "outline_enabled", "outline_color"))
    shapes = []
    for event in _events(plain):
        if fill_region:
            bounds = region_bounds
        else:
            key = (width, height, geometry, text_style, event[9])
            left, top, right, bottom = cache.get_or_measure(
                key, lambda event=event: _measure_text(plain, event, width, height))
            bounds = (left - settings.background_padding_x_px, top - settings.background_padding_y_px,
                      right + settings.background_padding_x_px, bottom + settings.background_padding_y_px)
        alpha = round(255 * (1 - settings.background_opacity / 100))
        tags = (rf"\an7\pos(0,0)\p7\fscx100\fscy100\bord0\shad0"
                rf"\1c{ass_color(settings.background_color, 100)}\1a&H{alpha:02X}&")
        if area is not None and not fill_region:
            tags += "\\clip(" + ",".join(f"{value:.8f}" for value in area) + ")"
        radius = settings.background_corner_radius_px if fill_region else 0
        shapes.append(["Dialogue: 0", event[1], event[2], "Text", "", "0", "0", "0", "",
                       "{" + tags + "}" + rounded_rectangle_path(bounds, radius, width, height)])
    return _with_events(document, shapes + _events(document))
