"""Persistent, atomic JSON artifacts shared by the backend and desktop UI."""
import json
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def validate_interval(start, end, duration, minimum=0, maximum=None):
    start, end = float(start), float(end)
    if not all(math.isfinite(v) for v in (start, end, duration)):
        raise ValueError("Границы должны быть конечными числами.")
    if not 0 <= start < end <= duration:
        raise ValueError(f"Нужны границы 0 ≤ начало < конец ≤ {duration:.3f} с.")
    if end - start < minimum - 0.001:
        raise ValueError(f"Клип короче минимальной длительности {minimum} с.")
    if maximum is not None and end - start > maximum + 0.001:
        raise ValueError(f"Клип длиннее максимальной длительности {maximum} с.")
    return start, end


def timecode(seconds):
    milliseconds = round(float(seconds) * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def parse_time(value):
    parts = str(value).strip().replace(",", ".").split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("Введите секунды или ЧЧ:ММ:СС.ммм.")
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result
