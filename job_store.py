"""Persistent, atomic JSON artifacts shared by the backend and desktop UI."""
import json
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def padding_seconds(value):
    try:
        seconds = float(str(value).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError("Запас в конце укажите числом секунд от 0 до 5.") from exc
    if not math.isfinite(seconds) or not 0 <= seconds <= 5:
        raise ValueError("Запас в конце должен быть конечным числом от 0 до 5 секунд.")
    return round(seconds, 3)


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


def analysis_bounds(settings, duration=None):
    """Resolve UI minutes to absolute source seconds; legacy settings mean full video."""
    full = settings.get("analyze_full_video", True)
    if type(full) is not bool:
        raise ValueError("Параметр «Анализ полного видео» должен быть boolean.")
    if full:
        return 0.0, None
    try:
        start = float(str(settings.get("analysis_start_min", "")).replace(",", ".")) * 60
        end = float(str(settings.get("analysis_end_min", "")).replace(",", ".")) * 60
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Укажите начало и конец анализа числами в минутах.") from exc
    if not all(math.isfinite(value) for value in (start, end)) or not 0 <= start < end:
        raise ValueError("Диапазон анализа: 0 ≤ начало < конец; значения в минутах должны быть конечными.")
    start, end = round(start, 3), round(end, 3)
    if start >= end:
        raise ValueError("Диапазон анализа должен быть не короче 0,001 секунды.")
    if duration is not None and end > duration:
        raise ValueError(f"Конец диапазона ({end / 60:g} мин) превышает длительность видео ({duration / 60:.3f} мин).")
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


# --- Профили выхода (16:9 хайлайты / 9:16 Shorts) -------------------------

PROFILE_NAMES = ("highlights", "shorts")

DEFAULT_PROFILE = {
    "aspect_ratio": "16:9",
    "min_duration_sec": 120,
    "max_duration_sec": 300,
    "end_padding_sec": 0.5,
}

SHORTS_PROFILE = {
    "enabled": False,
    "aspect_ratio": "9:16",
    "min_duration_sec": 20,
    "target_duration_sec": 40,
    "max_duration_sec": 60,
    "crop_mode": "center",
    "subtitles_enabled": True,
    "max_candidates": 30,
    "final_candidates": 10,
}

FORMATS = ("highlight", "short")


def normalize_profiles(raw):
    """Объединяет секцию ``outputs`` с дефолтами; отсутствие секции = старый режим 16:9 + shorts выключены."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Секция profiles должна быть объектом с полями highlights и shorts.")
    profiles = {}
    highlights = dict(DEFAULT_PROFILE)
    if "highlights" in raw:
        highlights.update(raw["highlights"])
    highlights["min_duration_sec"] = int(highlights["min_duration_sec"])
    highlights["max_duration_sec"] = int(highlights["max_duration_sec"])
    highlights["end_padding_sec"] = float(highlights["end_padding_sec"])
    if not 1 <= highlights["min_duration_sec"] <= highlights["max_duration_sec"] <= 480:
        raise ValueError("Профиль highlights: нужна длительность 1 ≤ минимум ≤ максимум ≤ 480 секунд.")
    if highlights["aspect_ratio"] not in ("16:9", "9:16"):
        raise ValueError("Недопустимое aspect_ratio профиля highlights.")
    profiles["highlights"] = highlights
    shorts = dict(SHORTS_PROFILE)
    if "shorts" in raw:
        shorts.update(raw["shorts"])
    shorts["enabled"] = bool(shorts["enabled"])
    shorts["min_duration_sec"] = int(shorts["min_duration_sec"])
    shorts["target_duration_sec"] = int(shorts["target_duration_sec"])
    shorts["max_duration_sec"] = int(shorts["max_duration_sec"])
    shorts["max_candidates"] = int(shorts["max_candidates"])
    shorts["final_candidates"] = int(shorts["final_candidates"])
    if shorts["subtitles_enabled"] is not True:
        shorts["subtitles_enabled"] = bool(shorts["subtitles_enabled"])
    if not 1 <= shorts["min_duration_sec"] <= shorts["max_duration_sec"]:
        raise ValueError("Профиль shorts: минимум должен быть не больше максимума.")
    if not shorts["min_duration_sec"] <= shorts["target_duration_sec"] <= shorts["max_duration_sec"]:
        raise ValueError("Профиль shorts: target должен находиться между минимумом и максимумом.")
    if shorts["aspect_ratio"] not in ("9:16",):
        raise ValueError("Профиль shorts должен иметь aspect_ratio 9:16.")
    if shorts["crop_mode"] not in ("center", "speaker", "auto"):
        raise ValueError("Недопустимый crop_mode профиля shorts.")
    profiles["shorts"] = shorts
    return profiles


def profile_settings(settings):
    """Плоские настройки → структура профилей (обратная совместимость)."""
    return normalize_profiles(settings.get("outputs"))


def report_profiles(report):
    """Профили из отчёта (поле ``profiles`` или настройки запуска анализа)."""
    if isinstance(report.get("profiles"), dict):
        return report["profiles"]
    return normalize_profiles((report.get("settings") or {}).get("outputs"))


def candidate_format(candidate):
    """Отсутствует / ``highlight`` → длинный хайлайт; ``short`` → Shorts."""
    value = candidate.get("format")
    if value is None:
        return "highlight"
    if value not in FORMATS:
        raise ValueError(f"Недопустимый формат кандидата: {value!r}.")
    return value


def profile_for_format(profiles, fmt):
    """Профиль по формату: хайлайты всегда, shorts только при включении."""
    fmt = candidate_format({"format": fmt})
    if fmt == "short":
        shorts = profiles["shorts"]
        if not shorts.get("enabled"):
            raise ValueError("Shorts отключены в настройках профиля (outputs.shorts.enabled=false).")
        return shorts
    return profiles["highlights"]


def validate_candidate_format(profiles, candidate):
    """Проверяет согласованность ID, формата и ограничений длительности профиля."""
    fmt = candidate_format(candidate)
    profile = profile_for_format(profiles, fmt)
    expected = "s" if fmt == "short" else "c"
    cid = candidate.get("candidate_id", "")
    if not cid.startswith(expected):
        raise ValueError(
            f"Кандидат {cid!r}: префикс ID должен быть '{expected}' для формата '{fmt}'.")
    start, end = float(candidate["start"]), float(candidate["end"])
    duration = candidate.get("duration_sec")
    if duration is not None:
        validate_interval(start, end, duration, profile["min_duration_sec"], profile["max_duration_sec"])
    else:
        if not 0 <= start < end:
            raise ValueError("Нужны границы 0 ≤ начало < конец.")
        if end - start < profile["min_duration_sec"] - 0.001:
            raise ValueError(f"Клип короче минимальной длительности {profile['min_duration_sec']} с.")
        if end - start > profile["max_duration_sec"] + 0.001:
            raise ValueError(f"Клип длиннее максимальной длительности {profile['max_duration_sec']} с.")
    return fmt


def migrate_report(report):
    """Дополняет старый отчёт полями формата, не трогая утверждения и границы."""
    if not isinstance(report, dict):
        raise ValueError("Отчёт должен быть объектом.")
    profiles = normalize_profiles((report.get("settings") or {}).get("outputs"))
    report["profiles"] = profiles
    for candidate in report.get("candidates", []):
        if "format" not in candidate:
            candidate["format"] = "highlight"
    # Добавляем пустой список Shorts, если его нет (обратная совместимость)
    if "shorts_candidates" not in report:
        report["shorts_candidates"] = []
    return report
