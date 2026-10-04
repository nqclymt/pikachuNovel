"""One chapter's resumable writing, editing and acceptance stages."""
import json
import os
import hashlib
from pathlib import Path

from core.chapter_checkpoints import (
    chapter_format_report, digest, has_stage, load_checkpoint,
    require_previous_draft, save_checkpoint,
)
from core.llm_provider import LLMCallCancelled
from core.cli_scheduler import check_cancelled
from core.prompt_loader import PromptLoader
from core.story_memory import load_story_memory_context, previous_chapter_paths
from core.text_utils import normalize_text


class ChapterReviewRequired(RuntimeError):
    pass


def _library_digest(ws, index):
    from training.style_engine_v2 import safe_style_sample_path
    references = [index.get("profile_path") or "style_library/profile.json"]
    references.extend(item.get("path") for item in index.get("samples", []) if isinstance(item, dict))
    values = {}
    for value in references:
        path = safe_style_sample_path(ws.reference, value)
        values[str(value)] = hashlib.sha256(path.read_bytes()).hexdigest() if path else None
    return digest(values)


def _source_snapshot(ws):
    """Detect concurrent manual edits before publishing a generated candidate."""
    fs = Path(ws.file_system)
    paths = []
    for directory in ("chapter_outlines", "story_arcs", "chapters", "system_panels",
                      "writing", "world_knowledge", "mechanics"):
        paths.extend(path for path in (fs / directory).rglob("*")
                     if path.is_file() and path.suffix in {".md", ".json"}
                     and "versions" not in path.parts
                     and not path.name.startswith(("conversation_", "job_")))
    ref = Path(ws.reference)
    paths.extend(path for path in (ref / "style_library").glob("*.json") if path.is_file())
    index_path = ref / "style_library" / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        index = {}
    return digest({"files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted(set(paths))},
                   "library": _library_digest(ws, index)})


def process_chapter(b, ws, *, volume, chapter, task_mode, llm, editor_llm,
                    humanize, humanize_strength, writing_rules, writing_instruction,
                    regenerate_existing, refinement_mode, resume_checkpoint,
                    generation_id,
                    reference_profile, reference_text, style_library,
                    volume_first_chapter, volume_chapter_count,
                    progress_callback=None, pause_event=None, stop_event=None,
                    cancel_event=None, completed=0, total=1):
    """Return the published text, or None on a requested stop. Never auto-rewrite."""
    def stage_call(label, operation):
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            if pause_event is not None:
                pause_event.wait()
            if stop_event is not None and stop_event.is_set():
                return None
            try:
                check_cancelled(cancel_event)
                if progress_callback:
                    progress_callback(label, completed, total, f"第{chapter}章：{label}")
                return operation()
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    return None
                if pause_event is None:
                    raise
                if progress_callback:
                    progress_callback("paused", completed, total, f"第{chapter}章已暂停；已完成阶段已保存")
                pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()

    require_previous_draft(ws, volume, chapter)
    outline_path = Path(ws.file_system) / "chapter_outlines" / f"vol_{volume:02d}" / f"chapter_{chapter:03d}.md"
    outline = b._read_file(str(outline_path))
    if not outline:
        raise RuntimeError(f"第{chapter}章缺少章纲，未继续写作。")
    outline = b.re.sub(r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', outline).strip()
    arc = b._find_story_arc_for_chapter(ws, volume, chapter) or ""
    out_file = Path(ws.file_system) / "chapters" / f"vol_{volume:02d}" / f"{chapter:03d}_第{chapter}章.md"
    existing = b._read_file(str(out_file)) or ""
    initial_snapshot = _source_snapshot(ws)
    history = "\n\n".join(b._read_file(str(path)) or "" for path in previous_chapter_paths(ws, volume, chapter))
    history = history or "（尚无已完成的前序正文，以本章章纲为准。）"
    memory = stage_call("memory", lambda: load_story_memory_context(
        ws, volume, chapter,
        lambda prompt: b._generate_with_cancel(llm, prompt, cancel_event, is_json=True, temperature=0.1),
    ))
    if memory is None:
        return None
    recent_context = history + ("\n\n=== 已发生事实与未回收伏笔 ===\n" + memory if memory else "")
    panel = ""
    if b.system_panel_status(ws)["enabled"]:
        previous = b._previous_system_panel(ws, volume, chapter)
        current = b._read_json_file(b._system_panel_chapter_path(ws, volume, chapter)) or {}
        panel = ("=== 系统面板：上一章实际状态与本章规划目标（不能视为已发生） ===\n"
                 + json.dumps({"previous": previous, "planned_end": current}, ensure_ascii=False) + "\n\n")
    knowledge = b.retrieve_world_knowledge(ws, f"{arc}\n{outline}\n第{chapter}章", "正文生成",
                                          volume=volume, trace_key=f"chapter_{chapter:03d}")
    style_index = b._read_json_file(os.path.join(ws.reference, "style_library", "index.json")) or {}
    reference_anchor = b._aligned_reference_chapter_context(ws, volume,
        chapter - volume_first_chapter + 1, volume_chapter_count)
    template_files = [Path(PromptLoader._base_dir) / name / "prompt.txt" for name in (
        "adaptive_drafting", "style_scene_plan", "prose_review", "prose_local_edit", "prose_edit_verify",
        "knowledge_consistency_audit", "chapter_acceptance")]
    # No secrets are persisted. Provider identity is part of the reproducible input hash.
    providers = [{key: getattr(provider, key, "") for key in
                  ("backend", "model", "base_url", "wire_api", "cli_agent", "cli_effort")}
                 for provider in (llm, editor_llm)]
    inputs = dict(outline=outline, arc=arc, history=recent_context, rules=writing_rules,
                  panel=panel, knowledge=knowledge.get("context"), style_index=style_index,
                  reference=reference_text, providers=providers, humanize=humanize,
                  library_digest=_library_digest(ws, style_index), reference_anchor=reference_anchor,
                  pipeline_revision=1, templates=digest([path.read_text(encoding="utf-8") for path in template_files]),
                  strength=humanize_strength, mode=task_mode, refinement_mode=refinement_mode,
                  current=existing if task_mode == "humanize_existing" or
                  (regenerate_existing and refinement_mode == "revise") else "")
    input_hash = digest(inputs)
    saved = load_checkpoint(ws, volume, chapter) if resume_checkpoint else {}
    state = saved if (saved.get("input_sha256") == input_hash
                      and saved.get("generation_id") == generation_id) else {}
    if saved and not state:
        print(f"  -> 第{chapter}章输入已变化，旧检查点不复用。")
    if state.get("stage") == "published":
        # A deleted/manually replaced final file must never be silently resurrected.
        state = {}
    if state and (not isinstance(state.get("scene_plan"), list)
                  or not all(isinstance(state.get(key), str) for key in ("anchor", "scene_style"))):
        state = {}
    if has_stage(state, "drafted") and not str(state.get("text") or "").strip():
        state["stage"] = "planned"
    if has_stage(state, "audited"):
        cached_audit = state.get("knowledge_audit")
        if (not isinstance(cached_audit, dict)
                or cached_audit.get("status") not in {"passed", "skipped", "conflict"}
                or state.get("knowledge_sha256") != hashlib.sha256(state["text"].encode("utf-8")).hexdigest()):
            state["stage"] = "edited"
    if has_stage(state, "validated"):
        accepted = state.get("acceptance") or {}
        actual_hash = hashlib.sha256(state["text"].encode("utf-8")).hexdigest()
        if (accepted.get("status") != "passed" or accepted.get("chapter_sha256") != actual_hash
                or chapter_format_report(state["text"], chapter)["errors"]):
            state["stage"] = "edited"
            state.pop("acceptance", None)

    candidate = Path(ws.file_system) / "drafts" / f"vol_{volume:02d}" / "candidates" / f"chapter_{chapter:03d}.md"
    if state and has_stage(state, "audited") and candidate.is_file():
        reviewed_text = b._read_file(str(candidate)) or ""
        if reviewed_text and reviewed_text != str(state.get("text") or "").strip():
            # An author can fix a retained candidate; re-audit it instead of paying
            # for another draft or silently restoring the rejected text.
            state.update(text=reviewed_text, stage="edited")
            for key in ("knowledge_audit", "acceptance", "format_report"):
                state.pop(key, None)
            save_checkpoint(ws, volume, chapter, state)

    def save(stage, **values):
        state.update(values, stage=stage, input_sha256=input_hash, generation_id=generation_id)
        save_checkpoint(ws, volume, chapter, state)

    if not has_stage(state, "planned"):
        def plan():
            scenes = b.plan_chapter_scenes(llm, outline, story_arc=arc,
                                          recent_context=recent_context,
                                          instruction=writing_rules, cancel_event=cancel_event)
            trace, scene_style = [], ""
            if style_library.get("ready"):
                profile = b.load_human_style_profile(ws.reference, style_index)
                anchor = b.format_human_style_context({"ready": True,
                    "profile": profile.get("profile") or {}, "metrics": profile.get("metrics") or {}}, include_samples=False)
                scene_style = b.build_scene_style_context(ws.reference, style_index.get("samples") or [],
                    scenes, samples_per_scene=4, chars_per_scene=5000, max_total_chars=18000,
                    trace=trace, cancel_event=cancel_event)
            else:
                anchor = b._human_style_anchor_for_query(ws, f"{outline}\n{writing_instruction}", reference_anchor)
            return dict(scene_plan=scenes, scene_style=scene_style, anchor=anchor, retrieval=trace)
        planned = stage_call("planning", plan)
        if planned is None:
            return None
        save("planned", **planned)
    scenes, anchor, scene_style = state["scene_plan"], state["anchor"], state["scene_style"]
    context = (
        f"=== 统一写作要求（全部阶段共享） ===\n{writing_rules}\n\n"
        "人工参考只提供表达机制，不能覆盖本轮要求、用户规范或改变已发生事实。\n\n"
        f"=== 参考风格基线 ===\n{reference_text}\n{anchor}\n\n"
        f"=== 当前章场景规划 ===\n{json.dumps(scenes, ensure_ascii=False)}\n\n"
        f"=== 场景参考 ===\n{scene_style}\n\n"
        f"=== 世界事实约束 ===\n{knowledge.get('context') or '以章纲及已发生事实为准。'}\n\n"
        f"=== 当前故事情节单元 ===\n{arc}\n\n"
        f"=== 前序正文及动态记忆 ===\n{recent_context}\n\n" + panel
        + (f"=== 当前正文（基于此定向调整） ===\n{existing}\n\n"
           if regenerate_existing and refinement_mode == "revise" else "")
        + f"=== 当前章纲（背景无需逐条写出） ===\n{outline}\n\n只输出本章标题及正文；完成必写事件，不照抄参考原句。"
    )
    prompt = PromptLoader.load("adaptive_drafting", context=context, start_chapter=chapter,
                               end_chapter=chapter, chapter_count=1)
    if len(prompt) > 60000:
        raise ValueError(f"第{chapter}章输入{len(prompt)}字符，超过60000保护上限；未截断正文或记忆。")
    trace_path = Path(ws.file_system) / "drafts" / f"vol_{volume:02d}" / "style_traces" / f"chapter_{chapter:03d}_{b.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    b._write_generated_chapter(str(trace_path), json.dumps({
        "chapter": chapter, "scene_plan": scenes, "retrieval": state.get("retrieval"),
        "prompt_chars": len(prompt), "prompt_sha256": digest(prompt),
        "input_sha256": input_hash, "resumed_stage": state["stage"],
        "planner_sources": list(dict.fromkeys(scene.get("planner_source") for scene in scenes)),
        "style_context_chars": len(scene_style),
    }, ensure_ascii=False, indent=2))
    if not has_stage(state, "drafted"):
        text = existing if task_mode == "humanize_existing" else stage_call("drafting", lambda:
            normalize_text(b._generate_with_cancel(llm, prompt, cancel_event)))
        if text is None:
            return None
        if not text.strip():
            raise RuntimeError(f"第{chapter}章返回空正文，未覆盖已有内容。")
        b._backup_raw_chapter(ws, volume, chapter, text)
        save("drafted", text=text)
    if not has_stage(state, "edited"):
        if humanize:
            edited = stage_call("editing", lambda: b._humanize_chapter_text(
                editor_llm, ws, volume, chapter, state["text"], cancel_event=cancel_event,
                strength=humanize_strength, style_profile=reference_profile,
                style_anchor=anchor + "\n\n" + scene_style, chapter_outline=outline,
                story_arc=arc, recent_context=recent_context, writing_requirements=writing_rules,
                writing_instruction=writing_instruction, return_report=True,
            ))
            if edited is None:
                return None
            text, report = edited
        else:
            text, report = state["text"], {}
        save("edited", text=text, editor_report=report)
    if not has_stage(state, "audited"):
        def audit_and_repair():
            text = state["text"]
            audit = b._audit_generated_chapter_knowledge(llm, chapter, text, outline, knowledge,
                cancel_event=cancel_event, writing_requirements=writing_rules)
            if audit.get("rewrite"):
                candidate, applied = b._repair_chapter_knowledge(text, audit)
                audit["applied_corrections"] = applied
                if applied:
                    verification = b._audit_generated_chapter_knowledge(llm, chapter, candidate,
                        outline, knowledge, cancel_event=cancel_event, writing_requirements=writing_rules)
                    audit["verification"] = verification
                    if verification.get("status") == "passed":
                        text = candidate
                        audit["status"] = "passed"
            return text, audit
        audited = stage_call("auditing", audit_and_repair)
        if audited is None:
            return None
        text, audit = audited
        b.record_consistency_audit(knowledge.get("snapshot_path"), audit)
        # Network/schema errors are retryable at this stage, not accepted as success.
        if audit.get("status") not in {"passed", "skipped", "conflict"}:
            raise RuntimeError(f"第{chapter}章事实审查未完成，初稿和精修检查点已保留。")
        save("audited", text=text, knowledge_audit=audit,
             knowledge_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    text = state["text"]
    b._write_generated_chapter(str(candidate), text)
    if state["knowledge_audit"].get("status") == "conflict":
        raise ChapterReviewRequired(f"第{chapter}章仍有事实冲突，后续章节已停止；候选稿：{candidate}")
    if not has_stage(state, "validated"):
        from core.chapter_acceptance import assess_chapter
        report = chapter_format_report(text, chapter)
        if report["errors"]:
            save_checkpoint(ws, volume, chapter, {**state, "format_report": report})
            raise ChapterReviewRequired(f"第{chapter}章格式验收未通过：{'；'.join(report['errors'])} 候选稿：{candidate}")
        acceptance = stage_call("validating", lambda: assess_chapter(
            lambda p: b._generate_with_cancel(llm, p, cancel_event, is_json=True, temperature=0.1),
            text, outline, scenes, writing_requirements=writing_rules,
            memory_context=recent_context + "\n\n" + panel,
            planning_issues=state.get("editor_report", {}).get("planning_issues", []),
        ))
        if acceptance is None:
            return None
        acceptance["format_report"] = report
        report_path = candidate.parent.parent / "acceptance" / f"chapter_{chapter:03d}.json"
        b._write_generated_chapter(str(report_path), json.dumps(acceptance, ensure_ascii=False, indent=2))
        if acceptance["status"] != "passed":
            save_checkpoint(ws, volume, chapter, {**state, "acceptance": acceptance})
            raise ChapterReviewRequired(f"第{chapter}章有待处理的剧情问题，后续章节已停止；候选稿：{candidate}；报告：{report_path}")
        save("validated", acceptance=acceptance)
    if stage_call("publishing", lambda: True) is None:
        return None
    if _source_snapshot(ws) != initial_snapshot:
        raise RuntimeError(f"第{chapter}章生成期间设定、章纲或正文被修改，候选稿已保留，请按最新输入继续。")
    if chapter in b._finalized_chapter_numbers(ws, "drafts", volume):
        return None
    if existing:
        backup = out_file.parent / "versions" / f"{out_file.name}_{b.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        b._write_generated_chapter(str(backup), existing)
    # The accepted text is also the exact input used by the next chapter's memory extraction.
    b._write_generated_chapter(str(out_file), text)
    save("published", output_sha256=digest(text.strip()))
    return text
