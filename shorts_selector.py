"""Independent Shorts candidate discovery.

Separate pass, catalog, and cache from the highlight pipeline. Reuses PASS 1
artifacts (block sentences + analysis) and the transcript. The LLM selects only
from a program-generated catalog of interval_ids with fixed durations — it can
never invent timestamps.
"""
import hashlib
import json
from pathlib import Path

from analyzer import ModelValidationError, ask, interval_iou
from interval_builder import build_interval_options
from job_store import normalize_profiles, profile_for_format, save_json, validate_interval
from llm_context import INITIAL_PROMPT_BYTES, fit_option_batches, sentence_json

SHORTS_SELECTION_VERSION = 2

START_PROMPT_TEMPLATE = ("Выбери до трёх сильных НАЧАЛ для вертикальных Shorts из тематического блока. "
                         "Shorts должен быть самодостаточным: понятный контекст, сильный захват "
                         "в первые секунды, одна законченная мысль. Не анонс, не переход, не набор тем. "
                         "Каждое начало — start_id существующего предложения и краткое обоснование. "
                         "Если сильных начал нет, верни starts=[]. "
                         "ID — непрозрачные идентификаторы, не секунды; вычитать номера ID запрещено.\n"
                         "Тема блока: {topic}\n"
                         "Кандидаты на начало (id, sec, text): {start_rows}\n"
                         "Полный текст блока: {sentences}")


def shorts_cache_signature(settings):
    """Вычисляет отдельную сигнатуру для кеша Shorts, зависящую только от настроек Shorts.

    В отличие от window_signature (который включает настройки Highlights),
    эта сигнатура меняется при изменении только настроек Shorts.
    """
    profiles = normalize_profiles(settings.get("outputs"))
    try:
        profile = profile_for_format(profiles, "short")
    except ValueError:
        return None
    key = {
        "min": profile["min_duration_sec"],
        "max": profile["max_duration_sec"],
        "target": profile.get("target_duration_sec"),
        "padding": profile.get("end_padding_sec", 0.5),
        "version": SHORTS_SELECTION_VERSION,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def shorts_full_key(window_signature, settings):
    """Формирует итоговый ключ кеша Shorts из базовой сигнатуры анализа и настроек Shorts.

    Итоговый ключ гарантирует, что кеш не будет переиспользован при изменении:
    - транскрипта, модели или диапазона анализа (через window_signature);
    - настроек Shorts (через shorts_cache_signature).

    Изменение только настроек Shorts не влияет на window_signature (Highlights).
    """
    shorts_sig = shorts_cache_signature(settings)
    if shorts_sig is None:
        return None
    combined = f"{window_signature}_{shorts_sig}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def _read_json_lenient(path):
    """Читает кеш Shorts: возвращает None при повреждённом JSON, чтобы пересоздать артефакт.

    Локальная защита от частичной записи / битых файлов: повреждённый артефакт игнорируется
    и пересоздаётся обычным путём. Ошибки LLM и ошибки записи не скрываются — они не
    возникают здесь.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_catalog_ok(catalog):
    """Проверяет, что кеш-каталог структурно корректен."""
    return (isinstance(catalog, dict)
            and isinstance(catalog.get("options"), list)
            and isinstance(catalog.get("sentence_ids"), list)
            and all(isinstance(o, dict) and "interval_id" in o for o in catalog["options"]))


def _shorts_profile(settings):
    profiles = normalize_profiles(settings.get("outputs"))
    try:
        return profile_for_format(profiles, "short")
    except ValueError:
        return None


def starts_schema(ids):
    return {"type": "object", "properties": {"starts": {"type": "array", "maxItems": 3,
            "items": {"type": "object", "properties": {
                "start_id": {"type": "string", "enum": ids},
                "topic": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["start_id", "topic", "reason"], "additionalProperties": False}}},
            "required": ["starts"], "additionalProperties": False}


def choice_schema(ids):
    return {"type": "object", "properties": {
            "interval_id": {"type": "string", "enum": ["none"] + ids},
            "title": {"type": "string"}, "reason": {"type": "string"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["interval_id", "title", "reason", "score"], "additionalProperties": False}


def _build_shorts_catalog(sentences, duration, profile, cache_dir):
    catalog_path = cache_dir / "catalog.json"
    catalog = _read_json_lenient(catalog_path)
    if not _is_catalog_ok(catalog):
        profile_settings = {"min_duration_sec": profile["min_duration_sec"],
                            "max_duration_sec": profile["max_duration_sec"],
                            "end_padding_sec": profile.get("end_padding_sec", 0.5)}
        options = []
        for sentence in sentences:
            options.extend(build_interval_options(sentences, sentence["id"], duration, profile_settings))
        catalog = {"options": options, "profile": profile_settings,
                   "sentence_ids": [s["id"] for s in sentences]}
        save_json(catalog_path, catalog)
    return catalog


EMPTY_STATS = {"options": 0, "presented_options": 0, "oversized_options": 0,
               "no_match_batches": 0, "selected": 0}


def _empty():
    return {"candidates": [], "warnings": [], "complete": True,
            "statistics": dict(EMPTY_STATS)}


def select_shorts(block, transcript, settings, cache_dir, log):
    """Run the independent Shorts selection pass for one thematic block.

    Returns a dict with candidates (tagged format='shorts'), warnings, complete, statistics.
    All artifacts are written under cache_dir, which must be distinct from the highlight cache.
    """
    profile = _shorts_profile(settings)
    if not profile:
        return _empty()
    sentences = sorted({s["id"]: s for w in block for s in w["sentences"]}.values(),
                       key=lambda s: s["start"])
    if not sentences:
        return _empty()
    cache_dir.mkdir(parents=True, exist_ok=True)
    duration = transcript["duration_sec"]
    min_sec, max_sec = profile["min_duration_sec"], profile["max_duration_sec"]
    topic = block[-1]["analysis"].get("topic", "Блок")
    profile_settings = {"min_duration_sec": min_sec, "max_duration_sec": max_sec,
                        "end_padding_sec": profile.get("end_padding_sec", 0.5)}

    catalog = _build_shorts_catalog(sentences, duration, profile, cache_dir)
    options = catalog["options"]
    if not options:
        return {"candidates": [], "warnings": ["Нет допустимых Shorts-интервалов в блоке."],
                "complete": True, "statistics": dict(EMPTY_STATS)}

    sentences_by_id = {s["id"]: s for s in sentences}
    unique_starts = [s["id"] for s in sentences]

    # --- PASS 2A-Shorts: pick up to 3 strong start sentences ---
    starts_path = cache_dir / "starts.json"
    starts = None
    cached = _read_json_lenient(starts_path)
    if (isinstance(cached, list) and cached
            and all(isinstance(entry, dict) and entry.get("start_id") in unique_starts
                    for entry in cached)):
        starts = cached
    if starts is None:
        prompt = START_PROMPT_TEMPLATE.format(
            topic=topic,
            start_rows=json.dumps([{"id": s["id"], "sec": s["start"], "text": s["text"][:90]}
                                   for s in sentences[:24]], ensure_ascii=False, separators=(",", ":")),
            sentences=sentence_json(sentences))

        def validate_starts(value):
            if not isinstance(value, dict) or not isinstance(value.get("starts"), list):
                raise ValueError("Нужен массив starts.")
            for item in value["starts"]:
                if not isinstance(item, dict) or item.get("start_id") not in unique_starts:
                    raise ValueError("start_id должен быть из разрешённого списка.")
                if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("topic", "reason")):
                    raise ValueError("Для начала нужны тема и обоснование.")
            return True

        try:
            result = ask(settings, prompt, starts_schema(unique_starts), validate_starts,
                         audit_path=cache_dir / "starts_attempts.json")
            starts = result["starts"]
        except ModelValidationError as exc:
            warning = f"PASS 2A-Shorts: три попытки не дали валидного ответа; " \
                      f"подборка по каталогу. {exc}"
            log(warning)
            starts = [{"start_id": sid, "topic": topic, "reason": "Автоматическая подборка."}
                      for sid in unique_starts
                      if any(o["start_id"] == sid for o in options)][:3]
        save_json(starts_path, starts)

    # --- PASS 2B-Shorts: for each start, pick the best end from the catalog ---
    candidates = []
    warnings = []
    no_match = 0
    presented = 0
    oversized_total = 0
    seen = set()

    for start_entry in starts:
        start_id = start_entry["start_id"]
        block_options = [o for o in options if o["start_id"] == start_id]
        if not block_options:
            continue
        by_id = {o["interval_id"]: o for o in block_options}
        option_ids = list(by_id)

        def build_end(batch, _by_id=by_id, _sbi=sentences_by_id):
            table = [{k: o[k] for k in ("interval_id", "end_id", "end", "duration_sec", "ending_text")}
                     for o in batch]
            first = batch[0]["start"]
            last = max(o["end_padding"]["base_end"] for o in batch)
            content = [s for s in sentences if s["start"] >= first and s["end"] <= last]
            before = [s for s in sentences if s["end"] <= first][-2:]
            return ("Из предложенных интервалов выбери ЛУЧШЕЕ окончание для одного Shorts. "
                    f"Целевая длительность ~{profile.get('target_duration_sec', 40)} с, "
                    f"максимум {max_sec} с. "
                    "Начало фиксировано — выбирай только interval_id из списка. "
                    "Если ни один не подходит, верни interval_id=none. "
                    "ID — непрозрачные идентификаторы, не секунды.\n"
                    f"Тема: {topic}\n"
                    f"Фиксированное начало: {start_id}.\n"
                    f"ДО начала (не входит в клип): {sentence_json(before)}\n"
                    f"Полный текст для сравнения окончаний: {sentence_json(content)}\n"
                    f"Допустимые варианты: {json.dumps(table, ensure_ascii=False, separators=(',', ':'))}")

        batches, oversized = fit_option_batches(block_options, build_end,
                                                 budget=INITIAL_PROMPT_BYTES)
        oversized_total += len(oversized)
        if oversized:
            warning = f"Shorts, старт {start_id}: {len(oversized)} вариантов не помещаются в контекст."
            warnings.append(warning)
            log(warning)

        for batch in batches:
            presented += len(batch)
            batch_key = hashlib.sha256(
                json.dumps([o["interval_id"] for o in batch], sort_keys=True).encode()
            ).hexdigest()[:12]
            batch_path = cache_dir / f"end_{batch_key}.json"
            cached = _read_json_lenient(batch_path)
            choice = cached if isinstance(cached, dict) else None
            if choice is not None:
                if choice.get("validation_failed"):
                    no_match += 1
                    continue
            else:
                allowed_ids = [o["interval_id"] for o in batch]

                def validate_choice(value, _allowed=allowed_ids):
                    if not isinstance(value, dict) or value.get("interval_id") not in ["none"] + _allowed:
                        raise ValueError("Нужен interval_id из предоставленного списка либо none.")
                    if type(value.get("score")) is not int or not 0 <= value["score"] <= 100:
                        raise ValueError("Нужна целая оценка 0–100.")
                    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
                        raise ValueError("Нужно редакционное обоснование.")
                    if value["interval_id"] != "none" and \
                       (not isinstance(value.get("title"), str) or not value["title"].strip()):
                        raise ValueError("Для выбранного клипа нужен заголовок.")
                    return True

                try:
                    choice = ask(settings, build_end(batch), choice_schema(allowed_ids),
                                 validate_choice,
                                 audit_path=cache_dir / f"end_{batch_key}_attempts.json")
                except ModelValidationError:
                    warning = f"Shorts, старт {start_id}: модель не выбрала за три попытки."
                    warnings.append(warning)
                    log(warning)
                    save_json(batch_path, {"validation_failed": True, "warning": warning})
                    no_match += 1
                    continue
                save_json(batch_path, choice)
            if choice.get("interval_id") in (None, "none"):
                no_match += 1
                continue
            opt = by_id[choice["interval_id"]]
            if opt["interval_id"] in seen:
                continue
            seen.add(opt["interval_id"])
            candidate = {**opt, "title": choice["title"], "reason": choice["reason"],
                         "score": choice["score"], "status": "pending", "format": "shorts"}
            validate_interval(candidate["start"], candidate["end"], duration, min_sec, max_sec)
            candidates.append(candidate)

    # --- Deduplicate by IoU (Shorts-specific threshold) ---
    ranked = []
    for candidate in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if not any(interval_iou(candidate, kept) >= 0.30 for kept in ranked):
            ranked.append(candidate)
    for index, candidate in enumerate(ranked, 1):
        candidate["candidate_id"] = f"sc{index:03d}"

    return {"candidates": ranked, "warnings": warnings,
            "complete": not warnings,
            "statistics": {"options": len(options),
                           "presented_options": presented,
                           "oversized_options": oversized_total,
                           "no_match_batches": no_match,
                           "selected": len(ranked)}}
