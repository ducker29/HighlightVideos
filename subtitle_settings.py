"""Versioned Shorts subtitle styles; no GUI dependencies.

Font size uses ASS units in the historical 384 x 288 script space.
The historical layout deliberately keeps FFmpeg's original SRT conversion.
"""
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class SubtitleSettings:
    font: str = "Arial"
    font_size: float = 18
    text_color: str = "#FFFFFF"
    position_mode: str = "historical"
    x: float = 50
    y: float = 16.6666666667
    background_enabled: bool = False
    background_color: str = "#000000"
    background_opacity: float = 100
    outline_enabled: bool = True
    outline_color: str = "#000000"
    area_enabled: bool = False
    width_percent: float = 68.75
    height_percent: float = 100
    highlight_word_enabled: bool = False
    highlight_word_color: str = "#FFFF00"
    background_padding_x_px: float = 0
    background_padding_y_px: float = 0
    background_corner_radius_px: float = 0
    background_geometry_mode: str = "legacy"
    background_fill_region: bool = False
    shadow_color: str = "#000000"
    shadow_size: float = 0
    shadow_enabled: bool = False

    def __post_init__(self):
        if (not isinstance(self.font, str) or not self.font.strip()
                or len(self.font) > 128 or re.search(r"[\x00-\x1f,{}\\'=;]", self.font)):
            raise ValueError("Некорректное семейство шрифта.")
        for name, low, high in (("font_size", 1, 144), ("x", 0, 100),
                                ("y", 0, 100), ("background_opacity", 0, 100),
                                ("width_percent", 1, 100), ("height_percent", 1, 100),
                                ("background_padding_x_px", 0, 512),
                                ("background_padding_y_px", 0, 512),
                                ("background_corner_radius_px", 0, 512), ("shadow_size", 0, 20)):
            value = getattr(self, name)
            if (type(value) not in (float, int) or not low <= value <= high
                    or not math.isfinite(value)):
                raise ValueError(f"{name}: требуется число от {low} до {high}.")
        if self.position_mode not in ("historical", "free"):
            raise ValueError("Неизвестный режим позиции.")
        if (not isinstance(self.background_geometry_mode, str)
                or self.background_geometry_mode not in ("legacy", "custom")):
            raise ValueError("background_geometry_mode: требуется legacy или custom.")
        for name in ("background_enabled", "outline_enabled", "area_enabled", "highlight_word_enabled",
                     "background_fill_region", "shadow_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} должен быть boolean.")
        if self.background_fill_region and self.background_geometry_mode == "legacy":
            raise ValueError("background_fill_region=True требует background_geometry_mode=custom.")
        for name in ("text_color", "background_color", "outline_color", "highlight_word_color", "shadow_color"):
            color = getattr(self, name)
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError(f"{name}: цвет должен иметь вид #RRGGBB.")
        if self.area_enabled and self.position_mode != "free":
            raise ValueError("Для ограничения области выберите позиционирование free.")


DEFAULTS = SubtitleSettings()
LEGACY_STYLE = ("FontName=Arial,FontSize=18,PrimaryColour=&HFFFFFF&,"
                "OutlineColour=&H00000000&,Outline=1,Alignment=2,"
                "MarginV=240,MarginL=60,MarginR=60")


def document(settings):
    return {"schema_version": 8, "shorts": asdict(settings)}


def from_document(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "shorts"}:
        raise ValueError("Ожидается JSON-пресет Subs Settings.")
    version = value["schema_version"]
    if type(version) is not int or version not in (1, 2, 3, 4, 5, 6, 7, 8):
        raise ValueError("Неподдерживаемая версия пресета.")
    fields = value["shorts"]
    defaults = asdict(DEFAULTS)
    added_fields = {"outline_enabled", "outline_color", "area_enabled", "width_percent", "height_percent"}
    highlight_fields = {"highlight_word_enabled", "highlight_word_color"}
    geometry_fields = {"background_padding_x_px", "background_padding_y_px",
                       "background_corner_radius_px", "background_geometry_mode"}
    expected = set(defaults)
    if version < 8:
        expected.remove("shadow_enabled")
    if version < 7:
        expected -= {"shadow_color", "shadow_size"}
    if version < 6:
        expected.remove("text_color")
    if version < 5:
        expected.remove("background_fill_region")
    if version < 4:
        expected -= geometry_fields
    if version < 3:
        expected -= highlight_fields
    if version == 1:
        expected -= added_fields
    if not isinstance(fields, dict) or set(fields) != expected:
        raise ValueError("Неверный набор параметров субтитров.")
    settings = SubtitleSettings(**(defaults | fields))
    if version < 8:
        settings = replace(settings, shadow_enabled=version == 7 and settings.shadow_size > 0)
    return settings


def effective_style(settings):
    """Return a comparison-only style with inactive options reset to defaults.

    Keep the original settings for editing and persistence: hidden values are
    retained there. Legacy box geometry ignores all custom pixel dimensions.
    This helper does not enable custom rendering or alter generated ASS.
    """
    inactive = set()
    if not settings.shadow_enabled:
        inactive.update(("shadow_color", "shadow_size"))
    elif not settings.shadow_size:
        inactive.add("shadow_color")
        inactive.add("shadow_enabled")
    if not settings.background_enabled:
        inactive.update(("background_color", "background_opacity", "background_geometry_mode", "background_fill_region"))
    if not settings.background_enabled or settings.background_geometry_mode == "legacy":
        inactive.update(("background_padding_x_px", "background_padding_y_px", "background_corner_radius_px",
                         "background_fill_region"))
    elif settings.area_enabled and settings.background_fill_region:
        inactive.update(("background_padding_x_px", "background_padding_y_px"))
    else:
        inactive.update(("background_corner_radius_px", "background_fill_region"))
    if not settings.outline_enabled:
        inactive.add("outline_color")
    if not settings.area_enabled:
        inactive.update(("width_percent", "height_percent"))
    if settings.position_mode == "historical":
        inactive.update(("x", "y"))
    if not settings.highlight_word_enabled:
        inactive.add("highlight_word_color")
    return replace(settings, **{key: getattr(DEFAULTS, key) for key in inactive})


def uses_legacy_srt_style(settings):
    """Whether the effective style permits the historical SRT/force_style path.

    Shared model predicate for future preview/renderer integration. An enabled
    legacy background still needs ASS; legacy geometry is not the SRT default.
    """
    return effective_style(settings) == effective_style(DEFAULTS)


def load_settings(path):
    try:
        return from_document(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Повреждённый JSON-пресет.") from exc


def save_settings(path, settings):
    settings = from_document(document(settings))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".subs_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document(settings), stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ass_color(color, opacity):
    alpha = round(255 * (1 - opacity / 100))
    return f"&H{alpha:02X}{color[5:7]}{color[3:5]}{color[1:3]}&"


def _ass_time(srt_time):
    h, m, seconds = srt_time.replace(",", ".").split(":")
    sec, millis = seconds.split(".")
    # Match FFmpeg's millisecond -> ASS centisecond rounding, including carries.
    ticks = ((int(h) * 3600 + int(m) * 60 + int(sec)) * 1000 + int(millis) + 5) // 10
    hours, ticks = divmod(ticks, 360000)
    minutes, ticks = divmod(ticks, 6000)
    seconds, centis = divmod(ticks, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{centis:02}"


def subtitle_area(settings):
    """Fixed area in historical ASS units, centred at X/Y and shifted into frame.

    Integer horizontal edges match ASS margins so wrapping and clipping agree.
    Geometry depends only on settings, never on the current cue or font size.
    """
    width = max(1, round(384 * settings.width_percent / 100))
    height = 288 * settings.height_percent / 100
    left = max(0, min(384 - width, round(settings.x * 3.84 - width / 2)))
    top = max(0, min(288 - height, settings.y * 2.88 - height / 2))
    return left, top, left + width, top + height


def build_ass(srt, settings, word_cues=None):
    """Style only: preserve cue timing/content, escape literal ASS control characters.

    Unbounded free positions retain the previous conservative text-size clamp.
    An enabled fixed area instead controls wrap margins and clips both layers,
    with fixed font size and geometry independent of the current cue.
    """
    s = settings
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\n"
              "ScaledBorderAndShadow: yes\nYCbCr Matrix: None\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
              "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding\n")
    align = 2 if s.position_mode == "historical" else 5
    margin = 240 if s.position_mode == "historical" else 0
    left_margin = right_margin = 60
    if s.area_enabled:
        left, top, right, bottom = subtitle_area(s)
        left_margin, right_margin = left, 384 - right
    outline = ass_color(s.outline_color, 100)
    primary = ass_color(s.text_color, 100)
    shadow = ass_color(s.shadow_color, 100)
    header += (f"Style: Text,{s.font},{s.font_size},{primary},{primary},{outline},{shadow},"
               f"0,0,0,0,100,100,0,0,1,{int(s.outline_enabled)},{s.shadow_size if s.shadow_enabled else 0},{align},{left_margin},{right_margin},{margin},1\n")
    # The box layer is independent of the text's outline toggle and colour.
    bg = ass_color(s.background_color, s.background_opacity)
    header += (f"Style: Box,{s.font},{s.font_size},&HFFFFFFFF,&HFFFFFFFF,{bg},{bg},"
               f"0,0,0,0,100,100,0,0,3,1,0,{align},{left_margin},{right_margin},{margin},1\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    for cue_index, block in enumerate(re.split(r"\r?\n\s*\r?\n", srt.strip().lstrip("\ufeff"))):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        start, end = lines[1].split(" --> ")
        text = "\\N".join(line.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")
                           for line in lines[2:])
        position = ""
        if s.area_enabled:
            x, y = (left + right) / 2, (top + bottom) / 2
            position = (f"{{\\an5\\pos({x:.3f},{y:.3f})"
                        f"\\clip({left:.3f},{top:.3f},{right:.3f},{bottom:.3f})}}")
        elif s.position_mode == "free":
            half_w = min(190, max(len(line) for line in lines[2:]) * s.font_size * .36 + 3)
            half_h = min(142, len(lines[2:]) * s.font_size * .7 + 3)
            x = max(half_w, min(384 - half_w, s.x * 3.84))
            y = max(half_h, min(288 - half_h, s.y * 2.88))
            position = f"{{\\an5\\pos({x:.3f},{y:.3f})}}"
        variants = [(_ass_time(start), _ass_time(end), text)]
        if s.highlight_word_enabled and word_cues and cue_index < len(word_cues):
            words = word_cues[cue_index]
            raw = "\n".join(lines[2:])
            searchable = raw.replace("\n", " ")
            spans = []
            cursor = 0
            for word in words:
                index = searchable.find(word["word"], cursor)
                if index < 0:
                    continue
                spans.append((word, index, index + len(word["word"])))
                cursor = index + len(word["word"])
            def ticks(value):
                return round(value * 100)
            def timestamp(value):
                hours, rest = divmod(value, 360000)
                minutes, rest = divmod(rest, 6000)
                seconds, centis = divmod(rest, 100)
                return f"{hours}:{minutes:02}:{seconds:02}.{centis:02}"
            def escape(value):
                return value.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")
            if words:
                first, last = ticks(words[0]["start"]), ticks(words[-1]["end"])
                boundaries = sorted({first, last} | {max(first, min(last, ticks(w[key])))
                                    for w in words for key in ("start", "end")})
                variants = []
                for a, b in zip(boundaries, boundaries[1:]):
                    active = next(((i, j) for w, i, j in spans
                                   if ticks(w["start"]) <= a < ticks(w["end"])), None)
                    content = text
                    if active:
                        i, j = active
                        color = ass_color(s.highlight_word_color, 100)
                        content = (escape(raw[:i]) + "{\\1c" + color + "}" + escape(raw[i:j])
                                   + "{\\1c" + primary + "}" + escape(raw[j:]))
                    variants.append((timestamp(a), timestamp(b), content))
        for layer, style in ((0, "Box"), (1, "Text")):
            if style == "Box" and not s.background_enabled:
                continue
            parts = [(_ass_time(start), _ass_time(end), text)] if style == "Box" else variants
            for event_start, event_end, content in parts:
                events.append(f"Dialogue: {layer},{event_start},{event_end},{style},,0,0,0,,{position}{content}\n")
    return header + "".join(events)
