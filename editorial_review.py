"""Independent editorial check with adjacent context; never grants human approval."""
from pathlib import Path
import hashlib

from analyzer import ModelValidationError, ask, interval_iou
from job_store import load_json, save_json, validate_interval
from llm_context import INITIAL_PROMPT_BYTES, byte_size, sentence_json
from interval_builder import make_interval

REVIEW_VERSION = 5
SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["keep", "revise", "reject"]},
    "start_id": {"type": "string"}, "end_id": {"type": "string"},
    "title": {"type": "string"}, "reason": {"type": "string"},
    "single_topic": {"type": "boolean"}, "self_contained": {"type": "boolean"},
    "complete_ending": {"type": "boolean"},
    "score": {"type": "integer", "minimum": 0, "maximum": 100}},
    "required": ["decision", "start_id", "end_id", "title", "reason", "single_topic",
                 "self_contained", "complete_ending", "score"], "additionalProperties": False}


def review_report(raw_report_path, log, model=None):
    raw_report_path = Path(raw_report_path)
    raw = load_json(raw_report_path)
    transcript = load_json(raw_report_path.parent / "transcript.json")
    settings = dict(raw["settings"])
    chosen_model = model or settings.get("editorial_model") or settings["llm_model"]
    settings.update(llm_model=chosen_model, llm_context_tokens=16384)
    model_key = hashlib.sha256(chosen_model.encode()).hexdigest()[:8]
    cache = raw_report_path.parent.parent / f"{raw['signature']}_editorial_v{REVIEW_VERSION}_{model_key}"
    cache.mkdir(parents=True, exist_ok=True)
    report_path = cache / "report.json"
    if report_path.exists():
        log("Используется сохранённый редакционный отчёт и решения ревью.")
        return report_path
    if any(c["status"] != "pending" for c in raw["candidates"]):
        raise ValueError("Исходный отчёт уже содержит решения редактора. Повторная автоматическая правка не выполнена.")
    save_json(cache / "transcript.json", transcript)
    kept, rejected, warnings = [], [], []
    for index, candidate in enumerate(raw["candidates"]):
        log(f"Редакционная проверка: {index + 1}/{len(raw['candidates'])}")
        sentences = [s for s in transcript["sentences"]
                     if s["end"] >= candidate["start"] - 90 and s["start"] <= candidate["end"] + 90]
        allowed = {s["id"]: s for s in sentences}
        interval_mode = raw.get("selection_method") == "intervals"
        semantic_end = candidate.get("end_padding", {}).get("base_end", candidate["end"])

        def validate(value):
            if not isinstance(value, dict) or any(key not in value for key in SCHEMA["required"]):
                raise ValueError("Неполное редакционное решение.")
            if value["decision"] not in ("keep", "revise", "reject"):
                raise ValueError("Неверное решение.")
            if type(value["score"]) is not int or not 0 <= value["score"] <= 100:
                raise ValueError("Нужна оценка 0–100.")
            if not all(isinstance(value[k], str) and value[k].strip() for k in ("reason", "title")):
                raise ValueError("Нужны заголовок и обоснование.")
            flags = ("single_topic", "self_contained", "complete_ending")
            if any(type(value[key]) is not bool for key in flags):
                raise ValueError("Редакционные признаки должны быть boolean.")
            if value["decision"] == "reject":
                return
            if not all(value[key] for key in flags):
                raise ValueError("Несвязный, несамодостаточный или оборванный клип нужно исправить либо отклонить.")
            if value["start_id"] not in allowed or value["end_id"] not in allowed:
                raise ValueError("Выбраны неизвестные ID предложений.")
            start, end = allowed[value["start_id"]]["start"], allowed[value["end_id"]]["end"]
            try:
                if interval_mode:
                    make_interval(allowed[value["start_id"]], allowed[value["end_id"]], transcript["duration_sec"], settings)
                else:
                    validate_interval(start, end, transcript["duration_sec"], settings["min_duration_sec"], settings["max_duration_sec"])
            except ValueError as exc:
                feasible = [(s["id"], round(s["end"] - start, 2)) for s in sentences
                            if settings["min_duration_sec"] <= s["end"] - start <= settings["max_duration_sec"]]
                if len(feasible) > 10:
                    feasible = [feasible[round(i * (len(feasible) - 1) / 9)] for i in range(10)]
                raise ValueError(f"Фактическая длина {end - start:.2f} с. {exc} Допустимые end_id и длительности для этого начала: {feasible}. "
                                 "Если смыслового окончания нет, отклони клип.") from exc

        path = cache / f"{candidate['candidate_id']}_decision.json"
        if path.exists():
            decision = load_json(path)
            validate(decision)
        else:
            before = [s for s in sentences if s["end"] <= candidate["start"]]
            inside = [s for s in sentences if s["start"] >= candidate["start"] and s["end"] <= semantic_end]
            after = [s for s in sentences if s["start"] >= semantic_end]
            instructions = ("Проверь чужую монтажную заявку СКЕПТИЧЕСКИ. Предыдущий редактор часто ошибается. "
                      "Не пересказывай его обоснование и не считай его оценку доказательством качества. "
                      "Проверь ВСЕ предложения выбранного интервала, а также контекст до/после. "
                      "Нужен непрерывный горизонтальный хайлайт с одной законченной мыслью, не анонс и не набор тем. "
                      "Оценивай содержание ТОЛЬКО блока КЛИП. Блоки ДО и ПОСЛЕ не входят в клип: это лишь подсказки для границ. "
                      "Смена темы ПОСЛЕ end_id — нормальный признак завершения, а не причина отклонения клипа. "
                      "Связанные примеры, сравнения стран и аргументы внутри одного тезиса не являются разными темами. "
                      "Не требуй целой лекции: законченного локального вывода достаточно. Короткое обращение к чату не обязательно портит весь клип. "
                      "Приветствия, просьбы о донатах, анонсы будущей беседы и перекличка с чатом сами по себе не хайлайт. "
                      "Нельзя заканчивать на первой фразе новой истории или до ответа на поставленный вопрос. "
                      "Не называй целостным клип, в котором после короткой истории идут другие темы и ответы чату. "
                      "Начало должно вводить предмет разговора: ссылки 'такого', 'она пишет', 'это' без понятного референта — проблема. "
                      "Возьми соседние предложения для исправления границ, если это сохраняет единый смысл и допустимую длину. "
                      "Сначала попробуй подобрать законченный подинтервал или слегка сдвинуть границы; reject только если это невозможно. "
                      "Если самодостаточный клип нужной длины невозможен, decision=reject. Не растягивай случайным текстом. "
                      "Оцени самодостаточность, завершённость, связность и интересность: 90+ исключительно сильный, 70–89 хороший, "
                      "50–69 требует заметных правок, ниже 50 слабый. Не ставь всем одинаковую оценку по умолчанию. "
                      "В title/reason только содержание и редакционные причины, без расчётов длительности и номеров ID. "
                      "ID — непрозрачные идентификаторы, НЕ секунды; вычитать номера ID запрещено. "
                      f"Исходная заявка: start_id={candidate['start_id']}, end_id={candidate['end_id']}, "
                      f"длительность, ВЫЧИСЛЕННАЯ КОДОМ: {candidate['end'] - candidate['start']:.2f} секунд.\n"
                      f"Допустимая длина строго {settings['min_duration_sec']}–{settings['max_duration_sec']} секунд. "
                      "Верни keep, revise либо reject и ID границ.\n")

            def build_prompt():
                return (instructions + f"ДО КЛИПА (не входит в клип): {sentence_json(before)}\n"
                        f"КЛИП (именно его проверь): {sentence_json(inside)}\n"
                        f"ПОСЛЕ КЛИПА (не входит в клип): {sentence_json(after)}")

            prompt = build_prompt()
            # Trim only the farthest optional context; the selected clip stays complete.
            while byte_size(prompt) > INITIAL_PROMPT_BYTES and (before or after):
                if before and (not after or byte_size(sentence_json(before)) >= byte_size(sentence_json(after))):
                    before.pop(0)
                else:
                    after.pop()
                prompt = build_prompt()
            if byte_size(prompt) > INITIAL_PROMPT_BYTES:
                warning = (f"{candidate['candidate_id']}: полный текст клипа не помещается в редакционный запрос даже без "
                           "соседнего контекста. Автоматическая проверка не выполнена; требуется ручное ревью.")
                warnings.append(warning)
                log(warning)
                pending = dict(candidate, status="pending", editorial_decision="needs_review",
                               warnings=list(candidate.get("warnings", [])) + [warning])
                pending.pop("approval_key", None)
                kept.append(pending)
                continue
            allowed = {s["id"]: s for s in before + inside + after}
            try:
                decision = ask(settings, prompt, SCHEMA, validate, audit_path=cache / f"{candidate['candidate_id']}_attempts.json")
            except ModelValidationError:
                warning = (f"{candidate['candidate_id']}: редактор трижды вернул недопустимое решение; "
                           "исходные валидные границы сохранены для ручного ревью.")
                warnings.append(warning)
                log(warning)
                pending = dict(candidate, status="pending", editorial_decision="needs_review",
                               warnings=list(candidate.get("warnings", [])) + [warning])
                pending.pop("approval_key", None)
                kept.append(pending)
                continue
            save_json(path, decision)
        if decision["decision"] == "reject":
            rejected.append({"candidate_id": candidate["candidate_id"], "reason": decision["reason"]})
            continue
        revised = dict(candidate, start_id=decision["start_id"], end_id=decision["end_id"],
                       start=allowed[decision["start_id"]]["start"], end=allowed[decision["end_id"]]["end"],
                       title=decision["title"], reason=decision["reason"], score=decision["score"], status="pending",
                       selection_score=candidate["score"], editorial_decision=decision["decision"],
                        warnings=["Повторно проверено LLM. Требуется ваше утверждение по исходному видео."])
        if interval_mode:
            revised.update(make_interval(allowed[decision["start_id"]], allowed[decision["end_id"]],
                                         transcript["duration_sec"], settings))
        revised.pop("approval_key", None)
        kept.append(revised)
    ranked = []
    for candidate in sorted(kept, key=lambda c: c["score"], reverse=True):
        if not any(interval_iou(candidate, other) >= 0.35 for other in ranked):
            ranked.append(candidate)
        else:
            rejected.append({"candidate_id": candidate["candidate_id"], "reason": "Пересечение с более сильным кандидатом после редакционной правки."})
    report = dict(raw, signature=cache.name, candidates=ranked, editorial_version=REVIEW_VERSION,
                  settings=dict(raw["settings"], editorial_model=chosen_model),
                  editorial_context_tokens=settings["llm_context_tokens"],
                  editorial_warnings=warnings,
                  raw_report_path=str(raw_report_path.resolve()), editorial_rejections=rejected)
    save_json(report_path, report)
    job_dir = raw_report_path.parent.parent.parent
    save_json(job_dir / "latest_report.json", {"path": str(report_path.resolve())})
    log(f"После редакционной проверки: {len(ranked)} кандидатов, отклонено {len(rejected)}. Ожидается ваше ревью.")
    return report_path
