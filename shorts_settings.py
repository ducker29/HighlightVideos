"""Immutable export settings, separate from candidate approval and analysis."""
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


RESOLUTIONS = {"720p": "720p — 720×1280", "1080p": "1080p — 1080×1920",
               "original": "Оригинальное — по разрешению источника"}
COMPRESSIONS = {"high": ("Высокое качество — CRF 18", 18),
                "balanced": ("Баланс — CRF 23", 23),
                "compact": ("Компактное — CRF 28", 28)}


@dataclass(frozen=True)
class ShortsSettings:
    resolution: str = "1080p"
    compression: str = "high"

    def __post_init__(self):
        if not isinstance(self.resolution, str) or self.resolution not in RESOLUTIONS:
            raise ValueError("Выберите разрешение 720p, 1080p или оригинальное.")
        if not isinstance(self.compression, str) or self.compression not in COMPRESSIONS:
            raise ValueError("Выберите сжатие: высокое качество, баланс или компактное.")

    @property
    def crf(self):
        return COMPRESSIONS[self.compression][1]

    def output_size(self, source_width, source_height):
        if self.resolution == "720p":
            return 720, 1280
        if self.resolution == "1080p":
            return 1080, 1920
        if any(type(value) is not int or value < 2 for value in (source_width, source_height)):
            raise ValueError("Некорректное разрешение источника.")
        # Largest 9:16 frame fitting the source, with even H.264-compatible sizes.
        # Tiny aspect deviations are unavoidable when rounding to even pixels.
        height = 2 * (source_height // 2)
        width = max(2, 2 * round(height * 9 / 32))
        if width > source_width:
            width = 2 * (source_width // 2)
            height = max(2, min(height, 2 * round(width * 16 / 18)))
        return width, height


DEFAULTS = ShortsSettings()


def document(settings):
    return {"schema_version": 1, "shorts_export": asdict(settings)}


def from_document(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "shorts_export"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("Неверный формат или версия настроек экспорта Shorts.")
    fields = value["shorts_export"]
    if not isinstance(fields, dict) or set(fields) != {"resolution", "compression"}:
        raise ValueError("Неверный набор настроек экспорта Shorts.")
    return ShortsSettings(**fields)


def load_settings(path):
    try:
        return from_document(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Повреждённый JSON настроек экспорта Shorts.") from exc


def save_settings(path, settings):
    settings = from_document(document(settings))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".shorts_export_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document(settings), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
