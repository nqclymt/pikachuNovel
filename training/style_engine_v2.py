"""Scene-level Style Engine v2 helpers.

The module deliberately keeps source prose verbatim.  It provides deterministic fallbacks for
scene splitting/analysis/retrieval so the style engine remains usable even when an LLM call fails.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from core.prompt_loader import PromptLoader
from core.llm_provider import LLMCallCancelled
from core.text_encoding import read_text_file
from core.text_utils import parse_json_response

SCENE_MIN_CHARS = 420
SCENE_TARGET_CHARS = 1050
SCENE_MAX_CHARS = 1900
MAX_SCENES_PER_CHAPTER = 9

_TRANSITION_RE = re.compile(
    r"^(?:与此同时|片刻后|半晌后|不多时|很快|随后|接着|转眼|翌日|次日|第二天|清晨|午后|傍晚|夜里|夜色|"
    r"回到|来到|走进|离开|出了|到了|待到|直到|当晚|次晨|另一边|另一处|此时|这时|就在这时)"
)
_DIALOGUE_RE = re.compile(r"[“「『][^”」』]{1,500}[”」』]")
_SENTENCE_RE = re.compile(r"(?<=[。！？!?])")


def _compact_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _paragraphs_with_offsets(text: str) -> list[tuple[int, int, str]]:
    """Return non-empty paragraph blocks with exact source offsets."""
    result: list[tuple[int, int, str]] = []
    # Chinese TXT sources often use one newline per natural paragraph, not a blank line.
    for match in re.finditer(r"[^\r\n]+", text or ""):
        raw = match.group(0)
        if raw.strip():
            left = len(raw) - len(raw.lstrip())
            right = len(raw.rstrip())
            start = match.start() + left
            end = match.start() + right
            result.append((start, end, text[start:end]))
    if not result and (text or "").strip():
        stripped = text.strip()
        start = text.find(stripped)
        result.append((start, start + len(stripped), stripped))
    return result


def _paragraph_mode(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return "empty"
    dialogue = sum(len(m.group(0)) for m in _DIALOGUE_RE.finditer(compact)) / max(1, len(compact))
    action_hits = sum(compact.count(word) for word in ("冲", "退", "挥", "刺", "斩", "抓", "抬手", "转身", "跑", "扑", "砸", "撞"))
    exposition_hits = sum(compact.count(word) for word in ("因为", "原来", "所谓", "便是", "分为", "意味着", "规则", "境界", "历史", "传说"))
    if dialogue >= 0.42:
        return "dialogue"
    if action_hits >= 3:
        return "action"
    if exposition_hits >= 3:
        return "exposition"
    return "narrative"


def split_scenes_verbatim(content: str, title: str = "") -> list[dict[str, Any]]:
    """Split one chapter into contiguous scene-sized source slices without rewriting text."""
    text = str(content or "")
    if not text.strip():
        return []
    body_offset = len(text) - len(text.lstrip())
    first_match = re.match(r"[^\r\n]*(?:\r\n|\r|\n|$)", text[body_offset:])
    first = first_match.group(0).strip() if first_match else ""
    if first_match and ((title and first == str(title).strip()) or re.match(r"^(?:\d+[.、]\s*)?第.{0,45}[章回节](?:\s|$)", first)):
        body_offset += first_match.end()
        body_offset += len(text[body_offset:]) - len(text[body_offset:].lstrip())
    body = text[body_offset:]
    paragraphs = []
    for start, end, raw in _paragraphs_with_offsets(body):
        if _compact_len(raw) <= SCENE_MAX_CHARS:
            paragraphs.append((start, end, raw))
            continue
        # Subdivide oversized paragraphs only at sentence ends outside paired dialogue quotes.
        cuts, quoted, last = [], [], 0
        pairs = {"“": "”", "「": "」", "『": "』", "‘": "’"}
        for offset, char in enumerate(raw):
            if char in pairs:
                quoted.append(pairs[char])
            elif quoted and char == quoted[-1]:
                quoted.pop()
            boundary = not quoted and (char in "。！？!?" or (char in pairs.values() and offset and raw[offset - 1] in "。！？!?"))
            if boundary and offset + 1 - last >= SCENE_TARGET_CHARS:
                cuts.append(offset + 1)
                last = offset + 1
        previous = 0
        for cut in cuts + [len(raw)]:
            if cut > previous:
                paragraphs.append((start + previous, start + cut, raw[previous:cut]))
            previous = cut
    if not paragraphs:
        return []

    boundaries = [0]
    running = 0
    last_mode = _paragraph_mode(paragraphs[0][2])
    for idx, (_, _, paragraph) in enumerate(paragraphs):
        plen = _compact_len(paragraph)
        if idx == 0:
            running = plen
            continue
        mode = _paragraph_mode(paragraph)
        transition = bool(_TRANSITION_RE.match(paragraph.lstrip()))
        mode_shift = mode != last_mode and {mode, last_mode} != {"narrative", "exposition"}
        should_split = (
            running >= SCENE_MIN_CHARS
            and (transition or (mode_shift and running >= SCENE_TARGET_CHARS * 0.7) or running + plen > SCENE_MAX_CHARS)
        )
        if should_split:
            boundaries.append(idx)
            running = plen
        else:
            running += plen
        last_mode = mode
    boundaries.append(len(paragraphs))

    scenes: list[dict[str, Any]] = []
    for scene_index, (start_idx, end_idx) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        if start_idx >= end_idx:
            continue
        start = paragraphs[start_idx][0]
        end = paragraphs[end_idx - 1][1]
        scene_text = body[start:end]
        if scenes and _compact_len(scene_text) < SCENE_MIN_CHARS * 0.55:
            previous = scenes[-1]
            merged_start = int(previous["body_start"])
            merged_text = body[merged_start:end]
            previous.update({"text": merged_text, "body_end": end, "source_end": body_offset + end, "char_count": _compact_len(merged_text)})
            continue
        scenes.append({
            "scene": scene_index,
            "text": scene_text,
            "body_start": start,
            "body_end": end,
            "source_start": body_offset + start,
            "source_end": body_offset + end,
            "char_count": _compact_len(scene_text),
        })

    # Very long chapters may still produce too many scenes; merge nearest neighbors without altering source order.
    while len(scenes) > MAX_SCENES_PER_CHAPTER:
        pair = min(range(len(scenes) - 1), key=lambda i: scenes[i]["char_count"] + scenes[i + 1]["char_count"])
        left, right = scenes[pair], scenes[pair + 1]
        merged_start, merged_end = int(left["body_start"]), int(right["body_end"])
        merged = dict(left)
        merged.update({"text": body[merged_start:merged_end], "body_end": merged_end, "source_end": body_offset + merged_end})
        merged["char_count"] = _compact_len(merged["text"])
        scenes[pair:pair + 2] = [merged]
    for index, scene in enumerate(scenes, start=1):
        scene["scene"] = index
        scene["position"] = "full" if len(scenes) == 1 else "opening" if index == 1 else "ending" if index == len(scenes) else "middle"
        scene["boundary_source"] = "heuristic"
        assert text[scene["source_start"]:scene["source_end"]] == scene["text"]
    return scenes


def _keyword_score(text: str, mapping: dict[str, tuple[str, ...]]) -> list[tuple[int, str]]:
    normalized = (text or "").lower()
    scored = []
    for key, words in mapping.items():
        score = sum(normalized.count(word.lower()) for word in words)
        if score:
            scored.append((score, key))
    return sorted(scored, key=lambda item: (-item[0], item[1]))


_SCENE_TYPE_WORDS = {
    "dialogue": ("对话", "聊天", "询问", "回答", "谈判", "试探", "说道", "开口"),
    "confrontation": ("冲突", "争执", "对峙", "质问", "羞辱", "嘲讽", "威胁", "逼迫"),
    "action": ("战斗", "交手", "攻击", "出手", "追杀", "逃", "剑", "刀", "拳"),
    "investigation": ("调查", "线索", "寻找", "追踪", "怀疑", "观察", "秘密"),
    "emotional": ("悲伤", "愤怒", "压抑", "心疼", "关系", "感情", "尴尬", "恐惧"),
    "exposition": ("设定", "规则", "境界", "来历", "历史", "势力", "说明", "介绍"),
    "transition": ("转场", "赶路", "抵达", "离开", "翌日", "第二天", "与此同时"),
    "climax": ("决战", "爆发", "生死", "翻盘", "反击", "击败", "高潮"),
    "aftermath": ("事后", "余波", "收拾", "疗伤", "休息", "善后"),
    "daily": ("吃饭", "回家", "休息", "闲聊", "生活", "玩笑", "日常"),
}
_EMOTION_WORDS = {
    "suppressed_tension": ("压抑", "试探", "戒备", "紧绷", "沉默", "冷淡", "不动声色"),
    "humiliation": ("羞辱", "轻贱", "嘲讽", "废物", "冷眼", "看不起", "憋屈"),
    "fear": ("恐惧", "害怕", "惊", "心惊", "危险", "不安"),
    "anger": ("愤怒", "怒", "火气", "恼", "杀意"),
    "sadness": ("悲伤", "难过", "失落", "苦涩", "绝望"),
    "warmth": ("温柔", "安心", "笑", "亲近", "关心", "温暖"),
    "curiosity": ("好奇", "疑惑", "不解", "想知道", "谜"),
}
_PLOT_FUNCTION_WORDS = {
    "setup": ("开场", "铺垫", "交代", "建立", "处境"),
    "escalation": ("升级", "加压", "恶化", "逼迫", "冲突"),
    "reveal": ("揭露", "揭晓", "真相", "原来", "发现", "暴露"),
    "payoff": ("反击", "打脸", "翻盘", "击败", "回收", "兑现"),
    "transition": ("转场", "过渡", "赶路", "抵达", "离开"),
    "relationship": ("关系", "信任", "误会", "亲近", "疏远", "感情"),
}
_INFO_FUNCTION_WORDS = {
    "partial_reveal": ("试探", "暗示", "线索", "隐瞒", "只说", "没有说", "部分"),
    "full_reveal": ("真相", "揭晓", "坦白", "说清", "全部"),
    "foreshadowing": ("伏笔", "预兆", "异常", "不对劲", "暗中", "悄然"),
    "worldbuilding": ("世界观", "规则", "历史", "势力", "境界", "制度"),
    "character_state": ("身份", "关系", "状态", "伤势", "实力", "处境"),
}


def infer_scene_metadata(text: str, *, position: str = "middle", semantic_hint: str = "") -> dict[str, Any]:
    # Chapter-wide hints describe other scenes too. They must not determine this scene's labels.
    combined = str(text or "")
    compact = re.sub(r"\s+", "", text or "")
    scene_scores = _keyword_score(combined, _SCENE_TYPE_WORDS)
    emotion_scores = _keyword_score(combined, _EMOTION_WORDS)
    plot_scores = _keyword_score(combined, _PLOT_FUNCTION_WORDS)
    info_scores = _keyword_score(combined, _INFO_FUNCTION_WORDS)
    dialogue_ratio = sum(len(m.group(0)) for m in _DIALOGUE_RE.finditer(compact)) / max(1, len(compact))
    punctuation = max(1, sum(compact.count(mark) for mark in "。！？!?"))
    short_sentences = sum(1 for part in _SENTENCE_RE.split(compact) if 0 < len(part) <= 12)
    short_ratio = short_sentences / punctuation
    conflict_hits = sum(combined.count(word) for word in ("冲突", "威胁", "追杀", "攻击", "羞辱", "对峙", "生死", "逼迫"))
    conflict_level = "high" if conflict_hits >= 5 else "medium" if conflict_hits >= 2 else "low"
    pacing = "fast" if short_ratio >= 0.38 or conflict_level == "high" else "slow" if dialogue_ratio < 0.12 and len(compact) > 1000 else "medium"
    ending_type = "hook" if position == "ending" and any(word in combined for word in ("突然", "却", "竟", "秘密", "危险", "来人", "消息", "发现")) else "open" if position == "ending" else "continuation"
    scene_type = scene_scores[0][1] if scene_scores else ("dialogue" if dialogue_ratio >= 0.4 else "daily")
    style_tags = [item[1] for item in scene_scores[:3]]
    if dialogue_ratio >= 0.45:
        style_tags.append("dialogue_heavy")
    if short_ratio >= 0.38:
        style_tags.append("short_sentence_rhythm")
    if position in {"opening", "ending"}:
        style_tags.append(position)
    max_signal = max([score for score, _ in scene_scores[:1] + emotion_scores[:1] + plot_scores[:1] + info_scores[:1]] or [0])
    # A heuristic signal is not a calibrated confidence probability.
    confidence = round(min(0.70, 0.25 + max_signal * 0.04), 2)
    return {
        "scene_type": scene_type,
        "emotion": emotion_scores[0][1] if emotion_scores else "neutral",
        "conflict_level": conflict_level,
        "plot_function": plot_scores[0][1] if plot_scores else ("setup" if position == "opening" else "transition" if scene_type == "transition" else "development"),
        "information_function": info_scores[0][1] if info_scores else "implicit_progress",
        "pacing": pacing,
        "ending_type": ending_type,
        "style_tags": list(dict.fromkeys(style_tags)),
        "analysis_method": "local_heuristic",
        "analysis_source": "heuristic",
        "analysis_confidence": confidence,
        "analysis_uncertainty": round(1.0 - confidence, 2),
    }


def normalize_scene_query(query: Any) -> dict[str, Any]:
    base = dict(query) if isinstance(query, dict) else {}
    nested = base.get("style_retrieval_query") if isinstance(base.get("style_retrieval_query"), dict) else {}
    text = "\n".join(str(base.get(key) or "") for key in ("scene_goal", "conflict", "text")) if base else str(query or "")
    inferred = infer_scene_metadata(text, position=str(base.get("position") or "middle"))
    allowed = {
        "scene_type": set(_SCENE_TYPE_WORDS), "emotion": set(_EMOTION_WORDS) | {"neutral"},
        "conflict_level": {"low", "medium", "high"},
        "plot_function": set(_PLOT_FUNCTION_WORDS) | {"development"},
        "information_function": set(_INFO_FUNCTION_WORDS) | {"implicit_progress"},
        "pacing": {"slow", "medium", "fast"}, "ending_type": {"hook", "open", "continuation"},
    }
    for key, values in allowed.items():
        value = base.get(key) or nested.get(key)
        if isinstance(value, str) and value.strip() in values:
            inferred[key] = value.strip()
    explicit_tags = base.get("style_tags", nested.get("style_tags", []))
    if isinstance(explicit_tags, list):
        inferred["style_tags"] = list(dict.fromkeys([*inferred["style_tags"], *[tag[:80] for tag in explicit_tags if isinstance(tag, str)]]))[:20]
    inferred["text"] = text
    return inferred


def _tokens(text: str) -> Counter[str]:
    lowered = str(text or "").lower()
    tokens: list[str] = re.findall(r"[a-z0-9_]{2,}", lowered)
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        for width in (2, 3, 4):
            tokens.extend(sequence[i:i + width] for i in range(max(0, len(sequence) - width + 1)))
    return Counter(tokens)


def _lexical_similarity(a: str, b: str) -> float:
    ca, cb = _tokens(a), _tokens(b)
    if not ca or not cb:
        return 0.0
    intersection = sum(ca[token] * cb[token] for token in ca.keys() & cb.keys())
    denom = math.sqrt(sum(v * v for v in ca.values()) * sum(v * v for v in cb.values()))
    return intersection / denom if denom else 0.0


def _metadata_score(item: dict[str, Any], query: dict[str, Any]) -> tuple[float, list[str]]:
    """Cheap first-stage score over structured scene metadata only."""
    score = 0.0
    reasons: list[str] = []
    weights = {
        "scene_type": 16.0,
        "emotion": 8.0,
        "conflict_level": 6.0,
        "plot_function": 7.0,
        "information_function": 8.0,
        "pacing": 4.0,
        "ending_type": 2.5,
    }
    for key, weight in weights.items():
        qv = str(query.get(key) or "")
        iv = str(item.get(key) or "")
        if qv and iv and qv == iv:
            score += weight
            reasons.append(f"{key}={qv}")
    qtags = {tag for tag in (query.get("style_tags") or []) if isinstance(tag, str)} if isinstance(query.get("style_tags"), list) else set()
    itags = {tag for tag in (item.get("style_tags") or []) if isinstance(tag, str)} if isinstance(item.get("style_tags"), list) else set()
    overlap = len(qtags & itags)
    if overlap:
        score += overlap * 2.5
        reasons.append(f"style_tags×{overlap}")
    try:
        confidence = float(item.get("analysis_confidence") or 0.0)
        confidence = min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    score += confidence * 1.2
    return score, reasons


def score_style_example(item: dict[str, Any], query: dict[str, Any]) -> tuple[float, list[str]]:
    score, reasons = _metadata_score(item, query)
    semantic = str(item.get("semantic_text") or "")
    lexical = _lexical_similarity(str(query.get("text") or ""), semantic)
    if lexical:
        score += lexical * 12.0
        reasons = [*reasons, f"lexical={lexical:.2f}"]
    return score, reasons


def retrieve_diverse_style_examples(reference_dir: str | Path, items: list[dict[str, Any]], query: Any, *, max_samples: int = 4, max_chars: int = 7600, exclude_ids=None, cancel_event=None) -> list[dict[str, Any]]:
    reference_dir = Path(reference_dir)
    max_samples, max_chars = max(0, min(8, int(max_samples))), max(0, int(max_chars))
    if not max_samples or not max_chars:
        return []
    _check_cancel(cancel_event)
    excluded = set(exclude_ids or [])
    normalized = normalize_scene_query(query)
    # Stage 1: structured metadata narrows thousands of examples cheaply. Stage 2 then pays
    # for Chinese n-gram semantics only on the strongest candidates plus a small diversity sample.
    metadata_ranked = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("id") or "") in excluded:
            continue
        try:
            int(item.get("chapter") or 0)
            int(item.get("scene") or 0)
        except (TypeError, ValueError):
            continue
        _check_cancel(cancel_event)
        score, reasons = _metadata_score(item, normalized)
        metadata_ranked.append((score, item, reasons))
    metadata_ranked.sort(key=lambda row: (-row[0], int(row[1].get("chapter") or 0), int(row[1].get("scene") or 0)))
    shortlist_size = min(len(metadata_ranked), max(320, max_samples * 100))
    shortlist = list(metadata_ranked[:shortlist_size])
    # Keep a deterministic sparse tail so a lexical-only match can still enter the rerank set.
    tail = metadata_ranked[shortlist_size:]
    if tail:
        step = max(1, len(tail) // 80)
        shortlist.extend(tail[::step][:80])
    ranked = []
    query_text = str(normalized.get("text") or "")
    for meta_score, item, reasons in shortlist:
        lexical = _lexical_similarity(query_text, str(item.get("semantic_text") or ""))
        full_score = meta_score + lexical * 12.0
        full_reasons = [*reasons]
        if lexical:
            full_reasons.append(f"lexical={lexical:.2f}")
        ranked.append((full_score, item, full_reasons))
    ranked.sort(key=lambda row: (-row[0], int(row[1].get("chapter") or 0), int(row[1].get("scene") or 0)))

    # Load a bounded candidate pool, then recompute marginal relevance after EACH selection.
    candidates = []
    seen_texts = set()
    for base_score, item, reasons in ranked[:max(64, max_samples * 24)]:
        _check_cancel(cancel_event)
        path = safe_style_sample_path(reference_dir, item.get("path"))
        if path is None:
            continue
        try:
            text = read_text_file(path)[0].strip()
        except (OSError, UnicodeError):
            continue
        digest = hashlib.sha256(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()
        if not text or digest in seen_texts:
            continue
        seen_texts.add(digest)
        candidates.append((base_score, item, reasons, text, _tokens(text[:1800])))
    selected: list[dict[str, Any]] = []
    selected_tokens = []
    used_chars = 0
    while candidates and len(selected) < max_samples and used_chars < max_chars:
        _check_cancel(cancel_event)
        choices = []
        for pos, (base_score, item, reasons, text, tokens) in enumerate(candidates):
            similarities = []
            for other in selected_tokens:
                denom = math.sqrt(sum(v*v for v in tokens.values()) * sum(v*v for v in other.values()))
                similarities.append(sum(tokens[t]*other[t] for t in tokens.keys() & other.keys()) / denom if denom else 0.0)
            similarity = max(similarities, default=0.0)
            if similarity >= 0.92:
                continue
            same_chapter = any(previous.get("chapter") == item.get("chapter") for previous in selected)
            final_score = base_score - (9.0 if same_chapter else 0.0) - 12.0 * similarity
            choices.append((final_score, -pos, pos))
        if not choices:
            break
        final_score, _, pos = max(choices)
        base_score, item, reasons, text, tokens = candidates.pop(pos)
        clipped = _bounded_source_excerpt(text, max_chars - used_chars)
        if not clipped:
            continue
        payload = dict(item)
        payload.update({"text": clipped, "base_score": round(base_score, 3),
                        "selection_score": round(final_score, 3), "match_reasons": reasons})
        selected.append(payload)
        selected_tokens.append(tokens)
        used_chars += len(clipped)
    return selected


def safe_style_sample_path(reference_dir: str | Path, value: Any) -> Path | None:
    """Index metadata may not escape the reference library, even through a symlink."""
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        return None
    root = (Path(reference_dir) / "style_library").resolve()
    candidate = (Path(reference_dir) / value).resolve()
    try:
        candidate.relative_to(root)
        return candidate if candidate.is_file() and candidate.stat().st_size <= 2000000 else None
    except (ValueError, OSError):
        return None


def _bounded_source_excerpt(text: str, limit: int) -> str:
    """Use a continuous prefix ending at a complete sentence/paragraph, never fabricate joins."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    prefix, quoted, boundary = text[:limit], [], 0
    pairs = {"“": "”", "「": "」", "『": "』", "‘": "’"}
    for offset, char in enumerate(prefix):
        if char in pairs:
            quoted.append(pairs[char])
        elif quoted and char == quoted[-1]:
            quoted.pop()
        if not quoted and (char in "。！？!?\n" or (char in pairs.values() and offset and prefix[offset - 1] in "。！？!?")):
            boundary = offset + 1
    return prefix[:boundary].rstrip() if boundary else ""


def _outline_event_blocks(chapter_outline: str) -> list[str]:
    """Extract events, not formatting headings or whole-arc future plot, for fallback planning."""
    source = str(chapter_outline or "").strip()
    section = ""
    sections: dict[str, list[str]] = {}
    plain = []
    labels = ("故事线", "单章简介", "剧情简介", "单章节奏", "人物状态", "伏笔", "章末", "Story Line")
    for raw in source.splitlines():
        line = raw.strip()
        clean = re.sub(r"^#{1,6}\s*", "", line).strip("* ")
        if re.fullmatch(r"【?第[^\n]{0,45}章[^\n]*】?", clean):
            continue
        matched = next((label for label in labels if clean == label or clean.startswith(label + "：") or clean.startswith(label + ":")), None)
        if matched:
            section = matched
            sections.setdefault(section, [])
            value = clean[len(matched):].lstrip("：: ")
            if value:
                sections[section].append(value)
            continue
        if re.match(r"^#{1,6}\s+", line):
            section = "other"
        elif line:
            sections.setdefault(section, []).append(line)
            if not section:
                plain.append(line)
    # Synopsis retains the actual on-page events; rhythm headings do not become pseudo-scenes.
    synopsis = sections.get("单章简介") or sections.get("剧情简介")
    if synopsis:
        return synopsis
    story_line = " ".join(sections.get("故事线") or sections.get("Story Line") or [])
    if story_line:
        return [part.strip() for part in re.split(r"[+＋→]+", story_line) if part.strip()]
    return plain or ([source] if source and not sections else [])


def fallback_scene_plan(chapter_outline: str, story_arc: str = "", instruction: str = "", *, max_scenes: int = 6) -> list[dict[str, Any]]:
    max_scenes = max(1, min(8, int(max_scenes)))
    blocks = _outline_event_blocks(chapter_outline)
    if not blocks:
        blocks = ["按当前章纲完成本章既定事件；未能识别场景结构，不引入后续情节。"]
    if len(blocks) > max_scenes:
        chunk = math.ceil(len(blocks) / max_scenes)
        blocks = [" ".join(blocks[i:i + chunk]) for i in range(0, len(blocks), chunk)]
    scenes = []
    for index, block in enumerate(blocks[:max_scenes], start=1):
        position = "opening" if index == 1 else "ending" if index == min(len(blocks), max_scenes) else "middle"
        metadata = infer_scene_metadata(block, position=position)
        scenes.append({
            "scene": index,
            "scene_goal": block[:500],
            "character_goals": {},
            "conflict": block[:300] if metadata["conflict_level"] != "low" else "",
            "new_information": [],
            "hidden_information": [],
            "emotional_start": metadata["emotion"],
            "emotional_end": metadata["emotion"],
            "foreshadowing": [],
            "payoffs": [],
            "style_retrieval_query": {
                key: metadata[key] for key in ("scene_type", "emotion", "conflict_level", "plot_function", "information_function", "pacing")
            },
            "ending_hook": "" if position != "ending" else block[-160:],
            "planner_source": "deterministic_fallback",
        })
    return scenes


def _check_cancel(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise LLMCallCancelled("场景规划/检索已取消")


def _text_list(value: Any) -> list[str]:
    return [item.strip()[:800] for item in value if isinstance(item, str) and item.strip()][:20] if isinstance(value, list) else []


def plan_chapter_scenes(llm: Any, chapter_outline: str, story_arc: str = "", recent_context: str = "", instruction: str = "", *, cancel_event=None) -> list[dict[str, Any]]:
    _check_cancel(cancel_event)
    fallback = fallback_scene_plan(chapter_outline, story_arc, instruction)
    if llm is None:
        return fallback
    # Template/programming errors are not provider failures: never silently mask them as success.
    prompt = PromptLoader.load(
        "style_scene_plan", chapter_outline=chapter_outline,
        story_arc=story_arc or "（无）", recent_context=recent_context[-6000:] if recent_context else "（无）",
        instruction=instruction or "（无）",
    )
    if len(prompt) > 60000:
        raise ValueError("场景规划上下文超过60000字符；请检查章纲或情节单元，未截断剧情事实。")
    try:
        if cancel_event is not None and hasattr(llm, "generate_cancelable"):
            raw = llm.generate_cancelable(prompt, cancel_event, temperature=0.1, is_json=True)
        else:
            raw = llm.generate(prompt, temperature=0.1, is_json=True)
        _check_cancel(cancel_event)
        parsed = parse_json_response(raw or "")
        scenes = parsed.get("scenes") if isinstance(parsed, dict) else None
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 8:
            raise ValueError("scene_count_invalid")
        normalized = []
        for index, item in enumerate(scenes, start=1):
            if not isinstance(item, dict) or not isinstance(item.get("scene_goal"), str) or not item["scene_goal"].strip():
                raise ValueError("scene_goal_missing")
            query = normalize_scene_query(item)
            normalized.append({
                "scene": index, "scene_goal": item["scene_goal"].strip()[:800],
                "character_goals": {str(k)[:80]: str(v)[:500] for k, v in list(item.get("character_goals", {}).items())[:20]} if isinstance(item.get("character_goals"), dict) else {},
                "conflict": str(item.get("conflict") or "").strip()[:600],
                "new_information": _text_list(item.get("new_information")),
                "hidden_information": _text_list(item.get("hidden_information")),
                "emotional_start": str(item.get("emotional_start") or query["emotion"])[:300],
                "emotional_end": str(item.get("emotional_end") or query["emotion"])[:300],
                "foreshadowing": _text_list(item.get("foreshadowing")), "payoffs": _text_list(item.get("payoffs")),
                "style_retrieval_query": {key: query[key] for key in ("scene_type", "emotion", "conflict_level", "plot_function", "information_function", "pacing")},
                "ending_hook": str(item.get("ending_hook") or "").strip()[:500], "planner_source": "llm",
            })
        print(f"  -> 场景规划：模型成功返回 {len(normalized)} 个场景。")
        return normalized
    except LLMCallCancelled:
        raise
    except Exception as exc:
        # Preserve basic generation, but expose degradation in both logs and saved scene plans.
        reason = type(exc).__name__
        print(f"  -> 警告：场景规划未通过（{reason}），使用本章事件兜底；不是模型规划结果。")
        for scene in fallback:
            scene["planner_error"] = reason
        return fallback


def build_scene_style_context(reference_dir: str | Path, index_items: list[dict[str, Any]], scene_plan: list[dict[str, Any]], *, samples_per_scene: int = 4, chars_per_scene: int = 5200, max_total_chars: int = 18000, trace: list | None = None, cancel_event=None) -> str:
    if max_total_chars <= 0:
        return ""
    scenes = [item for item in scene_plan[:8] if isinstance(item, dict)]
    blocks, used_ids = [], set()
    for number, scene in enumerate(scenes):
        _check_cancel(cancel_event)
        used = sum(len(block) for block in blocks) + 2 * len(blocks)
        remaining = max_total_chars - used
        if remaining <= 0:
            break
        quota = min(chars_per_scene, remaining // (len(scenes) - number))
        query = dict(scene.get("style_retrieval_query") or {})
        query["scene_goal"] = scene.get("scene_goal") or ""
        query["conflict"] = scene.get("conflict") or ""
        # Goals/facts are already present in Scene Plan. Do not duplicate or truncate them here.
        heading = f"### 目标 Scene {scene.get('scene')} 的写法案例\n"
        allowance = max(0, quota - len(heading))
        examples = retrieve_diverse_style_examples(
            reference_dir, index_items, query, max_samples=samples_per_scene,
            max_chars=allowance, exclude_ids=used_ids, cancel_event=cancel_event,
        )
        rendered, records = [], []
        for example in examples:
            meta = "/".join(str(example.get(key) or "")[:60] for key in ("scene_type", "emotion", "conflict_level", "information_function"))
            header = f"【案例 {example.get('id')}｜参考章{example.get('chapter')} 场景{example.get('scene')}｜{meta}】\n"
            left = allowance - sum(len(x) + 2 for x in rendered) - len(header)
            text = _bounded_source_excerpt(example["text"], left)
            if not text:
                continue
            rendered.append(header + text)
            used_ids.add(str(example.get("id") or ""))
            records.append({"id": example.get("id"), "chapter": example.get("chapter"), "scene": example.get("scene"),
                            "chars": len(text), "score": example.get("selection_score"), "reasons": example.get("match_reasons")})
        body = "\n\n".join(rendered) or "（没有合适且符合预算的案例；以章纲和作者画像为准。）"
        block = heading + body
        if len(block) <= quota:
            blocks.append(block)
        if trace is not None:
            trace.append({"scene": scene.get("scene"), "query": query, "examples": records, "budget": quota})
    result = "\n\n".join(blocks)
    assert len(result) <= max_total_chars
    return result
