"""Durable chapter stages. A checkpoint is reusable only for identical inputs."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


REVISION = 1
STAGES = ("planned", "drafted", "edited", "audited", "validated", "published")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode("utf-8")).hexdigest()


def checkpoint_path(ws, volume, chapter):
    return Path(ws.file_system) / "drafts" / f"vol_{volume:02d}" / "checkpoints" / f"chapter_{chapter:03d}.json"


def load_checkpoint(ws, volume, chapter):
    try:
        data = json.loads(checkpoint_path(ws, volume, chapter).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and data.get("revision") == REVISION
                and data.get("stage") in STAGES):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_checkpoint(ws, volume, chapter, state):
    path = checkpoint_path(ws, volume, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**state, "revision": REVISION, "volume": volume, "chapter": chapter}
    descriptor, temporary = tempfile.mkstemp(prefix=".chapter-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def has_stage(state, stage):
    return state.get("stage") in STAGES and STAGES.index(state["stage"]) >= STAGES.index(stage)


def chapter_format_report(text, chapter, max_chars=2700):
    lines = text.strip().splitlines()
    errors, warnings = [], []
    if not lines or not re.fullmatch(rf"第\s*{chapter}\s*章\s+\S.*", lines[0].strip()):
        errors.append(f"首行须为“第{chapter}章 标题”，且章号与当前任务一致。")
    body = "\n".join(lines[1:]).strip()
    count = len(re.sub(r"\s", "", body))
    if not body:
        errors.append("正文为空。")
    if "```" in text:
        errors.append("正文中含代码围栏，请检查是否误输出了格式说明。")
    if count > max_chars:
        warnings.append(f"正文共{count}字符，超过目标上限{max_chars}；保留原稿，不自动裁剪。")
    return {"errors": errors, "warnings": warnings, "characters": count}


def guarded_knowledge_repairs(text, audit):
    """All spans refer to the original text, never to an earlier replacement."""
    accepted, rejected, spans = [], [], []
    budget = max(80, int(len(text) * 0.2))
    paragraphs = text.splitlines()
    for correction in audit.get("corrections") or []:
        if not isinstance(correction, dict):
            continue
        original = str(correction.get("original") or "")
        replacement = str(correction.get("replacement") or "")
        fact_id = str(correction.get("fact_id") or "")
        facts = audit.get("fact_catalog") or {}
        reason = ""
        if not original or not replacement or original == replacement:
            reason = "empty_or_unchanged"
        elif not fact_id or fact_id not in facts:
            reason = "unknown_fact"
        elif text.count(original) != 1:
            reason = "ambiguous_or_missing_anchor"
        elif original in (paragraphs[0] if paragraphs else ""):
            reason = "title_is_not_editable"
        elif "\n" in original or "\n" in replacement:
            reason = "cross_paragraph_edit"
        elif len(original) > budget or len(replacement) > max(80, len(original) * 2):
            reason = "edit_too_large"
        else:
            start = text.index(original)
            end = start + len(original)
            if any(start < right and end > left for left, right, _ in spans):
                reason = "overlapping_edit"
            elif sum(len(item["original"]) for item in accepted) + len(original) > budget:
                reason = "total_edit_budget"
        if reason:
            rejected.append({"reason": reason, "original": original, "fact_id": fact_id})
            continue
        spans.append((start, end, replacement))
        accepted.append(correction)
    result = text
    for start, end, replacement in sorted(spans, reverse=True):
        result = result[:start] + replacement + result[end:]
    audit["rejected_corrections"] = rejected
    return result, accepted


def require_previous_draft(ws, volume, chapter):
    """Known preceding outlines are dependencies, even if their draft is missing."""
    candidates = []
    for path in (Path(ws.file_system) / "chapter_outlines").glob("vol_*/chapter_*.md"):
        vm = re.fullmatch(r"vol_(\d+)", path.parent.name)
        cm = re.fullmatch(r"chapter_(\d+)\.md", path.name)
        if vm and cm:
            key = (int(vm[1]), int(cm[1]))
            if key < (volume, chapter):
                candidates.append(key)
    if not candidates:
        return
    prev_volume, prev_chapter = max(candidates)
    draft = Path(ws.file_system) / "chapters" / f"vol_{prev_volume:02d}" / f"{prev_chapter:03d}_第{prev_chapter}章.md"
    if not draft.is_file() or not draft.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"第{chapter}章依赖第{prev_chapter}章正文；请先完成前章，再继续本任务。")
