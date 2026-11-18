"""Pure video graph planning, used by the mixed Shorts renderer.

Input contract: one already selected/seeked video stream, with PTS expressed
in local Shorts seconds, and dimensions/SAR matching its decoded orientation.
Audio must retain the same time origin. This module never seeks, trims, changes
speed/FPS, resets PTS, reads files or runs a process. The renderer uses input
-ss without copyts for a shared local time origin; do NOT independently
apply PTS-STARTPTS to video and discard an existing audio/video offset.

All branches retain the common input PTS and original rational time base, produce
square-pixel yuv420p frames of the export size, and run over the complete input.
Static crop branches avoid FFmpeg's initialization-only crop.w/h expressions.
Cost is proportional to the number of distinct Smart Crop rectangles: repeated
rectangles share a branch, but many different zooms can still be expensive.

No FIT_BLUR => None: the caller must keep its existing -vf rendering path,
including its existing subtitle handling. A non-None graph accepts an optional
trusted, pre-escaped SINGLE subtitles filter from that path, unchanged, last.
The returned graph does not contain audio mappings, encoder or duration options.
It requires FFmpeg framesync overlay/timeline and libass support for subtitles.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import math
import re

from shorts_framing import candidate_framing
from shorts_settings import DEFAULTS
from smart_crop_storage import EPS, validate_settings


@dataclass(frozen=True)
class ShortsFilterGraph:
    filter_complex: str
    output_label: str
    output_width: int
    output_height: int


def _seconds(value):
    """Lossless decimal round-trip for model floats, without 6-digit rounding."""
    result = format(Decimal(str(value)), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _enable(intervals):
    # Quoting the complete expression protects its commas in filter_complex.
    # between() is deliberately not used: its right boundary is inclusive.
    return "+".join(f"gte(t,{_seconds(start)})*lt(t,{_seconds(end)})"
                    for start, end in intervals)


def _dimension(value, name):
    if type(value) is not int or value < 2:
        raise ValueError(f"{name}: требуется целое разрешение не меньше 2.")
    return value


def _fit_sizes(source_width, source_height, sar, width, height):
    """Numeric display-aspect sizing, even dimensions for yuv420p.

    Foreground rounds to the nearest even pixel, bounded by the canvas and a
    two-pixel minimum. Arbitrary exact DAR cannot always be represented on an
    integer 4:2:0 grid. Background rounds UP to guarantee full coverage.
    SAR is included in the sizing before setsar=1, not simply discarded.
    """
    if not isinstance(sar, (tuple, list)) or len(sar) != 2:
        raise ValueError("source_sar должен быть парой положительных целых чисел.")
    if any(type(v) is not int or v <= 0 for v in sar):
        raise ValueError("source_sar должен быть парой положительных целых чисел.")
    aspect = Fraction(source_width * sar[0], source_height * sar[1])
    canvas = Fraction(width, height)

    def nearest_even(value, limit):
        return max(2, min(limit, 2 * round(value / 2)))

    def ceil_even(value):
        return 2 * math.ceil(value / 2)

    if aspect >= canvas:
        return (ceil_even(height * aspect), height), (width, nearest_even(width / aspect, height))
    return (width, ceil_even(width / aspect)), (nearest_even(height * aspect, width), height)


def _crop_periods(candidate, source_width, source_height, smart_crop):
    # Lazy import avoids a renderer/generator module-initialization cycle and
    # reuses exactly the current geometry.
    from renderer import _crop_params, _crop_to_pixels, validate_crop_x

    duration = candidate["end"] - candidate["start"]
    if smart_crop is None:
        render = candidate.get("render_settings") or {}
        crop_x = validate_crop_x(render.get("crop_x"))
        return [(0.0, duration, _crop_params(source_width, source_height, crop_x))]
    settings = validate_settings(smart_crop)
    if (settings["candidate_id"] != candidate["candidate_id"]
            or abs(settings["source_start"] - candidate["start"]) > 0.001
            or abs(settings["source_end"] - candidate["end"]) > 0.001
            or settings["source_width"] != source_width
            or settings["source_height"] != source_height):
        raise ValueError("Smart Crop не соответствует текущему Shorts или разрешению источника.")
    segments = settings["segments"]
    if any(s["start"] < -EPS or s["end"] > duration + EPS for s in segments):
        raise ValueError("Smart Crop выходит за границы Shorts.")
    periods = []
    start = 0.0
    for index, segment in enumerate(segments):
        # Storage tolerates microsecond gaps. Match the legacy renderer's
        # switching at each previous end; the final branch covers the tail.
        end = duration if index == len(segments) - 1 else segment["end"]
        if end <= start or end > duration:
            raise ValueError("Некорректные границы сегментов Smart Crop.")
        periods.append((start, end, _crop_to_pixels(segment["crop"], source_width, source_height)))
        start = end
    return periods


def build_shorts_filtergraph(candidate, source_width, source_height, *,
                             export_settings=DEFAULTS, smart_crop=None,
                             subtitles_filter=None, source_sar=(1, 1),
                             input_label="0:v:0", label_prefix="framing", blur_sigma=20):
    """Return an immutable video graph plan, or None for the untouched old path.

    smart_crop is a loaded settings DOCUMENT, never a path. subtitles_filter
    is the existing caller-built `subtitles=...` fragment, including ASS/default
    style options; this function neither regenerates nor shifts subtitle cues.
    Do not pass the returned string through shell quoting: it is a single
    subprocess argument. The caller maps [result.output_label] and
    keeps its existing audio mapping, -ss, -t and encoding options separately.
    """
    framing = candidate_framing(candidate)
    if not framing["intervals"]:
        return None
    source_width = _dimension(source_width, "source_width")
    source_height = _dimension(source_height, "source_height")
    width, height = export_settings.output_size(source_width, source_height)
    _dimension(width, "output_width")
    _dimension(height, "output_height")
    if width % 2 or height % 2:
        raise ValueError("Выходное разрешение должно быть чётным для yuv420p.")
    if not isinstance(input_label, str) or not re.fullmatch(r"(?:\d+:v(?::\d+)?|[A-Za-z_][A-Za-z0-9_]*)", input_label):
        raise ValueError("Некорректная метка входного видео.")
    if not isinstance(label_prefix, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label_prefix):
        raise ValueError("Некорректный префикс меток композиции.")
    if isinstance(blur_sigma, bool) or not isinstance(blur_sigma, (int, float)):
        raise ValueError("blur_sigma должен быть конечным числом в (0, 1024].")
    if not 0 < blur_sigma <= 1024 or not math.isfinite(blur_sigma):
        raise ValueError("blur_sigma должен быть конечным числом в (0, 1024].")
    if subtitles_filter is not None and (not isinstance(subtitles_filter, str)
                                          or not subtitles_filter.startswith("subtitles=")):
        raise ValueError("Нужен готовый subtitles= фильтр или None.")
    background_size, foreground_size = _fit_sizes(source_width, source_height, source_sar, width, height)
    periods = _crop_periods(candidate, source_width, source_height, smart_crop)
    rectangles = list(dict.fromkeys(rect for _, _, rect in periods))
    if any(cw < 2 or ch < 2 or x < 0 or y < 0
           or x + cw > source_width or y + ch > source_height for cw, ch, x, y in rectangles):
        raise ValueError("Исходник слишком мал или область CROP выходит за его границы.")

    def label(name):
        return f"[{label_prefix}_{name}]"

    if input_label == f"{label_prefix}_video" or input_label.startswith(label_prefix + "_"):
        raise ValueError("Входная метка конфликтует с внутренними метками композиции.")
    inputs = [label(f"src{i}") for i in range(len(rectangles) + 2)]
    # Identity time base, NOT AVTB: do not quantize sub-microsecond source PTS.
    graph = [f"[{input_label}]settb=expr=intb,format=yuv420p,split={len(inputs)}{''.join(inputs)}"]
    common = f"scale={width}:{height},setsar=1,format=yuv420p"
    for index, (cw, ch, x, y) in enumerate(rectangles):
        graph.append(f"{inputs[index]}crop={cw}:{ch}:{x}:{y},{common}{label(f'crop{index}')}")
    # First geometry is the full-duration base. Every other geometry is an
    # opaque, same-size overlay active only during its own half-open periods.
    current = label("crop0")
    overlay_options = "x=0:y=0:format=yuv420:eof_action=pass:repeatlast=0"
    for index, rectangle in enumerate(rectangles[1:], 1):
        times = [(a, b) for a, b, rect in periods if rect == rectangle]
        target = label(f"switch{index}")
        graph.append(f"{current}{label(f'crop{index}')}overlay={overlay_options}:enable='{_enable(times)}'{target}")
        current = target
    bgw, bgh = background_size
    fgw, fgh = foreground_size
    graph.append(f"{inputs[-2]}scale={bgw}:{bgh},setsar=1,crop={width}:{height},"
                 f"gblur=sigma={_seconds(blur_sigma)},format=yuv420p{label('bg')}")
    graph.append(f"{inputs[-1]}scale={fgw}:{fgh},setsar=1,format=yuv420p{label('fg')}")
    graph.append(f"{label('bg')}{label('fg')}overlay=x=(W-w)/2:y=(H-h)/2:"
                 f"format=yuv420:eof_action=pass:repeatlast=0,format=yuv420p{label('fit')}")
    times = [(item["start"], item["end"]) for item in framing["intervals"]]
    graph.append(f"{current}{label('fit')}overlay={overlay_options}:enable='{_enable(times)}',"
                 f"format=yuv420p{label('composed')}")
    graph.append(f"{label('composed')}{subtitles_filter if subtitles_filter is not None else 'null'}{label('video')}")
    return ShortsFilterGraph(";".join(graph), f"{label_prefix}_video", width, height)
