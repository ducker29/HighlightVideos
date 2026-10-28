"""PASS 2A proposes starts; PASS 2B chooses only prevalidated interval IDs."""
import hashlib
import json
from pathlib import Path

from analyzer import ModelValidationError, ask
from interval_builder import build_interval_options, make_interval
from job_store import load_json, save_json
from llm_context import excerpt, fit_option_batches, fit_sentence_groups, sentence_json

SELECTION_VERSION = 1


def starts_schema(ids):
    return {"type": "object", "properties": {"starts": {"type": "array", "maxItems": 3,
            "items": {"type": "object", "properties": {"start_id": {"type": "string", "enum": ids},
                     "topic": {"type": "string"}, "reason": {"type": "string"}},
                     "required": ["start_id", "topic", "reason"], "additionalProperties": False}}},
            "required": ["starts"], "additionalProperties": False}


def choice_schema(ids):
    return {"type": "object", "properties": {
            "interval_id": {"type": "string", "enum": ["none"] + ids},
            "title": {"type": "string"}, "reason": {"type": "string"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["interval_id", "title", "reason", "score"], "additionalProperties": False}


def start_prompt(part, eligible, topics, settings):
    allowed = [s["id"] for s in part if s["id"] in eligible]
    return ("Найди до трёх перспективных НАЧАЛ законченных разговорных хайлайтов. Не выбирай конец и не вычисляй длительность. "
            "Это горизонтальные клипы, не Shorts. Начало должно понятно вводить историю, вопрос или тезис. "
            "Не выбирай приветствия, анонсы и просьбы о донатах. Не выбирай отсылки к отсутствующему контексту. "
            "Несколько примеров одного тезиса допустимы; фрагмент должен иметь шанс закончиться естественным выводом. "
            "Для перечисленных разрешённых начал программа уже нашла окончания нужной длины. "
            "Если сильных начал нет, верни starts=[].\n"
            f"Целевая длина готового клипа {settings['min_duration_sec']}–{settings['max_duration_sec']} с; время считает код.\n"
            f"Темы блока: {topics}\nРазрешённые start_id: {json.dumps(allowed)}\n"
            f"Предложения: {sentence_json(part)}")


def end_prompt(options, sentences, topic):
    first, last = options[0]["start"], max(o["end_padding"]["base_end"] for o in options)
    content = [s for s in sentences if s["start"] >= first and s["end"] <= last]
    before = [s for s in sentences if s["end"] <= first][-2:]
    table = [{k: o[k] for k in ("interval_id", "end_id", "end", "duration_sec", "ending_text")} for o in options]
    return ("Выбери лучший непрерывный самодостаточный хайлайт с фиксированным началом. "
            "Длительности и концевой запас УЖЕ вычислены и проверены программой. Не считай номера ID секундами. "
            "Верни только interval_id из списка, заголовок, редакционное обоснование и score 0–100. "
            "В обосновании не пересчитывай длительность. Начало должно быть понятным, конец — завершать мысль. "
            "Начало новой истории, вопрос без ответа и оборванный аргумент не являются хорошим окончанием. "
            "Если ни один вариант не подходит по смыслу, interval_id=none, title='', score=0; объясни причину. "
            "Общий текст содержит продолжение ПОСЛЕ коротких вариантов: оно не входит в них. "
            "У каждого варианта собственный end_id и точная цитата окончания. Текст ДО начала — только контекст.\n"
            f"Рабочая тема: {excerpt(topic, 500)}\nФиксированное начало: {options[0]['start_id']} ({first:g} с).\n"
            f"ДО начала (не входит в клип): {sentence_json(before)}\n"
            f"Полный текст для сравнения окончаний: {sentence_json(content)}\n"
            f"Допустимые варианты: {json.dumps(table, ensure_ascii=False, separators=(',', ':'))}")


def _cached_ask(path, settings, prompt, schema, validate, warnings, log, label):
    if path.exists():
        saved = load_json(path)
        if saved.get("validation_failed"):
            warnings.append(saved["warning"])
            return None
        validate(saved)
        return saved
    audit_path = path.with_name(path.stem + "_attempts.json")
    previous = load_json(audit_path) if audit_path.exists() else []
    if previous and previous[-1].get("valid"):
        saved = json.loads(previous[-1]["reply"])
        validate(saved)
        save_json(path, saved)
        return saved
    try:
        value = ask(settings, prompt, schema, validate,
                    audit_path=audit_path, previous_attempts=previous)
    except ModelValidationError:
        warning = f"{label}: модель не выбрала допустимое значение за три попытки; часть отбора пропущена."
        warnings.append(warning)
        log(warning)
        save_json(path, {"validation_failed": True, "warning": warning})
        return None
    save_json(path, value)
    return value


def select_intervals(block, transcript, settings, directory, log):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result_path = directory / "selection.json"
    if result_path.exists():
        return load_json(result_path)
    sentences = sorted({s["id"]: s for w in block for s in w["sentences"]}.values(), key=lambda s: s["start"])
    unique = {s["id"]: s for s in sentences}
    duration = transcript["duration_sec"]
    options_by_start = {s["id"]: build_interval_options(sentences, s["id"], duration, settings) for s in sentences}
    eligible = {key for key, value in options_by_start.items() if value}
    topics = " / ".join(excerpt(w["analysis"]["topic"], 120) for w in block[:12])
    warnings, starts, candidates = [], {}, []
    stats = {"start_parts": 0, "proposed_starts": 0, "options": 0, "presented_options": 0,
             "oversized_options": 0, "end_batches": 0, "no_match_batches": 0}
    if eligible:
        parts = fit_sentence_groups(sentences, lambda part: start_prompt(part, eligible, topics, settings), overlap_seconds=30)
        for index, part in enumerate(parts):
            ids = [s["id"] for s in part if s["id"] in eligible]
            if not ids:
                continue
            log(f"PASS 2A: поиск начал, часть {index + 1}/{len(parts)}")
            stats["start_parts"] += 1
            key = hashlib.sha256(sentence_json(part).encode()).hexdigest()[:12]

            def validate_starts(value):
                if not isinstance(value, dict) or not isinstance(value.get("starts"), list) or len(value["starts"]) > 3:
                    raise ValueError("Нужен starts с массивом до трёх начал.")
                for proposal in value["starts"]:
                    if not isinstance(proposal, dict) or proposal.get("start_id") not in ids:
                        raise ValueError("Выбери только start_id из разрешённого списка.")
                    if any(not isinstance(proposal.get(k), str) or not proposal[k].strip() for k in ("topic", "reason")):
                        raise ValueError("Для начала нужны тема и обоснование.")

            value = _cached_ask(directory / f"starts_{key}.json", settings, start_prompt(part, eligible, topics, settings),
                                starts_schema(ids), validate_starts, warnings, log, "PASS 2A")
            if value:
                starts.update({p["start_id"]: p for p in value["starts"]})
    else:
        log("В тематическом блоке нет интервалов заданной длины с учётом запаса.")
    stats["proposed_starts"] = len(starts)
    for start_id, proposal in starts.items():
        options = options_by_start[start_id]
        save_json(directory / f"options_{start_id}.json", options)
        build_prompt = lambda group: end_prompt(group, sentences, proposal["topic"])
        batches, oversized = fit_option_batches(options, build_prompt)
        stats["options"] += len(options)
        stats["oversized_options"] += len(oversized)
        if oversized:
            warning = f"Начало {start_id}: {len(oversized)} вариантов не помещаются целиком в контекст; они не проверены моделью."
            warnings.append(warning)
            log(warning)
            save_json(directory / f"oversized_{start_id}.json", [o["interval_id"] for o in oversized])
        for index, batch in enumerate(batches):
            log(f"PASS 2B: начало {start_id}, варианты {index + 1}/{len(batches)}")
            allowed = {o["interval_id"]: o for o in batch}
            stats["end_batches"] += 1
            stats["presented_options"] += len(batch)
            key = hashlib.sha256(json.dumps(list(allowed)).encode()).hexdigest()[:12]

            def validate_choice(value):
                if not isinstance(value, dict) or value.get("interval_id") not in ["none"] + list(allowed):
                    raise ValueError("Нужен interval_id из предоставленного списка либо none.")
                if type(value.get("score")) is not int or not 0 <= value["score"] <= 100:
                    raise ValueError("Нужна целая оценка 0–100.")
                if not isinstance(value.get("reason"), str) or not value["reason"].strip():
                    raise ValueError("Нужно редакционное обоснование.")
                if value["interval_id"] != "none" and (not isinstance(value.get("title"), str) or not value["title"].strip()):
                    raise ValueError("Для выбранного клипа нужен заголовок.")

            choice = _cached_ask(directory / f"end_{start_id}_{key}.json", settings, build_prompt(batch),
                                 choice_schema(list(allowed)), validate_choice, warnings, log, "PASS 2B")
            if not choice or choice["interval_id"] == "none":
                stats["no_match_batches"] += 1
                continue
            option = allowed[choice["interval_id"]]
            # Verify the cached catalog against source timestamps, not model-provided numbers.
            checked = make_interval(unique[option["start_id"]], unique[option["end_id"]], duration, settings)
            candidate = dict(checked, title=choice["title"], reason=choice["reason"], score=choice["score"],
                             selection_interval_id=checked["interval_id"], status="pending",
                             warnings=["Интервал выбран из допустимых по длительности; смысловые границы требуют ревью."])
            candidates.append(candidate)
    result = {"candidates": candidates, "warnings": warnings, "complete": not warnings,
              "selection_version": SELECTION_VERSION, "statistics": stats}
    save_json(result_path, result)
    return result
