"""Build and retrieve a persistent human-written prose style library from a reference novel.

The library is deliberately separate from plot/story decomposition.  It keeps continuous
source windows so downstream drafting can learn writing mechanics without turning later
AI-generated chapters into style authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.prompt_loader import PromptLoader
from core.llm_provider import LLMCallCancelled
from core.text_encoding import read_text_file
from core.text_utils import normalize_text, parse_json_response
from training.style_engine_v2 import (
    infer_scene_metadata,
    normalize_scene_query,
    retrieve_diverse_style_examples,
    split_scenes_verbatim,
    safe_style_sample_path,
    _check_cancel,
)


STYLE_LIBRARY_VERSION = 2
STYLE_PIPELINE_REVISION = 3
STYLE_DIR_NAME = "style_library"
SAMPLE_MIN_CHARS = 1000
SAMPLE_TARGET_CHARS = 1750
SAMPLE_MAX_CHARS = 2400
PROFILE_SAMPLE_COUNT = 14
PROFILE_SAMPLE_CLIP = 1250

SCENE_TYPES: dict[str, dict[str, Any]] = {
    "dialogue": {"label": "人物对话", "keywords": ("对话", "交谈", "询问", "回答", "争辩", "谈判", "开口", "说道", "聊天")},
    "battle": {"label": "战斗动作", "keywords": ("战斗", "交手", "出手", "攻击", "厮杀", "搏杀", "剑", "刀", "拳", "招式", "敌人", "击败")},
    "daily": {"label": "日常互动", "keywords": ("日常", "吃饭", "回家", "休息", "闲聊", "生活", "逛", "玩笑", "相处")},
    "suspense": {"label": "悬疑留白", "keywords": ("悬疑", "秘密", "异常", "疑惑", "谜", "线索", "不对劲", "隐瞒", "真相")},
    "cultivation": {"label": "修炼成长", "keywords": ("修炼", "突破", "境界", "功法", "灵气", "斗气", "升级", "炼化", "闭关")},
    "exposition": {"label": "设定说明", "keywords": ("世界观", "设定", "规则", "制度", "来历", "介绍", "说明", "等级", "势力", "历史")},
    "emotion": {"label": "情绪关系", "keywords": ("感情", "愤怒", "悲伤", "紧张", "开心", "恐惧", "尴尬", "心疼", "信任", "不安", "心惊", "压抑")},
    "humiliation": {"label": "身份受辱", "keywords": ("受辱", "羞辱", "嘲讽", "轻贱", "废物", "落魄", "冷眼", "看不起", "驱赶", "憋屈", "身份落差")},
    "exploration": {"label": "探索发现", "keywords": ("探索", "发现", "寻找", "进入", "遗迹", "调查", "观察", "追踪", "陌生")},
    "crisis": {"label": "危机压迫", "keywords": ("危机", "危险", "追杀", "威胁", "困境", "绝境", "恐慌", "逼近", "生死")},
    "reveal": {"label": "揭示反转", "keywords": ("揭晓", "揭露", "反转", "真相", "身份揭露", "身份暴露", "原来", "暴露", "秘密", "意外")},
    "transition": {"label": "场景过渡", "keywords": ("转场", "过渡", "赶路", "离开", "抵达", "翌日", "第二天", "途中", "与此同时")},
    "payoff": {"label": "爽点反击", "keywords": ("爽点", "打脸", "反击", "碾压", "震惊", "扬眉", "报复", "翻盘", "胜利")},
    "opening": {"label": "章节开场", "keywords": ("开场", "开篇", "章初", "起笔", "开局")},
    "ending": {"label": "章末钩子", "keywords": ("章末", "结尾", "钩子", "悬念", "收尾", "结束", "下一章")},
}

PROFILE_KEYS = (
    "voice_summary",
    "narrative_distance",
    "viewpoint_control",
    "sentence_rhythm",
    "paragraph_rhythm",
    "dialogue_style",
    "dialogue_action_link",
    "action_style",
    "psychology_style",
    "exposition_style",
    "scene_entry",
    "scene_transition",
    "information_release",
    "conflict_escalation",
    "emotion_expression",
    "imagery_and_diction",
    "chapter_opening",
    "chapter_ending",
    "deliberate_irregularities",
)


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(read_text_file(path)[0])
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_chapter_heading(content: str, title: str = "") -> str:
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    first, sep, rest = text.partition("\n")
    first_clean = first.strip()
    if sep and (
        (title and first_clean == str(title).strip())
        or re.match(r"^(?:\d+[.、]\s*)?第.{0,45}[章回节](?:\s|$)", first_clean)
    ):
        return rest.strip()
    return text


def _adjust_slice(text: str, start: int, end: int) -> str:
    start = max(0, start)
    end = min(len(text), max(start, end))
    if start > 0:
        candidates = [text.find("\n\n", start, min(end, start + 260)), text.find("\n", start, min(end, start + 180))]
        found = min((value for value in candidates if value >= 0), default=-1)
        if found >= 0:
            start = found + (2 if text[found:found + 2] == "\n\n" else 1)
    if end < len(text):
        candidates = [text.rfind("\n\n", max(start, end - 260), end), text.rfind("\n", max(start, end - 180), end)]
        found = max(candidates)
        if found > start + 300:
            end = found
    return text[start:end].strip()


def continuous_sample_windows(content: str, title: str = "") -> list[dict[str, str]]:
    """Return one to three continuous source windows. No scattered paragraph stitching."""
    body = _strip_chapter_heading(content, title)
    if not body:
        return []
    compact_len = len(re.sub(r"\s+", "", body))
    if compact_len <= SAMPLE_MAX_CHARS:
        return [{"position": "full", "text": body}]

    windows: list[dict[str, str]] = []
    opening = _adjust_slice(body, 0, min(len(body), SAMPLE_TARGET_CHARS))
    if len(re.sub(r"\s+", "", opening)) >= min(SAMPLE_MIN_CHARS, compact_len // 3):
        windows.append({"position": "opening", "text": opening})

    if compact_len >= 5200:
        middle_start = max(0, len(body) // 2 - SAMPLE_TARGET_CHARS // 2)
        middle = _adjust_slice(body, middle_start, min(len(body), middle_start + SAMPLE_TARGET_CHARS))
        if len(re.sub(r"\s+", "", middle)) >= SAMPLE_MIN_CHARS:
            windows.append({"position": "middle", "text": middle})

    ending_start = max(0, len(body) - SAMPLE_TARGET_CHARS)
    ending = _adjust_slice(body, ending_start, len(body))
    if len(re.sub(r"\s+", "", ending)) >= min(SAMPLE_MIN_CHARS, compact_len // 3):
        windows.append({"position": "ending", "text": ending})
    return windows or [{"position": "full", "text": body[:SAMPLE_MAX_CHARS]}]


def _scene_analysis_text(card: dict[str, Any] | None, title: str = "") -> str:
    card = card if isinstance(card, dict) else {}
    rhythm = card.get("chapter_rhythm") if isinstance(card.get("chapter_rhythm"), dict) else {}
    highlights = card.get("highlights") if isinstance(card.get("highlights"), list) else []
    return "\n".join(
        str(item or "")
        for item in (
            title,
            card.get("chapter_outline_600"),
            rhythm.get("core_content"),
            rhythm.get("emotion_tone"),
            rhythm.get("beat_detail"),
            card.get("story_line"),
            "；".join(str(value) for value in highlights),
        )
        if item
    )


def infer_scene_types(text: str, *, position: str = "") -> list[str]:
    normalized = (text or "").lower()
    scored: list[tuple[int, str]] = []
    for key, spec in SCENE_TYPES.items():
        if key in {"opening", "ending"}:
            continue
        score = sum(normalized.count(keyword.lower()) for keyword in spec["keywords"])
        if score:
            scored.append((score, key))
    tags = [key for _, key in sorted(scored, key=lambda item: (-item[0], item[1]))[:4]]
    if position in {"opening", "full"}:
        tags.append("opening")
    if position in {"ending", "full"}:
        tags.append("ending")
    if not tags:
        tags.append("general")
    return list(dict.fromkeys(tags))


def _style_metrics(text: str) -> dict[str, Any]:
    clean = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # The TXT importer treats a non-empty line as a natural paragraph. Adding a blank line
    # between chapters must not turn every chapter into one very long paragraph.
    paragraphs = [item.strip() for item in clean.splitlines() if item.strip()]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?])", clean) if item.strip()]
    sentence_lengths = [len(re.sub(r"\s+", "", item)) for item in sentences]
    paragraph_lengths = [len(re.sub(r"\s+", "", item)) for item in paragraphs]

    def stats(values: list[int]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        avg = sum(values) / len(values)
        variance = sum((value - avg) ** 2 for value in values) / len(values)
        return avg, math.sqrt(variance)

    sentence_avg, sentence_std = stats(sentence_lengths)
    paragraph_avg, paragraph_std = stats(paragraph_lengths)
    compact = re.sub(r"\s+", "", clean)
    dialogue_chars = sum(len(match.group(0)) for match in re.finditer(r"[“「『][^”」』]{1,600}[”」』]", compact))
    total_chars = max(1, len(compact))
    total_sentences = max(1, len(sentence_lengths))
    punctuation_marks = ("\uFF0C", "\u3002", "\uFF1F", "?", "\uFF01", "!", "\uFF1B", "\uFF1A")
    punctuation_total = max(1, sum(compact.count(mark) for mark in punctuation_marks))
    return {
        "sentence_count": len(sentence_lengths),
        "paragraph_count": len(paragraph_lengths),
        "sentence_avg": round(sentence_avg, 2),
        "sentence_std": round(sentence_std, 2),
        "short_sentence_ratio": round(sum(1 for n in sentence_lengths if n <= 12) / total_sentences, 4),
        "long_sentence_ratio": round(sum(1 for n in sentence_lengths if n >= 30) / total_sentences, 4),
        "paragraph_avg": round(paragraph_avg, 2),
        "paragraph_std": round(paragraph_std, 2),
        "dialogue_ratio": round(dialogue_chars / total_chars, 4),
        "comma_ratio": round(compact.count("\uFF0C") / punctuation_total, 4),
        "question_ratio": round((compact.count("\uFF1F") + compact.count("?")) / punctuation_total, 4),
        "exclamation_ratio": round((compact.count("\uFF01") + compact.count("!")) / punctuation_total, 4),
    }


def _load_cards(output_dir: Path, target: int) -> dict[int, dict[str, Any]]:
    cards: dict[int, dict[str, Any]] = {}
    cards_dir = output_dir / "chapter_cards"
    for number in range(1, target + 1):
        payload = _read_json(cards_dir / f"chapter_{number:04d}.json", {})
        if isinstance(payload, dict) and payload:
            cards[number] = payload
    return cards


def _representative_samples(index_items: list[dict[str, Any]], reference_dir: Path) -> list[dict[str, Any]]:
    if not index_items:
        return []
    ordered = sorted(index_items, key=lambda item: (int(item.get("chapter") or 0), int(item.get("scene") or 0)))
    count = min(PROFILE_SAMPLE_COUNT, len(ordered))
    selected, used_types, used_chapters = [], set(), set()
    # Stratify across the entire book first, then favor a new scene type/chapter within each stratum.
    for number in range(count):
        lo, hi = len(ordered) * number // count, len(ordered) * (number + 1) // count
        candidates = ordered[lo:hi]
        candidate = max(candidates, key=lambda item: (
            item.get("scene_type") not in used_types,
            item.get("chapter") not in used_chapters,
            min(int(item.get("char_count") or 0), PROFILE_SAMPLE_CLIP),
        ))
        selected.append(candidate)
        used_types.add(candidate.get("scene_type"))
        used_chapters.add(candidate.get("chapter"))
    result = []
    for item in selected:
        path = safe_style_sample_path(reference_dir, item.get("path"))
        if path is None:
            continue
        text = read_text_file(path)[0].strip()
        result.append({"id": item["id"], "scene_types": item.get("scene_types", []), "text": text[:PROFILE_SAMPLE_CLIP]})
    return result


def _normalize_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    # v2 keeps a structured Author Bible, while flat v1-compatible keys remain available to old callers.
    for section in ("narrative", "language", "dialogue", "emotion", "description", "plot"):
        value = payload.get(section)
        profile[section] = value if isinstance(value, dict) else {}
    flat_fallbacks = {
        "narrative_distance": ("narrative", "narrative_distance"),
        "viewpoint_control": ("narrative", "pov"),
        "information_release": ("narrative", "information_control"),
        "sentence_rhythm": ("language", "sentence_rhythm"),
        "paragraph_rhythm": ("language", "paragraph_rhythm"),
        "dialogue_style": ("dialogue", "frequency"),
        "dialogue_action_link": ("dialogue", "action_interleaving"),
        "emotion_expression": ("emotion", "action_based_expression"),
        "imagery_and_diction": ("description", "metaphor"),
        "conflict_escalation": ("plot", "conflict_escalation"),
        "chapter_opening": ("plot", "chapter_opening"),
        "chapter_ending": ("plot", "chapter_ending"),
    }
    for key in PROFILE_KEYS:
        value = payload.get(key)
        if not value and key in flat_fallbacks:
            section, nested = flat_fallbacks[key]
            value = profile.get(section, {}).get(nested)
        profile[key] = str(value or "").strip()[:1200]
    rules = payload.get("style_principles") or payload.get("reproduction_rules") or []
    avoid = payload.get("avoid_patterns") or []
    playbooks = payload.get("scene_playbooks") or {}
    profile["style_principles"] = [str(item).strip()[:500] for item in rules if str(item).strip()][:20] if isinstance(rules, list) else []
    profile["reproduction_rules"] = [str(item).strip()[:500] for item in rules if str(item).strip()][:20] if isinstance(rules, list) else []
    profile["avoid_patterns"] = [str(item).strip()[:500] for item in avoid if str(item).strip()][:20] if isinstance(avoid, list) else []
    if isinstance(playbooks, dict):
        profile["scene_playbooks"] = {
            str(key)[:40]: str(value).strip()[:1200]
            for key, value in playbooks.items()
            if str(value).strip()
        }
    else:
        profile["scene_playbooks"] = {}
    return profile


def _generate_profile(llm: Any, representative: list[dict[str, Any]], metrics: dict[str, Any], cancel_event=None) -> dict[str, Any]:
    if llm is None:
        raise RuntimeError("未配置参考拆解模型，无法提炼人工文笔画像。")
    prompt = PromptLoader.load(
        "reference_style_profile",
        metrics_json=json.dumps(metrics, ensure_ascii=False, indent=2),
        sample_material="\n\n===== 连续人工样本 =====\n\n".join(
            f"【样本 {item['id']}｜{','.join(item['scene_types'])}】\n{item['text']}" for item in representative
        ),
    )
    last_error: Exception | None = None
    current = prompt
    for attempt in range(3):
        _check_cancel(cancel_event)
        if cancel_event is not None and hasattr(llm, "generate_cancelable"):
            raw = llm.generate_cancelable(current, cancel_event, temperature=0.15, is_json=True)
        else:
            raw = llm.generate(current, temperature=0.15, is_json=True)
        _check_cancel(cancel_event)
        try:
            parsed = parse_json_response(raw or "")
            if not isinstance(parsed, dict):
                raise ValueError("模型没有返回 JSON 对象")
            normalized = _normalize_profile(parsed)
            if not _profile_has_content(normalized):
                raise ValueError("作者画像为空，不能标为高级画像已完成")
            return normalized
        except Exception as exc:  # noqa: BLE001 - model JSON needs bounded retries
            last_error = exc
            if attempt < 2:
                current = prompt + f"\n\n上次输出无法解析：{exc}\n请严格只返回合法 JSON 对象。"
    raise RuntimeError(f"人工文笔画像 JSON 解析失败：{last_error}")


def _profile_has_content(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_profile_has_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_profile_has_content(item) for item in value)
    return isinstance(value, str) and bool(value.strip())


def load_human_style_profile(reference_dir: str | Path, index=None) -> dict[str, Any]:
    reference_dir = Path(reference_dir)
    index = index if isinstance(index, dict) else _read_json(reference_dir / STYLE_DIR_NAME / "index.json", {})
    if not isinstance(index, dict):
        return {}
    path = safe_style_sample_path(reference_dir, index.get("profile_path") or f"{STYLE_DIR_NAME}/profile.json")
    profile = _read_json(path, {}) if path else {}
    if not isinstance(profile, dict) or profile.get("version") != STYLE_LIBRARY_VERSION:
        return {}
    if profile.get("source_digest") != index.get("source_digest") or profile.get("chapter_count") != index.get("chapter_count"):
        return {}
    return profile


def human_style_library_status(reference_dir: str | Path) -> dict[str, Any]:
    reference_dir = Path(reference_dir)
    root = reference_dir / STYLE_DIR_NAME
    index = _read_json(root / "index.json", {})
    profile = load_human_style_profile(reference_dir, index)
    samples = index.get("samples") if isinstance(index, dict) else []
    samples = samples if isinstance(samples, list) else []
    index_valid = bool(
        isinstance(index, dict)
        and index.get("version") == STYLE_LIBRARY_VERSION
        and samples
    )
    probes = samples[:2] + samples[-2:] if samples else []
    sample_files_ready = bool(probes) and all(
        isinstance(item, dict)
        and bool(item.get("path"))
        and safe_style_sample_path(reference_dir, item.get("path")) is not None
        for item in probes
    )
    profile_valid = bool(
        isinstance(profile, dict)
        and profile.get("version") == STYLE_LIBRARY_VERSION
    )
    advanced_profile_ready = bool(profile_valid and _profile_has_content(profile.get("profile")))
    ready = bool(index_valid and sample_files_ready)
    if advanced_profile_ready:
        profile_mode = "qualitative"
    elif profile_valid and profile.get("metrics"):
        profile_mode = "metrics_only"
    elif ready:
        profile_mode = "samples_only"
    else:
        profile_mode = "missing"
    return {
        "ready": ready,
        "advanced_profile_ready": advanced_profile_ready,
        "pipeline_revision": index.get("pipeline_revision", 0) if isinstance(index, dict) else 0,
        "needs_rebuild": bool(index_valid and index.get("pipeline_revision") != STYLE_PIPELINE_REVISION),
        "analysis_source": "heuristic_scene_boundaries_and_labels",
        "profile_mode": profile_mode,
        "profile_error": str(profile.get("profile_error") or "") if isinstance(profile, dict) else "",
        "version": int(index.get("version") or 0) if isinstance(index, dict) else 0,
        "sample_count": len(samples),
        "scene_example_count": int(index.get("scene_example_count") or len(samples)) if isinstance(index, dict) else len(samples),
        "engine": str(index.get("engine") or ("scene_style_engine_v2" if ready else "")) if isinstance(index, dict) else "",
        "chapter_count": int(index.get("chapter_count") or 0) if isinstance(index, dict) else 0,
        "updated_at": str(profile.get("generated_at") or index.get("generated_at") or "") if isinstance(profile, dict) else "",
        "path": str(root),
    }


def build_human_style_library(
    txt_path: str | Path,
    output_dir: str | Path,
    *,
    chapters: list[dict[str, Any]] | None = None,
    cards: list[dict[str, Any]] | dict[int, dict[str, Any]] | None = None,
    max_chapters: int | None = None,
    llm: Any | None = None,
    force: bool = False,
    cancel_event=None,
) -> dict[str, Any]:
    """Build continuous prose samples and one qualitative authorial-mechanics profile."""
    txt_path = Path(txt_path)
    output_dir = Path(output_dir)
    _check_cancel(cancel_event)
    if not txt_path.is_file():
        raise FileNotFoundError(f"未找到参考小说：{txt_path}")
    if chapters is None:
        from training.outline_builder import split_chapters
        _, chapters = split_chapters(str(txt_path))
    target = min(max_chapters or len(chapters), len(chapters))
    if target < 1:
        raise ValueError("人工文笔库至少需要 1 章参考正文。")
    chapters = list(chapters[:target])
    source_digest = _file_digest(txt_path)
    style_root = output_dir / STYLE_DIR_NAME
    sample_dir = style_root / "samples"
    index_path = style_root / "index.json"
    profile_path = style_root / "profile.json"

    existing_index = _read_json(index_path, {})
    existing_profile = load_human_style_profile(output_dir, existing_index)
    same_snapshot = bool(
        isinstance(existing_index, dict)
        and existing_index.get("version") == STYLE_LIBRARY_VERSION
        and existing_index.get("source_digest") == source_digest
        and existing_index.get("pipeline_revision") == STYLE_PIPELINE_REVISION
        and int(existing_index.get("chapter_count") or 0) == target
    )
    reuse_existing_samples = False
    if force or not same_snapshot:
        # Never delete the live library before a replacement is complete. Each build is immutable
        # until its index is atomically published; interrupted/concurrent readers keep the old snapshot.
        generation = style_root / "generations" / uuid.uuid4().hex
        sample_dir = generation / "samples"
        profile_path = generation / "profile.json"
    else:
        status = human_style_library_status(output_dir)
        if status.get("ready") and status.get("advanced_profile_ready"):
            print(
                f"  人工文笔库已存在：{status['sample_count']} 个连续样本，"
                f"覆盖 {status['chapter_count']} 章；高级作者画像已完成。"
            )
            return status
        if status.get("ready"):
            reuse_existing_samples = True
            profile_path = safe_style_sample_path(output_dir, existing_index.get("profile_path") or f"{STYLE_DIR_NAME}/profile.json") or profile_path
            print(
                f"  复用现有 {status['sample_count']} 个连续人工样本；"
                "本次仅补齐统计画像/高级作者画像，不重建样本。"
            )

    if isinstance(cards, list):
        card_map = {int(item.get("chapter") or 0): item for item in cards if isinstance(item, dict)}
    elif isinstance(cards, dict):
        card_map = {int(key): value for key, value in cards.items() if isinstance(value, dict)}
    else:
        card_map = _load_cards(output_dir, target)

    sample_dir.mkdir(parents=True, exist_ok=True)
    index_items: list[dict[str, Any]] = []
    all_bodies: list[str] = []
    for number, chapter in enumerate(chapters, start=1):
        _check_cancel(cancel_event)
        title = str(chapter.get("title") or f"第{number}章")
        content = str(chapter.get("content") or "")
        body = _strip_chapter_heading(content, title)
        if body:
            all_bodies.append(body)
        if reuse_existing_samples:
            continue
        chapter_semantic = _scene_analysis_text(card_map.get(number), title)
        scenes = split_scenes_verbatim(content, title)
        for scene in scenes:
            ordinal = int(scene.get("scene") or 0)
            position = str(scene.get("position") or "middle")
            sample_id = f"ch{number:04d}_sc{ordinal:03d}"
            filename = f"{sample_id}.txt"
            scene_text = str(scene.get("text") or "")
            _write_text(sample_dir / filename, scene_text)
            metadata = infer_scene_metadata(
                scene_text,
                position=position,
                semantic_hint="",
            )
            legacy_types = infer_scene_types(
                scene_text, position=position,
            )
            scene_types = list(dict.fromkeys([metadata.get("scene_type"), *legacy_types]))
            scene_types = [value for value in scene_types if value]
            index_items.append({
                "id": sample_id,
                "chapter": number,
                "scene": ordinal,
                "position": position,
                "scene_type": metadata.get("scene_type"),
                "scene_types": scene_types,
                "emotion": metadata.get("emotion"),
                "conflict_level": metadata.get("conflict_level"),
                "plot_function": metadata.get("plot_function"),
                "information_function": metadata.get("information_function"),
                "pacing": metadata.get("pacing"),
                "ending_type": metadata.get("ending_type"),
                "style_tags": metadata.get("style_tags") or [],
                "analysis_confidence": metadata.get("analysis_confidence"),
                "analysis_uncertainty": metadata.get("analysis_uncertainty"),
                "char_count": int(scene.get("char_count") or len(re.sub(r"\s+", "", scene_text))),
                "path": (sample_dir / filename).relative_to(output_dir).as_posix(),
                "semantic_text": scene_text[:2800],
                "source_start": scene.get("source_start"), "source_end": scene.get("source_end"),
                "text_sha256": hashlib.sha256(scene_text.encode("utf-8")).hexdigest(),
                "analysis_source": metadata.get("analysis_source", "heuristic"),
            })

    if reuse_existing_samples:
        index_items = [item for item in existing_index.get("samples", []) if isinstance(item, dict)]
    else:
        index_payload = {
            "version": STYLE_LIBRARY_VERSION,
            "source_digest": source_digest,
            "chapter_count": target,
            "sample_count": len(index_items),
            "scene_example_count": len(index_items),
            "engine": "scene_style_engine_v2",
            "pipeline_revision": STYLE_PIPELINE_REVISION,
            "profile_path": profile_path.relative_to(output_dir).as_posix(),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "samples": index_items,
        }
        if not index_items:
            raise ValueError("没有提取到有效场景；原人工库保持不变。")
        print(f"\n--- Style Engine v2：已准备 {len(index_items)} 个连续原文 Scene 案例（覆盖 {target} 章）---")

    metrics = _style_metrics("\n\n".join(all_bodies))
    generated_at = datetime.now().isoformat(timespec="seconds")
    profile_payload = {
        "version": STYLE_LIBRARY_VERSION,
        "source_digest": source_digest,
        "chapter_count": target,
        "sample_count": len(index_items),
        "generated_at": generated_at,
        "metrics": metrics,
        "profile": {},
        "profile_mode": "metrics_only",
        "profile_error": "",
    }
    if not force and existing_profile.get("source_digest") == source_digest and _profile_has_content(existing_profile.get("profile")):
        profile_payload["profile"] = existing_profile["profile"]
        profile_payload["profile_mode"] = "qualitative"
    # 先把可用的统计画像落盘。即使后续高级画像调用被中断，连续人工样本库仍然可用。
    _check_cancel(cancel_event)
    if _file_digest(txt_path) != source_digest:
        raise RuntimeError("提炼期间参考小说发生变化，未发布新人工库，请重试。")
    _write_json(profile_path, profile_payload)
    if not reuse_existing_samples:
        _write_json(sample_dir.parent / "index.json", index_payload)
        _write_json(index_path, index_payload)
    # Compatibility copy for users/tools opening the old location; readers use index.profile_path.
    _write_json(style_root / "profile.json", profile_payload)
    representative = _representative_samples(index_items, output_dir)
    if llm is None:
        status = human_style_library_status(output_dir)
        print(
            f"  -> 人工文笔库可用：{status['sample_count']} 个连续样本 + 统计画像；"
            + ("保留同一参考小说已有的高级作者画像。" if status.get("advanced_profile_ready") else "未配置画像模型，高级作者画像尚未提炼。")
        )
        return status
    print(f"  正在从 {len(representative)} 个跨场景连续样本提炼高级作者文笔画像...")
    try:
        profile = _generate_profile(llm, representative, metrics, cancel_event=cancel_event)
    except LLMCallCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - advanced profile is optional enhancement
        profile_payload["profile_error"] = type(exc).__name__
        _write_json(profile_path, profile_payload)
        status = human_style_library_status(output_dir)
        print(
            f"  -> 高级作者画像提炼失败，但人工文笔库仍可用：{type(exc).__name__}\n"
            f"     将使用 {status['sample_count']} 个连续人工样本 + 全书统计画像生成正文。"
        )
        return status
    profile_payload["profile"] = profile
    profile_payload["profile_mode"] = "qualitative"
    profile_payload["profile_error"] = ""
    _write_json(profile_path, profile_payload)
    _write_json(style_root / "profile.json", profile_payload)
    status = human_style_library_status(output_dir)
    print(f"  -> 人工文笔库完成：{status['sample_count']} 个连续样本 + 高级作者文笔画像。")
    return status


def _target_scene_types(query: str) -> list[str]:
    tags = infer_scene_types(query)
    if tags == ["general"]:
        tags = []
    lower = (query or "").lower()
    if any(token in lower for token in ("章末", "结尾", "钩子", "收束")) and "ending" not in tags:
        tags.append("ending")
    if any(token in lower for token in ("开篇", "开场", "章初", "开局")) and "opening" not in tags:
        tags.append("opening")
    if re.search(r"第\s*0*1\s*章", query or "") and "opening" not in tags:
        tags.append("opening")
    return tags


def _retrieval_score(item: dict[str, Any], target_tags: list[str], query: str) -> float:
    scene_types = item.get("scene_types") if isinstance(item.get("scene_types"), list) else []
    semantic = str(item.get("semantic_text") or "")
    inferred_types = infer_scene_types(semantic, position=str(item.get("position") or ""))
    effective_types = list(dict.fromkeys([*scene_types, *inferred_types]))
    overlap = len(set(effective_types) & set(target_tags))
    score = overlap * 12.0
    for tag in target_tags:
        spec = SCENE_TYPES.get(tag) or {}
        for keyword in spec.get("keywords", ()):
            if keyword in query and keyword in semantic:
                score += 1.5
    position = str(item.get("position") or "")
    if "opening" in target_tags and position in {"opening", "full"}:
        score += 8.0
    if "ending" in target_tags and position in {"ending", "full"}:
        score += 8.0
    if position == "middle" and not ({"opening", "ending"} & set(target_tags)):
        score += 0.4
    score += min(0.8, float(item.get("char_count") or 0) / SAMPLE_TARGET_CHARS * 0.4)
    return score


def retrieve_human_style_context(
    reference_dir: str | Path,
    query: Any,
    *,
    max_samples: int = 3,
    max_chars: int = 6800,
) -> dict[str, Any]:
    root = Path(reference_dir) / STYLE_DIR_NAME
    index = _read_json(root / "index.json", {})
    profile_payload = load_human_style_profile(reference_dir, index)
    if not human_style_library_status(reference_dir)["ready"]:
        return {"ready": False, "target_scene_types": [], "samples": [], "profile": {}}
    items = index.get("samples") if isinstance(index, dict) else []
    if not isinstance(items, list) or not items:
        return {"ready": False, "target_scene_types": [], "samples": [], "profile": {}}
    normalized_query = normalize_scene_query(query)
    target_tags = _target_scene_types(normalized_query.get("text") or "")
    selected = retrieve_diverse_style_examples(
        reference_dir,
        [item for item in items if isinstance(item, dict)],
        query,
        max_samples=max_samples,
        max_chars=max_chars,
    )
    for item in selected:
        semantic = str(item.get("semantic_text") or "")
        item["scene_types"] = list(dict.fromkeys([
            *((item.get("scene_types") or []) if isinstance(item.get("scene_types"), list) else []),
            *infer_scene_types(semantic, position=str(item.get("position") or "")),
        ]))
    return {
        "ready": bool(selected),
        "target_scene_types": target_tags,
        "target_scene_query": normalized_query,
        "samples": selected,
        "profile": profile_payload.get("profile") if isinstance(profile_payload, dict) else {},
        "metrics": profile_payload.get("metrics") if isinstance(profile_payload, dict) else {},
    }


def format_human_style_context(context: dict[str, Any], *, include_samples: bool = True) -> str:
    if not isinstance(context, dict) or not context.get("ready"):
        return ""
    profile = context.get("profile") if isinstance(context.get("profile"), dict) else {}
    metrics = context.get("metrics") if isinstance(context.get("metrics"), dict) else {}
    target_tags = context.get("target_scene_types") or []
    target_labels = [SCENE_TYPES.get(tag, {}).get("label", tag) for tag in target_tags]
    profile_lines = []
    # Structured Author Bible was previously saved but many of its fields never reached Writer.
    for section in ("narrative", "language", "dialogue", "emotion", "description", "plot"):
        fields = profile.get(section)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if isinstance(value, (str, int, float)) and str(value).strip():
                    profile_lines.append(f"- {section}.{key}: {str(value)[:500]}")
    for key in PROFILE_KEYS:
        value = str(profile.get(key) or "").strip()
        if value:
            profile_lines.append(f"- {key}: {value}")
    rules = profile.get("reproduction_rules") if isinstance(profile.get("reproduction_rules"), list) else []
    if rules:
        profile_lines.append("- 本作者稳定写法：" + "；".join(str(item) for item in rules[:12]))
    if metrics:
        profile_lines.append(
            "- 全书统计画像："
            f"平均句长 {float(metrics.get('sentence_avg') or 0):.1f}；"
            f"句长波动 {float(metrics.get('sentence_std') or 0):.1f}；"
            f"平均段长 {float(metrics.get('paragraph_avg') or 0):.1f}；"
            f"对白占比 {float(metrics.get('dialogue_ratio') or 0):.1%}。"
        )
    playbooks = profile.get("scene_playbooks") if isinstance(profile.get("scene_playbooks"), dict) else {}
    relevant_playbooks = []
    for tag in target_tags:
        for key in (tag, SCENE_TYPES.get(tag, {}).get("label", "")):
            if key and playbooks.get(key):
                relevant_playbooks.append(f"{SCENE_TYPES.get(tag, {}).get('label', tag)}：{playbooks[key]}")
                break
    profile_lines = profile_lines[:50]
    # Bound guidance, never scene facts or the chapter outline.
    profile_lines = [line[:520] for line in profile_lines]
    while sum(map(len, profile_lines)) > 8500:
        profile_lines.pop()
    sample_blocks = []
    for index, sample in enumerate((context.get("samples") or []) if include_samples else [], start=1):
        labels = [SCENE_TYPES.get(tag, {}).get("label", tag) for tag in sample.get("scene_types") or []]
        sample_blocks.append(
            f"【连续人工样本 / Scene 案例{index}｜参考章{sample.get('chapter')} 场景{sample.get('scene') or '-'}｜"
            f"{' / '.join(labels) or sample.get('scene_type') or '通用叙事'}｜"
            f"{sample.get('emotion','')} / {sample.get('conflict_level','')} / {sample.get('information_function','')}】\n"
            f"{sample.get('text', '')}"
        )
    return (
        "=== Style Engine v2 人工文笔库 / 人工场景库（正文风格最高优先级；只学习写法，禁止复制内容）===\n"
        "这些案例全部来自参考小说中的连续人工 Scene 原文。当前章纲是剧情唯一蓝图；前序生成正文只负责事实承接，不是风格范本。\n"
        "严禁复制样本中的人物、专名、事件、独特比喻、标志性措辞或原句。只迁移叙述距离、句段节奏、对白承接、动作组织、信息释放、情绪表达、转场和章尾机制。\n"
        f"本章识别场景：{' / '.join(target_labels) if target_labels else '通用叙事'}\n\n"
        "【作者文笔画像】\n"
        + ("\n".join(profile_lines) or "（仅使用下方连续人工样本作为隐式文笔锚点。）")
        + ("\n【本章场景写法提示】\n" + "\n".join(relevant_playbooks) if relevant_playbooks else "")
        + "\n\n"
        + "\n\n".join(sample_blocks)
        + "\n\n执行要求：先在内部吸收上述写作机制，再用当前新书的人物与事件独立完成正文；不要解释你在模仿什么。"
    )
