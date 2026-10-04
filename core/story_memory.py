"""Evidence-backed continuity from published prose, separate from planned outlines."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

from core.prompt_loader import PromptLoader


SCHEMA_VERSION = 1
MAX_CONTEXT_CHARS = 14000
MAX_CHAPTER_CHARS = 50000
KINDS = ("knowledge", "relationship", "inventory", "injury", "promise", "foreshadowing", "state")
STATUSES = {"active", "resolved", "inactive", "unknown"}
KIND_NAMES = {
    "knowledge": "人物所知", "relationship": "人物关系", "inventory": "物品归属",
    "injury": "伤势", "promise": "承诺", "foreshadowing": "伏笔", "state": "其他已发生状态",
}


class StoryMemoryError(RuntimeError):
    """Continuity extraction was unavailable or not supported by source evidence."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entries(ws, folder: str, target: tuple[int, int]):
    base = Path(ws.file_system) / folder
    if not base.is_dir():
        return []
    entries = []
    root = base.resolve()
    pattern = {"chapters": r"(\d+)_第(\d+)章\.md",
               "chapter_outlines": r"chapter_(\d+)\.md",
               "system_panels": r"chapter_(\d+)\.json"}[folder]
    for volume_dir in base.iterdir():
        match = re.fullmatch(r"vol_(\d+)", volume_dir.name)
        if not match or not volume_dir.is_dir():
            continue
        volume = int(match.group(1))
        if volume > target[0]:
            continue
        for path in volume_dir.iterdir():
            match = re.fullmatch(pattern, path.name)
            if not match or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            chapter = int(match.group(1))
            if folder == "chapters" and chapter != int(match.group(2)):
                continue
            if volume > 0 and chapter > 0 and (volume, chapter) < target and path.stat().st_size:
                entries.append(((volume, chapter), path))
    entries.sort(key=lambda item: (item[0], item[1].name))
    seen = set()
    for identity, _ in entries:
        if identity in seen:
            raise StoryMemoryError(f"第{identity[0]}卷第{identity[1]}章有重复章节文件，无法确定连续性顺序。")
        seen.add(identity)
    return entries


def previous_chapter_paths(ws, volume: int, chapter_num: int, limit: int = 2) -> list[Path]:
    """Return chronological published predecessors across volumes; never current/future prose."""
    if limit <= 0:
        return []
    entries = _entries(ws, "chapters", (int(volume), int(chapter_num)))
    return [path for _, path in entries[-limit:]]


def previous_panel_path(ws, volume: int, chapter_num: int) -> Path | None:
    """Require the actual predecessor's panel; never silently revive older/initial state."""
    target = (int(volume), int(chapter_num))
    panels = _entries(ws, "system_panels", target)
    predecessors = panels + _entries(ws, "chapters", target) + _entries(ws, "chapter_outlines", target)
    if not predecessors:
        return None
    expected = max(key for key, _ in predecessors)
    path = next((path for key, path in panels if key == expected), None)
    if path is None:
        raise StoryMemoryError(f"缺少第{expected[0]}卷第{expected[1]}章的系统面板；请补全前章面板后继续，未回退到初始或更旧状态。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not any(isinstance(payload.get(key), dict) for key in ("panel", "protagonist_state")):
            raise ValueError("missing panel")
    except (OSError, ValueError) as exc:
        raise StoryMemoryError(f"前章系统面板无法读取或格式无效：{path}；未回退到初始状态。") from exc
    return path


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise StoryMemoryError(f"记忆提取字段 {label} 缺失、为空或超过 {limit} 字符。")
    return value.strip()


def _validate_delta(payload, source: str):
    if not isinstance(payload, dict):
        raise StoryMemoryError("记忆提取必须返回 JSON 对象。")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise StoryMemoryError("记忆提取缺少带证据的 summary 对象。")
    summary_text = _text(summary.get("text"), "summary.text", 600)
    evidence = summary.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise StoryMemoryError("章节摘要必须提供 1—8 段原文证据。")
    quotes = [_text(item, "summary.evidence", 700) for item in evidence]
    if any(quote not in source for quote in quotes):
        raise StoryMemoryError("章节摘要的证据不在本章已发布正文中。")
    updates = payload.get("updates")
    if not isinstance(updates, list) or len(updates) > 80:
        raise StoryMemoryError("记忆 updates 必须是最多 80 条的数组。")
    normalized, seen = [], set()
    for item in updates:
        if not isinstance(item, dict) or item.get("kind") not in KINDS or item.get("status") not in STATUSES:
            raise StoryMemoryError("记忆更新的 kind 或 status 无效。")
        update = {
            "kind": item["kind"], "entity": _text(item.get("entity"), "entity", 80),
            "key": _text(item.get("key"), "key", 100), "value": _text(item.get("value"), "value", 450),
            "status": item["status"], "evidence": _text(item.get("evidence"), "evidence", 700),
        }
        if update["evidence"] not in source:
            raise StoryMemoryError(f"记忆更新“{update['entity']} / {update['key']}”的证据不在源正文中。")
        identity = (update["kind"], update["entity"], update["key"])
        if identity in seen:
            raise StoryMemoryError("同一章对同一记忆键返回了多条互相覆盖的更新。")
        seen.add(identity)
        normalized.append(update)
    return {"summary": {"text": summary_text, "evidence": quotes}, "updates": normalized}


def _parse_response(raw, source):
    if not isinstance(raw, str) or not raw.strip():
        raise StoryMemoryError("记忆提取模型未返回结果，尚未生成后续正文。")
    raw = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    if fenced:
        raw = fenced.group(1)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StoryMemoryError("记忆提取返回了无效 JSON，尚未生成后续正文。") from exc
    return _validate_delta(payload, source)


def _atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _render_context(state, summaries, max_chars=MAX_CONTEXT_CHARS):
    """Budget whole entries; prioritize unresolved promises/clues and latest state per category."""
    if not summaries:
        return ""
    parts = ["【已发布正文形成的长期记忆】\n以下是已发生事实，不是未来章纲；证据均来自标注章节。"]
    used = len(parts[0])
    for summary in summaries[-4:]:
        line = f"\n第{summary['volume']}卷第{summary['chapter']}章摘要：{summary['text']}"
        if used + len(line) < min(3000, max_chars // 3):
            parts.append(line)
            used += len(line)
    groups = {}
    for kind in KINDS:
        items = [item for item in state.values() if item["kind"] == kind]
        groups[kind] = sorted(items, key=lambda item: (
            item["status"] in {"active", "unknown"}, item["volume"], item["chapter"],
        ), reverse=True)
    ordered_kinds = ("promise", "foreshadowing", "knowledge", "relationship", "inventory", "injury", "state")
    omitted = 0
    # Round-robin avoids a large inventory excluding every relationship or injury.
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for kind in ordered_kinds:
            if index >= len(groups[kind]):
                continue
            item = groups[kind][index]
            line = (
                f"\n- [{KIND_NAMES[kind]}/{item['status']}] {item['entity']} / {item['key']}：{item['value']}"
                f"（第{item['volume']}卷第{item['chapter']}章；原文：{item['evidence']}）"
            )
            if used + len(line) > max_chars - 160:
                omitted += 1
                continue
            parts.append(line)
            used += len(line)
    if omitted:
        parts.append(f"\n[长度限制：还有 {omitted} 条已缓存记忆未展示；未展示不代表事实被取消，不可据此断言人物不知道或物品不存在。]")
    return "".join(parts)


def load_story_memory_context(ws, volume: int, chapter_num: int, generate: Callable[[str], str]) -> str:
    """Extract/cache prior prose deltas and reconstruct state at the requested chapter boundary.

    ``generate`` owns cancellation, quotas and transport. Its exceptions propagate unchanged.
    Changed/deleted predecessors change the dependency chain, invalidating all later cached deltas.
    Only final ``chapters`` files are sources: outlines, raw drafts and future chapters are excluded.
    """
    entries = _entries(ws, "chapters", (int(volume), int(chapter_num)))
    state, summaries = {}, []
    dependency = _digest(f"story-memory-schema-{SCHEMA_VERSION}")
    for (source_volume, source_chapter), path in entries:
        source = path.read_text(encoding="utf-8").strip()
        if not source:
            continue
        if len(source) > MAX_CHAPTER_CHARS:
            raise StoryMemoryError(f"第{source_volume}卷第{source_chapter}章超过记忆提取长度上限，未截断正文或继续生成。")
        content_hash = _digest(source)
        cache_path = (Path(ws.file_system) / "story_memory" / f"vol_{source_volume:02d}"
                      / f"chapter_{source_chapter:03d}.json")
        identity = f"vol_{source_volume:02d}/chapter_{source_chapter:03d}"
        metadata = {"version": SCHEMA_VERSION, "source": identity,
                    "content_hash": content_hash, "dependency_hash": dependency}
        delta = None
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cache, dict) and all(cache.get(key) == value for key, value in metadata.items()):
                delta = _validate_delta(cache.get("delta"), source)
        except (OSError, ValueError, StoryMemoryError):
            pass  # Invalid caches are reconstructed from source, never accepted as memory.
        if delta is None:
            prompt = PromptLoader.load(
                "story_memory_extract", volume=source_volume, chapter_num=source_chapter,
                previous_memory=_render_context(state, summaries, max_chars=10000) or "（无前序已发布正文）",
                chapter_text=source,
            )
            delta = _parse_response(generate(prompt), source)
            if _digest(path.read_text(encoding="utf-8").strip()) != content_hash:
                raise StoryMemoryError("记忆提取期间源正文发生修改；请重新执行以读取最新内容。")
            _atomic_json(cache_path, {**metadata, "delta": delta})
        for item in delta["updates"]:
            key = (item["kind"], item["entity"], item["key"])
            state[key] = {**item, "volume": source_volume, "chapter": source_chapter}
        summaries.append({**delta["summary"], "volume": source_volume, "chapter": source_chapter})
        # Include the verified extraction, so replacing a corrupt cache invalidates descendants too.
        dependency = _digest(json.dumps({**metadata, "delta": delta}, ensure_ascii=False, sort_keys=True))
    return _render_context(state, summaries)
