"""Evidence-based, bounded prose edits with explicit semantic acceptance checks."""

import hashlib
import json
import math
import re

from core.llm_provider import LLMCallCancelled, LLMExecutionBlocked
from core.prompt_loader import PromptLoader
from core.text_utils import normalize_text, parse_json_response


MAX_PROMPT_CHARS = 60000
ISSUE_CATEGORIES = {
    "outline_leakage", "redundant_explanation", "generic_dialogue",
    "mechanical_rhythm", "repeated_beat",
}
_QUANTITIES = re.compile(
    r"\d+(?:\.\d+)?%?|[零〇一二两三四五六七八九十百千万亿]+(?=[年月日天时刻息丈尺寸里岁成重层阶级颗枚斤])"
)


def _check_cancel(event):
    if event is not None and event.is_set():
        raise LLMCallCancelled("正文编辑审读已取消")


def _call_json(llm, prompt, cancel_event):
    _check_cancel(cancel_event)
    if cancel_event is not None and hasattr(llm, "generate_cancelable"):
        raw = llm.generate_cancelable(prompt, cancel_event, temperature=0.2, is_json=True)
    else:
        raw = llm.generate(prompt, temperature=0.2, is_json=True)
    _check_cancel(cancel_event)
    payload = parse_json_response(raw or "")
    if not isinstance(payload, dict):
        raise ValueError("expected_json_object")
    return payload


def _paragraph_records(text):
    # Both single-newline and blank-line paragraphs occur in imported/generated prose.
    # Keep exact spans so an accepted edit never reformats its neighbours or the title.
    records = []
    for match in re.finditer(r"[^\r\n]+", text):
        raw = match.group()
        body = raw.strip()
        if not body:
            continue
        start = match.start() + len(raw) - len(raw.lstrip())
        records.append({"index": len(records), "text": body, "start": start,
                        "end": start + len(body), "editable": True})
    if records and re.match(r"^(?:#{1,6}\s*)?第.{1,30}?[章回节](?:\s|$|[：:])", records[0]["text"]):
        records[0]["editable"] = False
    return records


def _render_edits(text, records, replacements):
    result = text
    for record in reversed(records):
        if record["index"] in replacements:
            result = result[:record["start"]] + replacements[record["index"]] + result[record["end"]:]
    return result


def edit_chapter_prose(
    llm, chapter_text, *, chapter_outline="", story_arc="", recent_context="",
    writing_guide="", style_anchor="", strength="standard", cancel_event=None,
):
    """Review -> propose local edits -> verify. A failed stage preserves its input.

    The report distinguishes no issues from failed/skipped work. Model validation is
    a conservative acceptance gate, not a guarantee of literary or factual quality.
    """
    _check_cancel(cancel_event)
    strength = strength if strength in {"light", "standard", "deep"} else "standard"
    records = _paragraph_records(chapter_text)
    by_index = {record["index"]: record for record in records}
    editable = [record for record in records if record["editable"]]
    max_edits, ratio = {"light": (3, 0.2), "standard": (6, 0.3), "deep": (10, 0.45)}[strength]
    limit = min(max_edits, max(1, math.ceil(len(editable) * ratio)))
    report = {
        "version": 1, "strength": strength, "status": "unchanged", "stage": "review",
        "model": str(getattr(llm, "model", "")), "max_edits": limit,
        "input_sha256": hashlib.sha256(chapter_text.encode("utf-8")).hexdigest(),
        "issues": [], "planning_issues": [], "rejected": [], "applied_edits": [],
        "prompt_chars": {},
    }

    def finish(text, status, reason=""):
        _check_cancel(cancel_event)
        report.update(status=status, reason=reason,
                      output_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        return text, report

    def request(folder, **variables):
        # Template errors are programming errors; do not report them as a successful no-op.
        prompt = PromptLoader.load(folder, **variables)
        report["stage"] = folder
        report["prompt_chars"][folder] = len(prompt)
        if len(prompt) > MAX_PROMPT_CHARS:
            report["failure"] = "context_limit"
            return None
        try:
            return _call_json(llm, prompt, cancel_event)
        except (LLMCallCancelled, LLMExecutionBlocked):
            raise
        except Exception as exc:
            # Do not persist provider exception bodies, which can contain credentials.
            report["failure"] = type(exc).__name__
            return None

    if not editable:
        return finish(chapter_text, "skipped", "no_editable_paragraphs")
    context = (
        f"【当前章纲：约束事件与信息边界，不是正文范本】\n{chapter_outline or '（未提供）'}\n\n"
        f"【故事情节单元：不得提前写入后续事件】\n{story_arc or '（未提供）'}\n\n"
        f"【最近正文末尾节选：仅用于检查承接和重复】\n{recent_context[-4000:] or '（未提供）'}\n\n"
        f"【用户写作要求】\n{writing_guide or '（未提供）'}\n\n"
        f"【人工文风依据：保留正常表达与不均匀节奏】\n{style_anchor or '（未提供）'}"
    )
    public_records = [{key: row[key] for key in ("index", "text", "editable")} for row in records]
    review = request("prose_review", context=context,
                     paragraph_records=json.dumps(public_records, ensure_ascii=False))
    if review is None:
        return finish(chapter_text, "review_failed", report["failure"])
    if not isinstance(review.get("issues"), list):
        return finish(chapter_text, "review_failed", "invalid_issues_schema")

    local = {}
    for issue in review["issues"][:40]:
        if not isinstance(issue, dict):
            report["rejected"].append({"reason": "invalid_issue"})
            continue
        index = issue.get("index")
        row = by_index.get(index) if type(index) is int else None
        quote, reason = issue.get("quote"), issue.get("reason")
        scope, category = issue.get("scope"), issue.get("category")
        if (not row or not row["editable"] or not isinstance(quote, str)
                or len(quote.strip()) < 4 or quote not in row["text"]
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(category, str) or category not in ISSUE_CATEGORIES
                or not isinstance(scope, str) or scope not in {"local", "planning"}):
            report["rejected"].append({"index": index if type(index) is int else None,
                                       "reason": "unanchored_or_invalid_issue"})
            continue
        item = {"index": index, "quote": quote, "reason": reason[:1200],
                "category": category, "scope": scope}
        if scope == "planning":
            report["planning_issues"].append(item)
            continue
        instruction = issue.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            report["rejected"].append({"index": index, "reason": "missing_edit_instruction"})
            continue
        item["instruction"] = instruction[:1200]
        report["issues"].append(item)
        local.setdefault(index, item)

    # A paragraph with a planning-level problem is never rewritten to fix that problem indirectly.
    for issue in report["planning_issues"]:
        local.pop(issue["index"], None)
    selected = list(local.values())[:limit]
    if not selected:
        status = "no_changes" if not review["issues"] else "unchanged"
        return finish(chapter_text, status, "no_actionable_local_issues")
    targets = [{**item, "original": by_index[item["index"]]["text"]} for item in selected]
    proposals = request("prose_local_edit", context=context, chapter_text=chapter_text,
                        targets=json.dumps(targets, ensure_ascii=False))
    if proposals is None:
        return finish(chapter_text, "edit_failed", report["failure"])
    if not isinstance(proposals.get("replacements"), list):
        return finish(chapter_text, "edit_failed", "invalid_replacements_schema")

    candidates, seen, duplicates = {}, set(), set()
    allowed = {item["index"] for item in selected}
    for item in proposals["replacements"]:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if type(index) is not int or index not in allowed:
            continue
        if index in seen:
            duplicates.add(index)
            continue
        seen.add(index)
        original = by_index[index]["text"]
        value = item.get("text")
        if item.get("original") != original or not isinstance(value, str):
            report["rejected"].append({"index": index, "reason": "original_mismatch"})
            continue
        replacement = normalize_text(value).strip()
        if replacement == original:
            continue
        if (not replacement or "\n" in replacement or "\r" in replacement
                or not max(2, len(original) * 0.25) <= len(replacement) <= max(40, len(original) * 1.65)):
            report["rejected"].append({"index": index, "reason": "invalid_edit_size_or_paragraphs"})
            continue
        if _QUANTITIES.findall(original) != _QUANTITIES.findall(replacement):
            report["rejected"].append({"index": index, "reason": "quantity_changed"})
            continue
        candidates[index] = replacement
    for index in duplicates:
        candidates.pop(index, None)
        report["rejected"].append({"index": index, "reason": "duplicate_replacement"})
    if not candidates:
        return finish(chapter_text, "unchanged", "no_valid_proposals")

    proposed_text = _render_edits(chapter_text, records, candidates)
    verification_targets = [
        {"index": index, "original": by_index[index]["text"], "replacement": text,
         "issue": local[index]} for index, text in candidates.items()
    ]
    verification = request("prose_edit_verify", context=context, chapter_text=chapter_text,
                           proposed_chapter=proposed_text,
                           targets=json.dumps(verification_targets, ensure_ascii=False))
    if verification is None:
        return finish(chapter_text, "verification_failed", report["failure"])
    if not isinstance(verification.get("checks"), list):
        return finish(chapter_text, "verification_failed", "invalid_checks_schema")
    checks, duplicates = {}, set()
    for check in verification["checks"]:
        if not isinstance(check, dict) or type(check.get("index")) is not int:
            continue
        index = check["index"]
        if index in checks:
            duplicates.add(index)
        checks[index] = check
    accepted = {}
    for index, replacement in candidates.items():
        check = checks.get(index, {})
        explanation = check.get("reason")
        if (index not in duplicates and isinstance(explanation, str) and explanation.strip()
                and all(check.get(key) is True for key in
                        ("facts_preserved", "issue_resolved", "no_new_problems"))):
            accepted[index] = replacement
            report["applied_edits"].append({
                "index": index, "original": by_index[index]["text"], "replacement": replacement,
                "issue": local[index], "verification_reason": explanation[:1200],
            })
        else:
            report["rejected"].append({"index": index, "reason": "verification_not_passed",
                                       "detail": explanation[:1200] if isinstance(explanation, str) else ""})
    return finish(_render_edits(chapter_text, records, accepted), "edited" if accepted else "unchanged")
