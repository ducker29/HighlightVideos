"""Byte-budget planning for complete prompts, including bounded repair feedback."""
import json

MAX_PROMPT_BYTES = 27000
REPAIR_RESERVE_BYTES = 5000
INITIAL_PROMPT_BYTES = MAX_PROMPT_BYTES - REPAIR_RESERVE_BYTES
CONTEXT_POLICY_VERSION = 1


class ContextBudgetExceeded(ValueError):
    pass


def byte_size(text):
    return len(text.encode("utf-8"))


def excerpt(text, budget):
    """Only for summaries/repair diagnostics, never for source transcript content."""
    if byte_size(text) <= budget:
        return text
    if budget < 3:
        return "." * max(0, budget)
    return text.encode("utf-8")[:budget - 3].decode("utf-8", errors="ignore") + "..."


def sentence_json(sentences):
    return json.dumps([{key: sentence[key] for key in ("id", "start", "end", "text")}
                       for sentence in sentences], ensure_ascii=False, separators=(",", ":"))


def repair_feedback(prompt, reply, error, fallback):
    available = MAX_PROMPT_BYTES - byte_size(prompt)
    if available <= 0:
        return ""
    # Keep the actual invalid IDs/decision, rather than repeating long generated prose.
    try:
        value = json.loads(reply)
        if isinstance(value, dict) and isinstance(value.get("clips"), list):
            value = {"clips": [{k: c[k] for k in ("start_id", "end_id", "score") if k in c}
                               if isinstance(c, dict) else c for c in value["clips"]]}
        elif isinstance(value, dict) and "interval_id" in value:
            value = {k: value[k] for k in ("interval_id", "score") if k in value}
        elif isinstance(value, dict) and "decision" in value:
            value = {k: value[k] for k in ("decision", "start_id", "end_id", "score") if k in value}
        reply = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        pass
    error_budget = min(3000, available // 2)
    reply_budget = min(1500, available // 3)
    feedback = (f"\nПредыдущий ответ отклонён: {excerpt(str(error), error_budget)}\n"
                f"Предыдущий JSON: {excerpt(reply, reply_budget)}\n"
                "Исправь этот ответ, используя разрешённые ID. " + fallback)
    return excerpt(feedback, available)


def fit_sentence_groups(sentences, build_prompt, overlap_seconds=30, budget=INITIAL_PROMPT_BYTES):
    """Greedy complete-sentence groups; count the final prompt, not just its transcript."""
    groups = []
    first = 0
    while first < len(sentences):
        last = first
        while last < len(sentences) and byte_size(build_prompt(sentences[first:last + 1])) <= budget:
            last += 1
        if last == first:
            raise ContextBudgetExceeded(f"Одно предложение {sentences[first]['id']} вместе с инструкциями не помещается в бюджет {budget} байт.")
        groups.append(sentences[first:last])
        if last == len(sentences):
            break
        next_first = first + 1
        while next_first < last and sentences[next_first]["start"] < sentences[last - 1]["end"] - overlap_seconds:
            next_first += 1
        # If the whole group is shorter than the desired overlap, advance by half.
        if sentences[first]["start"] >= sentences[last - 1]["end"] - overlap_seconds:
            next_first = first + max(1, (last - first) // 2)
        first = next_first
    return groups


def fit_option_batches(options, build_prompt, budget=INITIAL_PROMPT_BYTES):
    """Non-overlapping exhaustive batches; explicitly return indivisible oversized options."""
    batches, oversized, current = [], [], []
    for option in options:
        if byte_size(build_prompt(current + [option])) <= budget:
            current.append(option)
            continue
        if current:
            batches.append(current)
            current = []
        if byte_size(build_prompt([option])) <= budget:
            current = [option]
        else:
            oversized.append(option)
    if current:
        batches.append(current)
    return batches, oversized
