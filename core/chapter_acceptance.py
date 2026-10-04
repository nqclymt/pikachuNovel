"""Evidence-checked chapter acceptance; no rewriting or silent acceptance on errors."""

import hashlib
import json

from core.prompt_loader import PromptLoader


CONTEXT_CATEGORIES = {"hidden_information", "character_knowledge", "ending_state"}
PLANNING_CATEGORIES = {"fact_conflict", "missing_required_event", "premature_reveal", "literary_preference"}
MAX_PROMPT_CHARS = 120000


def _invalid(message):
    raise RuntimeError(f"章节内容验收结果无效：{message}。未判定为通过，请检查后重新验收。")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid(f"JSON 对象重复定义了 {key}，不能判断哪一项有效")
        result[key] = value
    return result


def _json_constant(value):
    _invalid(f"JSON 包含不支持的数值 {value}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{label} 必须是非空文本")
    return value.strip()


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _identifier(prefix, value):
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _required_events(scene_plan):
    if not isinstance(scene_plan, list):
        raise RuntimeError("章节内容验收需要场景计划列表，未调用验收模型。")
    events = {}
    for number, scene in enumerate(scene_plan, 1):
        if not isinstance(scene, dict):
            raise RuntimeError("章节内容验收的场景计划包含无效条目。")
        required = scene.get("required_events", [])
        if not isinstance(required, list):
            raise RuntimeError("章节内容验收的 required_events 必须是列表。")
        for event in required:
            if not isinstance(event, str) or not event.strip():
                raise RuntimeError("章节内容验收的必写事件必须是非空文本。")
            event = event.strip()
            event_id = _identifier("event_", event)
            item = events.setdefault(event_id, {"event_id": event_id, "text": event, "scenes": []})
            if number not in item["scenes"]:
                item["scenes"].append(number)
    return list(events.values())


def _planning_issues(issues):
    if issues is None:
        return []
    if not isinstance(issues, list):
        raise RuntimeError("章节内容验收的审读问题必须是列表。")
    normalized = {}
    for issue in issues:
        if not isinstance(issue, (dict, str)) or not issue or (isinstance(issue, str) and not issue.strip()):
            raise RuntimeError("章节内容验收的审读问题条目无效。")
        serialized = json.dumps(issue, ensure_ascii=False, sort_keys=True)
        issue_id = _identifier("planning_", serialized)
        normalized.setdefault(issue_id, {"issue_id": issue_id, "issue": issue})
    return list(normalized.values())


def _covered_checks(payload, field, key, expected):
    values = payload.get(field)
    if not isinstance(values, list):
        _invalid(f"{field} 必须是列表")
    result = {}
    for row in values:
        if not isinstance(row, dict):
            _invalid(f"{field} 包含非对象条目")
        identifier = row.get(key)
        if not isinstance(identifier, str) or identifier not in expected:
            _invalid(f"{field} 引用了未知的 {key}")
        if identifier in result:
            _invalid(f"{field} 重复检查了 {identifier}")
        result[identifier] = row
    if set(result) != set(expected):
        _invalid(f"{field} 未覆盖全部待检查项")
    return list(result.values())


def _status(row, values, label):
    value = row.get("status")
    if not isinstance(value, str) or value not in values:
        _invalid(f"{label}.status 不属于允许的枚举")
    return value


def _evidence(row, chapter, label, allow_empty=False):
    value = row.get("evidence")
    if not isinstance(value, str):
        _invalid(f"{label}.evidence 必须是文本")
    value = value.strip()
    if allow_empty and not value:
        return ""
    if len(value) < 4 or len(value) > 1200 or value not in chapter:
        _invalid(f"{label} 的正文证据不是本章逐字片段（4—1200字符）")
    return value


def _constraint(row, sources, label, required):
    value = row.get("constraint_quote", "")
    if not isinstance(value, str):
        _invalid(f"{label}.constraint_quote 必须是文本")
    value = value.strip()
    if not value and not required:
        return ""
    if len(value) < 4 or len(value) > 1200 or not any(value in source for source in sources):
        _invalid(f"{label} 缺少来自章纲、场景计划或记忆的真实约束证据")
    return value


def assess_chapter(generate, chapter_text, chapter_outline, scene_plan,
                   writing_requirements="", memory_context="", planning_issues=None):
    """Call the supplied cancellable generator once, then validate every audit claim.

    Only explicit missing events or evidence-backed factual/information violations
    block publication. Uncertainty and literary preferences are warnings. Caller
    retains the candidate and decides how to continue; this function never writes it.
    """
    if not isinstance(chapter_text, str) or not chapter_text.strip():
        raise RuntimeError("章节正文为空，无法进行内容验收。")
    if not isinstance(chapter_outline, str) or not isinstance(memory_context, str):
        raise RuntimeError("章节内容验收的章纲与记忆上下文必须是文本。")
    events = _required_events(scene_plan)
    pending = _planning_issues(planning_issues)
    sources = [chapter_outline, memory_context, *_strings(scene_plan)]
    prompt = PromptLoader.load(
        "chapter_acceptance", chapter_text=chapter_text, chapter_outline=chapter_outline,
        scene_plan=json.dumps(scene_plan, ensure_ascii=False),
        required_events=json.dumps(events, ensure_ascii=False),
        planning_issues=json.dumps(pending, ensure_ascii=False),
        writing_requirements=str(writing_requirements or ""), memory_context=memory_context,
    )
    if len(prompt) > MAX_PROMPT_CHARS:
        raise RuntimeError("章节内容验收上下文超过120000字符，未截断剧情事实；请缩小明确相关的记忆范围后再验收。")
    raw = generate(prompt)
    if not isinstance(raw, str) or len(raw) > 500000:
        _invalid("模型输出不是文本或超过大小限制")
    try:
        payload = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_json_constant)
    except (ValueError, TypeError, RecursionError):
        _invalid("模型没有返回合法 JSON 对象")
    if not isinstance(payload, dict):
        _invalid("顶层必须是 JSON 对象")
    event_map = {item["event_id"]: item for item in events}
    event_checks = _covered_checks(payload, "event_checks", "event_id", event_map)
    context_checks = _covered_checks(payload, "context_checks", "category", CONTEXT_CATEGORIES)
    planning_checks = _covered_checks(payload, "planning_issue_checks", "issue_id",
                                      {item["issue_id"] for item in pending})
    blocking, warnings = [], []
    event_status = {}

    for row in event_checks:
        label = f"event_checks[{row['event_id']}]"
        status = _status(row, {"present", "missing", "uncertain"}, label)
        evidence = _evidence(row, chapter_text, label, allow_empty=status == "missing")
        reason = _text(row.get("reason"), f"{label}.reason")
        event_status[row["event_id"]] = status
        if status != "present":
            item = {"category": "missing_required_event" if status == "missing" else "uncertain",
                    "event_id": row["event_id"], "event": event_map[row["event_id"]]["text"],
                    "evidence": evidence, "reason": reason}
            (blocking if status == "missing" else warnings).append(item)

    for row in context_checks:
        label = f"context_checks[{row['category']}]"
        status = _status(row, {"clear", "violation", "uncertain", "not_applicable"}, label)
        evidence = _evidence(row, chapter_text, label)
        constraint = _constraint(row, sources, label, required=status in {"clear", "violation"})
        reason = _text(row.get("reason"), f"{label}.reason")
        if status in {"violation", "uncertain"}:
            category = "premature_reveal" if row["category"] == "hidden_information" else "fact_conflict"
            item = {"category": category if status == "violation" else "uncertain",
                    "dimension": row["category"], "evidence": evidence,
                    "constraint_quote": constraint, "reason": reason}
            (blocking if status == "violation" else warnings).append(item)

    for row in planning_checks:
        label = f"planning_issue_checks[{row['issue_id']}]"
        status = _status(row, {"resolved", "unresolved", "uncertain"}, label)
        category = row.get("category")
        if not isinstance(category, str) or category not in PLANNING_CATEGORIES:
            _invalid(f"{label}.category 不属于允许的枚举")
        missing = status == "unresolved" and category == "missing_required_event"
        event_id = row.get("event_id", "")
        if not isinstance(event_id, str) or (event_id and event_id not in event_map):
            _invalid(f"{label} 引用了未知必写事件")
        if missing and (not event_id or event_status.get(event_id) != "missing"):
            _invalid(f"{label} 的缺失事件必须同时在 event_checks 中明确判为 missing")
        evidence = _evidence(row, chapter_text, label, allow_empty=missing)
        reason = _text(row.get("reason"), f"{label}.reason")
        constraint = _constraint(row, sources, label, required=(
            status == "unresolved" and category in {"fact_conflict", "premature_reveal"}
        ))
        if status != "resolved":
            item = {"category": "uncertain" if status == "uncertain" else category,
                    "issue_id": row["issue_id"], "event_id": event_id,
                    "evidence": evidence, "constraint_quote": constraint, "reason": reason}
            (blocking if status == "unresolved" and category != "literary_preference" else warnings).append(item)

    style_warnings = payload.get("style_warnings", [])
    if not isinstance(style_warnings, list) or len(style_warnings) > 12:
        _invalid("style_warnings 必须是最多12条的列表")
    for row in style_warnings:
        if not isinstance(row, dict):
            _invalid("style_warnings 包含无效条目")
        warnings.append({"category": "literary_preference",
                         "evidence": _evidence(row, chapter_text, "style_warnings"),
                         "reason": _text(row.get("reason"), "style_warnings.reason")})

    return {"version": 1, "status": "needs_review" if blocking else "passed",
            "chapter_sha256": hashlib.sha256(chapter_text.encode("utf-8")).hexdigest(),
            "required_events": events, "event_checks": event_checks,
            "context_checks": context_checks, "planning_issue_checks": planning_checks,
            "blocking_issues": blocking, "warnings": warnings}
