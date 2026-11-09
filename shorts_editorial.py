"""Independent editorial check for Shorts candidates; never grants human approval.

Reuses transcript_review.py for context display and analyzer.ask for LLM calls.
Checks are Shorts-specific: strong opening, self-contained, complete ending,
no trailing pause, single topic, not a fragment of a larger story.
"""
from pathlib import Path
import hashlib
import json

from analyzer import ModelValidationError, ask, interval_iou
from job_store import load_json, save_json, normalize_profiles, profile_for_format
from llm_context import INITIAL_PROMPT_BYTES, byte_size, sentence_json
from transcript_review import review_sections

REVIEW_VERSION = 1
SHORTS_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["keep", "revise", "reject"]},
        "start_id": {"type": "string"},
        "end_id": {"type": "string"},
        "title": {"type": "string"},
        "reason": {"type": "string"},
        "strong_opening": {"type": "boolean"},
        "self_contained": {"type": "boolean"},
        "complete_ending": {"type": "boolean"},
        "single_topic": {"type": "boolean"},
        "no_trailing_pause": {"type": "boolean"},
        "not_a_fragment": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["decision", "start_id", "end_id", "title", "reason",
                 "strong_opening", "self_contained", "complete_ending",
                 "single_topic", "no_trailing_pause", "not_a_fragment", "score"],
    "additionalProperties": False,
}


def _validate_shorts(value, allowed_ids, min_sec, max_sec, duration_sec):
    if not isinstance(value, dict) or any(key not in value for key in SHORTS_SCHEMA["required"]):
        raise ValueError("Неполное редакционное решение для Shorts.")
    if value["decision"] not in ("keep", "revise", "reject"):
        raise ValueError("Неверное решение.")
    if type(value["score"]) is not int or not 0 <= value["score"] <= 100:
        raise ValueError("Нужна оценка 0–100.")
    if not all(isinstance(value[k], str) and value[k].strip() for k in ("reason", "title")):
        raise ValueError("Нужны заголовок и обоснование.")
    flags = ("strong_opening", "self_contained", "complete_ending",
             "single_topic", "no_trailing_pause", "not_a_fragment")
    if any(type(value[key]) is not bool for key in flags):
        raise ValueError("Редакционные признаки Shorts должны быть boolean.")
    if value["decision"] == "reject":
        return
    if not all(value[key] for key in flags):
        raise ValueError(
            "Short со слабым началом, без самостоятельности, оборванной мыслью, "
            "сменой темы, паузой или являющийся фрагментом нужно исправить либо отклонить.")
    if value["start_id"] not in allowed_ids or value["end_id"] not in allowed_ids:
        raise ValueError("Выбраны неизвестные ID предложений.")


def review_shorts_candidates(report_path, log, model=None):
    """Run editorial review on shorts_candidates in a report.

    Returns the path to the updated report. Shorts candidates get
    editorial_decision, updated score, and warnings. Never sets status to approved.
    """
    report_path = Path(report_path)
    report = load_json(report_path)
    transcript = load_json(report_path.parent / "transcript.json")
    settings = dict(report["settings"])
    profiles = normalize_profiles(settings.get("outputs"))
    shorts_profile = profiles.get("shorts", {})
    if not shorts_profile.get("enabled"):
        log("Shorts отключены — редакционная проверка не требуется.")
        return report_path

    shorts_candidates = report.get("shorts_candidates", [])
    if not shorts_candidates:
        log("Нет Shorts-кандидатов для редакционной проверки.")
        return report_path

    chosen_model = model or settings.get("editorial_model") or settings["llm_model"]
    settings.update(llm_model=chosen_model, llm_context_tokens=16384)
    model_key = hashlib.sha256(chosen_model.encode()).hexdigest()[:8]

    job_dir = report_path.parent.parent
    cache = job_dir / "analysis" / f"shorts_{report['signature']}_editorial_v{REVIEW_VERSION}_{model_key}"
    cache.mkdir(parents=True, exist_ok=True)

    min_sec = shorts_profile["min_duration_sec"]
    max_sec = shorts_profile["max_duration_sec"]
    duration_sec = transcript["duration_sec"]

    kept, rejected, warnings = [], [], []
    for index, candidate in enumerate(shorts_candidates):
        if candidate.get("status") != "pending":
            warnings.append(f"{candidate['candidate_id']}: уже имеет статус {candidate['status']}, пропускаю.")
            kept.append(candidate)
            continue

        log(f"Редакционная проверка Shorts: {index + 1}/{len(shorts_candidates)}")
        sections = review_sections(transcript, candidate["start"], candidate["end"], context_count=3)
        allowed = {s["id"] for s in sections["before"]} | {s["id"] for s in sections["after"]}
        for row in sections["rows"]:
            allowed.add(row["sentence"]["id"])

        def validate_shorts(value):
            _validate_shorts(value, allowed, min_sec, max_sec, duration_sec)

        path = cache / f"{candidate['candidate_id']}_decision.json"
        if path.exists():
            decision = load_json(path)
            validate_shorts(decision)
        else:
            before_text = sentence_json(sections["before"])
            after_text = sentence_json(sections["after"])
            inside_text = sentence_json([r["sentence"] for r in sections["rows"]])
            start_id = candidate.get("start_id", sections["rows"][0]["sentence"]["id"] if sections["rows"] else "")
            end_id = candidate.get("end_id", sections["rows"][-1]["sentence"]["id"] if sections["rows"] else "")

            instructions = (
                "Проверь Short (20–60 сек, 9:16) СКЕПТИЧЕСКИ. Это вертикальный ролик, "
                "который зритель увидит отдельно от полного стрима.\n"
                "КРИТЕРИИ:\n"
                "1. Сильное начало: первые 1–3 секунды цепляют, нет долгого вступления, "
                "нет 'привет', 'ребят', 'давайте обсудим'.\n"
                "2. Самодостаточность: понятен без предшествующего контекста стрима.\n"
                "3. Завершённость: есть вывод, развязка, или чёткая остановка. "
                "Не обрывается на полуслове или на анонсе следующей темы.\n"
                "4. Одна тема: внутри Short не происходит смены предмета разговора.\n"
                "5. Нет лишней паузы: в начале и в конце нет затянутого молчания.\n"
                "6. Не фрагмент: это не кусок большой истории, которая требует контекста.\n"
                "Если все 6 пунктов выполняются и duration в пределах "
                f"{min_sec}–{max_sec} сек — keep. "
                "Если можно исправить, двигая границы на соседние предложения — revise. "
                "Иначе reject.\n"
                "ID — непрозрачные идентификаторы, НЕ секунды.\n"
                f"Исходная заявка: start_id={start_id}, end_id={end_id}, "
                f"длительность: {candidate['end'] - candidate['start']:.2f} секунд.\n"
            )

            def build_prompt():
                return (instructions +
                        f"КОНТЕКСТ ДО (не входит в Short): {before_text}\n"
                        f"-----НАЧАЛО-SHORT-----\n"
                        f"{inside_text}\n"
                        f"-----КОНЕЦ-SHORT-----\n"
                        f"КОНТЕКСТ ПОСЛЕ (не входит в Short): {after_text}")

            prompt = build_prompt()
            while byte_size(prompt) > INITIAL_PROMPT_BYTES and (sections["before"] or sections["after"]):
                if sections["before"] and (not sections["after"] or
                                           byte_size(sentence_json(sections["before"])) >=
                                           byte_size(sentence_json(sections["after"]))):
                    sections["before"].pop(0)
                else:
                    sections["after"].pop()
                prompt = build_prompt()

            if byte_size(prompt) > INITIAL_PROMPT_BYTES:
                warning = (f"{candidate['candidate_id']}: полный текст Short не помещается в запрос. "
                           "Требуется ручное ревью.")
                warnings.append(warning)
                log(warning)
                candidate["editorial_decision"] = "needs_review"
                candidate["warnings"] = list(candidate.get("warnings", [])) + [warning]
                kept.append(candidate)
                continue

            try:
                decision = ask(settings, prompt, SHORTS_SCHEMA, validate_shorts,
                               audit_path=cache / f"{candidate['candidate_id']}_attempts.json")
            except ModelValidationError:
                warning = (f"{candidate['candidate_id']}: редактор трижды вернул недопустимое решение; "
                           "исходные границы сохранены для ручного ревью.")
                warnings.append(warning)
                log(warning)
                candidate["editorial_decision"] = "needs_review"
                candidate["warnings"] = list(candidate.get("warnings", [])) + [warning]
                kept.append(candidate)
                continue
            except Exception as exc:
                warning = (f"{candidate['candidate_id']}: ошибка при редакционной проверке: {exc}; "
                           "исходные границы сохранены для ручного ревью.")
                warnings.append(warning)
                log(warning)
                candidate["editorial_decision"] = "needs_review"
                candidate["warnings"] = list(candidate.get("warnings", [])) + [warning]
                kept.append(candidate)
                continue
            save_json(path, decision)

        if decision["decision"] == "reject":
            rejected.append({"candidate_id": candidate["candidate_id"], "reason": decision["reason"]})
            continue

        if decision["decision"] == "revise":
            new_start_id = decision["start_id"]
            new_end_id = decision["end_id"]
            if new_start_id != start_id or new_end_id != end_id:
                start_sent = next((s for s in transcript["sentences"] if s["id"] == new_start_id), None)
                end_sent = next((s for s in transcript["sentences"] if s["id"] == new_end_id), None)
                if start_sent and end_sent:
                    candidate["start_id"] = new_start_id
                    candidate["end_id"] = new_end_id
                    candidate["start"] = start_sent["start"]
                    candidate["end"] = end_sent["end"]
                    candidate["warnings"] = (list(candidate.get("warnings", [])) +
                                             ["Границы скорректированы редакцией. Требуется повторное утверждение."])
                    candidate.pop("approval_key", None)

        candidate["title"] = decision["title"]
        candidate["reason"] = decision["reason"]
        candidate["score"] = decision["score"]
        candidate["editorial_decision"] = decision["decision"]
        candidate["editorial_flags"] = {k: decision[k] for k in
                                        ("strong_opening", "self_contained", "complete_ending",
                                         "single_topic", "no_trailing_pause", "not_a_fragment")}
        candidate.pop("approval_key", None)
        kept.append(candidate)

    ranked = []
    for candidate in sorted(kept, key=lambda c: c["score"], reverse=True):
        if not any(interval_iou(candidate, other) >= 0.30 for other in ranked):
            ranked.append(candidate)
        else:
            rejected.append({"candidate_id": candidate["candidate_id"],
                             "reason": "Пересечение с более сильным Shorts-кандидатом после редакционной правки."})

    report["shorts_candidates"] = ranked
    report["shorts_editorial_version"] = REVIEW_VERSION
    report["shorts_editorial_warnings"] = warnings
    report["shorts_editorial_rejections"] = rejected
    report["shorts_editorial_model"] = chosen_model

    save_json(report_path, report)
    log(f"После редакционной проверки Shorts: {len(ranked)} кандидатов, отклонено {len(rejected)}.")
    return report_path
