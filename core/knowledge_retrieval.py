"""面向长篇生成的目标世界知识检索与审计快照。"""

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

from core.world_knowledge import WORLD_SECTIONS, world_knowledge_status


_CORE_SECTIONS = {"世界观", "力量体系", "技能体系"}
_RULE_MARKERS = ("规则", "限制", "条件", "代价", "禁止", "不得", "只能", "必须", "上限", "下限", "层级", "境界")
_STOP_TOKENS = {
    "一个", "当前", "本章", "故事", "情节", "生成", "内容", "进行", "需要", "以及",
    "人物", "章节", "阶段", "这个", "没有", "可以", "根据", "相关", "目标", "世界",
}
_SECTION_HINTS = {
    "世界观": ("世界", "规则", "历史", "时代", "地域", "地点", "环境"),
    "力量体系": ("境界", "等级", "力量", "修炼", "实力", "晋升", "突破"),
    "关键人物": ("人物", "角色", "身份", "关系", "主角", "配角"),
    "势力描述": ("势力", "组织", "门派", "宗门", "家族", "阵营", "联盟"),
    "故事主线": ("主线", "事件", "阴谋", "任务", "目标", "冲突"),
    "关键物品": ("物品", "道具", "法宝", "资源", "武器", "丹药"),
    "技能体系": ("技能", "功法", "法术", "能力", "招式", "神通"),
}


def _read_text(path):
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    return ""


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _safe_name(value):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "retrieval")).strip("_") or "retrieval"


def _section_files(ws):
    final_dir = os.path.join(ws.file_system, "world_knowledge", "worlds", "_final")
    if not os.path.isdir(final_dir):
        return []
    files = []
    for section, _ in WORLD_SECTIONS:
        path = os.path.join(final_dir, f"{section}.md")
        if os.path.exists(path):
            files.append((section, path))
    return files


def _split_chunks(section, path, text, target_chars=700):
    heading = section
    buffer = []
    chunks = []

    def flush():
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body or body in {"无", f"# {section}"}:
            return
        chunks.append({
            "section": section,
            "heading": heading,
            "source": path,
            "text": body,
        })

    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if not block:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", block)
        if match:
            flush()
            heading = match.group(1).strip()
            continue
        if sum(len(item) for item in buffer) + len(block) > target_chars and buffer:
            flush()
        if len(block) <= target_chars:
            buffer.append(block)
            continue
        for start in range(0, len(block), target_chars):
            part = block[start:start + target_chars].strip()
            if part:
                buffer.append(part)
                flush()
    flush()
    return chunks


def _tokens(text):
    result = []
    lowered = str(text or "").lower()
    result.extend(re.findall(r"[a-z0-9_]{2,}", lowered))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        if len(sequence) <= 6:
            result.append(sequence)
        for width in (2, 3, 4):
            result.extend(sequence[index:index + width] for index in range(len(sequence) - width + 1))
    return [token for token in result if token not in _STOP_TOKENS]


def _load_aliases(ws, chunks):
    aliases = defaultdict(set)
    path = os.path.join(ws.file_system, "world_knowledge", "aliases.json")
    if os.path.exists(path):
        try:
            data = json.loads(_read_text(path) or "{}")
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            for key, value in data.items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if str(item).strip():
                        aliases[str(key).strip()].add(str(item).strip())
                        aliases[str(item).strip()].add(str(key).strip())

    for chunk in chunks:
        for canonical, raw_aliases in re.findall(
            r"([\u4e00-\u9fffA-Za-z0-9·]{2,16})[（(]([^）)\n]{1,30})[）)]",
            chunk["text"],
        ):
            for alias in re.split(r"[、,/，或]", raw_aliases):
                alias = alias.strip()
                if 1 < len(alias) <= 16:
                    aliases[canonical].add(alias)
                    aliases[alias].add(canonical)
    return aliases


def _expand_query(query, aliases):
    additions = []
    for name, related in aliases.items():
        if name and name in query:
            additions.extend(sorted(related))
    return query + ("\n别名与关联实体：" + "、".join(dict.fromkeys(additions)) if additions else ""), additions


def _rank_chunks(query, chunks, aliases):
    expanded_query, expanded_aliases = _expand_query(query, aliases)
    query_counts = Counter(_tokens(expanded_query))
    document_tokens = [Counter(_tokens(f"{item['heading']} {item['text']}")) for item in chunks]
    document_frequency = Counter()
    for counts in document_tokens:
        document_frequency.update(counts.keys())
    average_length = sum(sum(item.values()) for item in document_tokens) / max(1, len(document_tokens))
    ranked = []
    for index, (chunk, counts) in enumerate(zip(chunks, document_tokens)):
        length = sum(counts.values()) or 1
        score = 0.0
        matched = []
        for token, query_frequency in query_counts.items():
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            inverse_frequency = math.log(1 + (len(chunks) - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            normalized = frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / max(1, average_length)))
            score += inverse_frequency * normalized * min(query_frequency, 2)
            if len(token) >= 3:
                matched.append(token)
        for section, hints in _SECTION_HINTS.items():
            if chunk["section"] == section and any(hint in query for hint in hints):
                score += 1.25
        if chunk["heading"] and chunk["heading"] in query:
            score += 4.0
        ranked.append((score, index, matched))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked, expanded_aliases


def _core_rules(chunks, max_chars):
    preferred = [
        item for item in chunks
        if item["section"] in _CORE_SECTIONS and any(marker in item["text"] for marker in _RULE_MARKERS)
    ]
    if not preferred:
        preferred = [item for item in chunks if item["section"] in _CORE_SECTIONS]
    selected = []
    used = 0
    for item in preferred:
        rendered = f"【{item['section']} / {item['heading']}】\n{item['text']}"
        if selected and used + len(rendered) > max_chars:
            break
        selected.append(rendered[:max_chars - used])
        used += len(rendered)
        if used >= max_chars:
            break
    return "\n\n".join(selected)


def retrieve_world_knowledge(ws, query, purpose, volume=1, trace_key=None,
                             max_chars=7500, max_hits=8):
    """检索少量事实约束；无可用知识库时返回空上下文。"""
    status = world_knowledge_status(ws)
    if not status.get("enabled") or not status.get("ready"):
        return {"context": "", "hits": [], "snapshot_path": None, "fallback": False}

    chunks = []
    for section, path in _section_files(ws):
        chunks.extend(_split_chunks(section, path, _read_text(path)))
    if not chunks:
        return {"context": "", "hits": [], "snapshot_path": None, "fallback": False}

    aliases = _load_aliases(ws, chunks)
    ranked, expanded_aliases = _rank_chunks(str(query or ""), chunks, aliases)
    core_budget = min(2600, max_chars // 3)
    core_text = _core_rules(chunks, core_budget)
    dynamic_budget = max(1200, max_chars - len(core_text) - 500)
    selected = []
    used = 0
    fallback = not ranked or ranked[0][0] < 1.0
    candidates = ranked[:max_hits]
    if fallback:
        hinted_sections = [
            section for section, hints in _SECTION_HINTS.items()
            if any(hint in str(query or "") for hint in hints)
        ] or [section for section, _ in WORLD_SECTIONS]
        candidate_indexes = [
            index for index, chunk in enumerate(chunks) if chunk["section"] in hinted_sections
        ]
        candidates = [(0.0, index, []) for index in candidate_indexes[:max_hits]]

    hits = []
    for score, index, matched in candidates:
        item = chunks[index]
        rendered = f"【{item['section']} / {item['heading']}】\n{item['text']}"
        if selected and used + len(rendered) > dynamic_budget:
            continue
        rendered = rendered[:dynamic_budget - used]
        if not rendered:
            break
        selected.append(rendered)
        used += len(rendered)
        hits.append({
            "section": item["section"],
            "heading": item["heading"],
            "source": os.path.relpath(item["source"], ws.file_system).replace("\\", "/"),
            "score": round(score, 4),
            "matched_terms": list(dict.fromkeys(matched))[:12],
            "excerpt": item["text"][:240],
        })
        if used >= dynamic_budget:
            break

    selected_text = "\n\n".join(selected) or "（未命中明确条目；请遵守上方核心规则。）"
    context = (
        "以下内容只用于约束事实和检查冲突，不要求在成文中逐条解释。"
        "仅在剧情自然涉及相关信息时体现；禁止为了覆盖知识点而增加旁白、百科说明或重复介绍。\n\n"
        f"【固定核心规则】\n{core_text or '（无额外核心规则）'}\n\n"
        f"【本轮相关事实】\n{selected_text}"
    )

    trace_name = _safe_name(trace_key or purpose)
    snapshot_path = os.path.join(
        ws.file_system, "knowledge_retrieval", f"vol_{int(volume):02d}", f"{trace_name}.json",
    )
    _write_json(snapshot_path, {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": purpose,
        "query": str(query or "")[:30000],
        "expanded_aliases": expanded_aliases,
        "fallback": fallback,
        "hits": hits,
        "injected_context": context,
    })
    return {
        "context": context,
        "hits": hits,
        "snapshot_path": snapshot_path,
        "fallback": fallback,
        "expanded_aliases": expanded_aliases,
    }


def record_consistency_audit(snapshot_path, audit):
    if not snapshot_path or not os.path.exists(snapshot_path):
        return
    try:
        payload = json.loads(_read_text(snapshot_path) or "{}")
    except json.JSONDecodeError:
        payload = {}
    payload["consistency_audit"] = audit
    _write_json(snapshot_path, payload)
