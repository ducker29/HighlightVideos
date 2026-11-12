"""Versioned Shorts subtitle styles; no GUI dependencies.

Font size uses ASS units in the historical 384 x 288 script space.
The historical layout deliberately keeps FFmpeg's original SRT conversion.
"""
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SubtitleSettings:
    font: str = "Arial"
    font_size: float = 18
    position_mode: str = "historical"
    x: float = 50
    y: float = 16.6666666667
    background_enabled: bool = False
    background_color: str = "#000000"
    background_opacity: float = 100

    def __post_init__(self):
        if (not isinstance(self.font, str) or not self.font.strip()
                or len(self.font) > 128 or re.search(r"[\x00-\x1f,{}\\'=;]", self.font)):
            raise ValueError("Некорректное семейство шрифта.")
        for name, low, high in (("font_size", 1, 144), ("x", 0, 100),
                                ("y", 0, 100), ("background_opacity", 0, 100)):
            value = getattr(self, name)
            if (type(value) not in (float, int) or not math.isfinite(value)
                    or not low <= value <= high):
                raise ValueError(f"{name}: требуется число от {low} до {high}.")
        if self.position_mode not in ("historical", "free"):
            raise ValueError("Неизвестный режим позиции.")
        if type(self.background_enabled) is not bool:
            raise ValueError("background_enabled должен быть boolean.")
        if not isinstance(self.background_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", self.background_color):
            raise ValueError("Цвет должен иметь вид #RRGGBB.")


DEFAULTS = SubtitleSettings()
LEGACY_STYLE = ("FontName=Arial,FontSize=18,PrimaryColour=&HFFFFFF&,"
                "OutlineColour=&H00000000&,Outline=1,Alignment=2,"
                "MarginV=240,MarginL=60,MarginR=60")


def document(settings):
    return {"schema_version": 1, "shorts": asdict(settings)}


def from_document(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "shorts"}:
        raise ValueError("Ожидается JSON-пресет Subs Settings.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Неподдерживаемая версия пресета.")
    fields = value["shorts"]
    if not isinstance(fields, dict) or set(fields) != set(asdict(DEFAULTS)):
        raise ValueError("Неверный набор параметров субтитров.")
    return SubtitleSettings(**fields)


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


def build_ass(srt, settings):
    """Style only: preserve cue timing/content, escape literal ASS control characters.

    Free positions use a centre anchor. Conservative text extents clamp the anchor
    into the frame; libass handles wrapping within the script margins.
    """
    s = settings
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\n"
              "ScaledBorderAndShadow: yes\nYCbCr Matrix: None\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
              "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding\n")
    align = 2 if s.position_mode == "historical" else 5
    margin = 240 if s.position_mode == "historical" else 0
    header += (f"Style: Text,{s.font},{s.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
               f"0,0,0,0,100,100,0,0,1,1,0,{align},60,60,{margin},1\n")
    # Separate box layer preserves the historical black text outline.
    bg = ass_color(s.background_color, s.background_opacity)
    header += (f"Style: Box,{s.font},{s.font_size},&HFFFFFFFF,&HFFFFFFFF,{bg},{bg},"
               f"0,0,0,0,100,100,0,0,3,1,0,{align},60,60,{margin},1\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    for block in re.split(r"\r?\n\s*\r?\n", srt.strip().lstrip("\ufeff")):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        start, end = lines[1].split(" --> ")
        text = "\\N".join(line.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")
                           for line in lines[2:])
        position = ""
        if s.position_mode == "free":
            half_w = min(190, max(len(line) for line in lines[2:]) * s.font_size * .36 + 3)
            half_h = min(142, len(lines[2:]) * s.font_size * .7 + 3)
            x = max(half_w, min(384 - half_w, s.x * 3.84))
            y = max(half_h, min(288 - half_h, s.y * 2.88))
            position = f"{{\\an5\\pos({x:.3f},{y:.3f})}}"
        for layer, style in ((0, "Box"), (1, "Text")):
            if style == "Box" and not s.background_enabled:
                continue
            events.append(f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{style},,0,0,0,,{position}{text}\n")
    return header + "".join(events)
