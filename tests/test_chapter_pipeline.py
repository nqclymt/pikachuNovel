import contextlib
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.chapter_checkpoints import (
    chapter_format_report, digest, guarded_knowledge_repairs, load_checkpoint, save_checkpoint,
)
from core.llm_provider import LLMExecutionBlocked
from training import adaptive_builder as drafting


PROSE = "第1章 借书\n\n客人提出借书，主人摇头，把书收回了柜中。\n\n客人空手离开。"


@pytest.fixture
def workspace(tmp_path):
    fs = tmp_path / "file_system"
    outlines = fs / "chapter_outlines" / "vol_01"
    outlines.mkdir(parents=True)
    (outlines / "chapter_001.md").write_text("客人借书被拒绝，空手离开。", encoding="utf-8")
    reference = tmp_path / "reference"
    reference.mkdir()
    return SimpleNamespace(file_system=str(fs), reference=str(reference),
                           reference_sample=str(reference / "missing.txt"),
                           reference_chapters=str(reference / "chapters"))


@contextlib.contextmanager
def pipeline(workspace, editor=None, audit=None):
    counts = {"writer": 0, "planner": 0, "editor": 0, "audit": 0}
    calls = []
    class Model:
        model = "fixture"
        def generate(self, prompt, **kwargs):
            counts["writer"] += 1
            calls.append(prompt)
            return PROSE
    model = Model()
    def plan(*args, **kwargs):
        counts["planner"] += 1
        return [{"required_events": ["借书被拒绝"], "planner_source": "llm"}]
    def edit(*args, **kwargs):
        counts["editor"] += 1
        calls.append(kwargs["writing_requirements"])
        if editor:
            editor(counts["editor"])
        return args[4], {"planning_issues": []}
    def check(*args, **kwargs):
        counts["audit"] += 1
        if audit:
            audit(counts["audit"])
        return {"status": "skipped", "rewrite": False}
    with contextlib.ExitStack() as stack:
        fixtures = {"_load_volume_outline_context": "设计", "_get_lite_llm": model,
                    "_get_humanize_llm": model, "_effective_style_profile": {},
                    "_reference_style_profile_text": "参考", "_finalized_chapter_numbers": set(),
                    "_find_story_arc_for_chapter": "借书遭拒", "_aligned_reference_chapter_context": {},
                    "_human_style_anchor_for_query": "", "system_panel_status": {"enabled": False},
                    "human_style_library_status": {"ready": False},
                    "retrieve_world_knowledge": {"context": "", "hits": [], "snapshot_path": None}}
        for name, value in fixtures.items():
            stack.enter_context(patch.object(drafting, name, return_value=value))
        stack.enter_context(patch.object(drafting, "plan_chapter_scenes", side_effect=plan))
        stack.enter_context(patch.object(drafting, "_humanize_chapter_text", side_effect=edit))
        stack.enter_context(patch.object(drafting, "_audit_generated_chapter_knowledge", side_effect=check))
        stack.enter_context(patch("core.chapter_acceptance.assess_chapter", side_effect=lambda *args, **kw: {
            "status": "passed", "chapter_sha256": hashlib.sha256(args[1].encode("utf-8")).hexdigest(),
            "blocking_issues": [], "warnings": []}))
        yield counts, calls


@pytest.mark.parametrize("blocked_stage", ["editor", "audit"])
def test_resume_reuses_paid_draft_and_preserves_instructions(workspace, blocked_stage):
    def block_first(count):
        if count == 1:
            raise LLMExecutionBlocked("额度用尽")
    with pipeline(workspace, **{blocked_stage: block_first}) as (counts, calls):
        with pytest.raises(LLMExecutionBlocked):
            drafting.gen_serial_chapters(workspace, writing_instruction="保留冷淡语气")
        state = load_checkpoint(workspace, 1, 1)
        assert state["stage"] == ("drafted" if blocked_stage == "editor" else "edited")
        assert not (Path(workspace.file_system) / "chapters/vol_01/001_第1章.md").exists()
        with patch.object(drafting, "_list_novel_story_arcs", return_value=[{"idx": 1, "start_ch": 1, "end_ch": 1}]):
            resume = drafting.chapter_draft_resume_status(workspace, 1, 1)
        assert resume["can_resume"] and resume["next_chapter"] == 1
        result = drafting.gen_serial_chapters(workspace, writing_instruction="保留冷淡语气", resume_checkpoint=True)
        assert len(result["artifacts"]) == 1
        assert counts["planner"] == counts["writer"] == 1
        assert counts["editor"] == (2 if blocked_stage == "editor" else 1)
        assert all("保留冷淡语气" in prompt for prompt in calls)
        assert load_checkpoint(workspace, 1, 1)["stage"] == "published"


def test_changed_instruction_invalidates_draft_checkpoint(workspace):
    def block(count):
        if count == 1:
            raise LLMExecutionBlocked("blocked")
    with pipeline(workspace, editor=block) as (counts, _):
        with pytest.raises(LLMExecutionBlocked):
            drafting.gen_serial_chapters(workspace, writing_instruction="旧要求")
        drafting.gen_serial_chapters(workspace, writing_instruction="新要求", resume_checkpoint=True)
        assert counts["writer"] == counts["planner"] == 2


def test_concurrent_outline_edit_prevents_publication(workspace):
    def edit(_):
        (Path(workspace.file_system) / "chapter_outlines/vol_01/chapter_001.md").write_text("已改成另一件事", encoding="utf-8")
    with pipeline(workspace, editor=edit):
        with pytest.raises(RuntimeError, match="生成期间"):
            drafting.gen_serial_chapters(workspace)
    assert (Path(workspace.file_system) / "drafts/vol_01/candidates/chapter_001.md").exists()
    assert not (Path(workspace.file_system) / "chapters/vol_01/001_第1章.md").exists()


def test_missing_previous_chapter_stops_before_model_call(workspace):
    outlines = Path(workspace.file_system) / "chapter_outlines/vol_01"
    (outlines / "chapter_002.md").write_text("次日来访。", encoding="utf-8")
    with pipeline(workspace) as (counts, _):
        with pytest.raises(RuntimeError, match="依赖第1章"):
            drafting.gen_serial_chapters(workspace, start_chapter=2)
        assert counts["writer"] == counts["planner"] == 0


def test_repair_rejects_ambiguous_unproven_and_oversized_changes():
    text = "第1章 秘密\n\n甲知道秘密。乙知道秘密。\n\n雨一直下着。"
    for original, replacement, fact in [("知道秘密", "不知情", "f1"),
                                        ("乙知道秘密", "乙不知情", "missing"),
                                        (text, "扩写" * 1000, "f1")]:
        audit = {"fact_catalog": {"f1": "乙不知情"}, "corrections": [
            {"original": original, "replacement": replacement, "fact_id": fact}]}
        result, applied = guarded_knowledge_repairs(text, audit)
        assert result == text and not applied and audit["rejected_corrections"]
    audit = {"fact_catalog": {"f1": "乙不知情"}, "corrections": [
        {"original": "乙知道秘密", "replacement": "乙仍不知情", "fact_id": "f1"}]}
    result, applied = guarded_knowledge_repairs(text, audit)
    assert "甲知道秘密" in result and "乙仍不知情" in result and len(applied) == 1


def test_format_length_is_warning_and_never_trims():
    text = "第2章 长章\n" + "雨" * 2800
    report = chapter_format_report(text, 2)
    assert not report["errors"] and report["warnings"]
    assert chapter_format_report(text, 1)["errors"]


def test_resume_does_not_skip_later_chapters_published_by_an_older_task(workspace):
    fs = Path(workspace.file_system)
    output = fs / "chapters/vol_01"
    output.mkdir(parents=True)
    for chapter, generation in ((1, "current-task"), (2, "old-task")):
        text = f"第{chapter}章 原稿\n\n原来的正文。"
        (output / f"{chapter:03d}_第{chapter}章.md").write_text(text, encoding="utf-8")
        (fs / f"chapter_outlines/vol_01/chapter_{chapter:03d}.md").write_text("事件。", encoding="utf-8")
        save_checkpoint(workspace, 1, chapter, {"stage": "published", "generation_id": generation,
                        "output_sha256": digest(text)})
    with pipeline(workspace), patch("training.chapter_pipeline.process_chapter", return_value=PROSE) as process:
        drafting.gen_serial_chapters(workspace, regenerate_existing=True, resume_checkpoint=True,
                                     generation_id="current-task")
    assert [call.kwargs["chapter"] for call in process.call_args_list] == [2]


def test_rejected_candidate_can_be_edited_and_revalidated_without_redrafting(workspace):
    from training.chapter_pipeline import ChapterReviewRequired
    with pipeline(workspace) as (counts, _):
        with patch("core.chapter_acceptance.assess_chapter", return_value={"status": "needs_review"}):
            with pytest.raises(ChapterReviewRequired):
                drafting.gen_serial_chapters(workspace)
        candidate = Path(workspace.file_system) / "drafts/vol_01/candidates/chapter_001.md"
        changed = PROSE.replace("主人摇头", "主人明确拒绝")
        candidate.write_text(changed, encoding="utf-8")
        drafting.gen_serial_chapters(workspace, resume_checkpoint=True)
    assert counts["writer"] == counts["planner"] == counts["editor"] == 1
    assert (Path(workspace.file_system) / "chapters/vol_01/001_第1章.md").read_text(encoding="utf-8").strip() == changed


def test_stop_after_acceptance_keeps_checkpoint_without_publishing(workspace):
    stop = threading.Event()
    def accept(*args, **kw):
        stop.set()
        return {"status": "passed", "chapter_sha256": hashlib.sha256(args[1].encode("utf-8")).hexdigest()}
    with pipeline(workspace) as (counts, _):
        with patch("core.chapter_acceptance.assess_chapter", side_effect=accept):
            result = drafting.gen_serial_chapters(workspace, stop_event=stop)
        assert result["stopped"] and not result["artifacts"]
        assert load_checkpoint(workspace, 1, 1)["stage"] == "validated"
        assert not (Path(workspace.file_system) / "chapters/vol_01/001_第1章.md").exists()
        stop.clear()
        with patch("core.chapter_acceptance.assess_chapter", side_effect=AssertionError("valid acceptance should be reused")):
            result = drafting.gen_serial_chapters(workspace, stop_event=stop, resume_checkpoint=True)
        assert len(result["artifacts"]) == 1
        assert counts["writer"] == counts["planner"] == counts["editor"] == counts["audit"] == 1


def test_tampered_audited_text_is_reaudited(workspace):
    from training.chapter_pipeline import ChapterReviewRequired
    with pipeline(workspace) as (counts, _):
        with patch("core.chapter_acceptance.assess_chapter", return_value={"status": "needs_review"}):
            with pytest.raises(ChapterReviewRequired):
                drafting.gen_serial_chapters(workspace)
        state = load_checkpoint(workspace, 1, 1)
        state["text"] = state["text"].replace("客人空手离开", "客人独自离开")
        state["stage"] = "validated"
        state["acceptance"] = {"status": "passed", "chapter_sha256": "wrong"}
        save_checkpoint(workspace, 1, 1, state)
        (Path(workspace.file_system) / "drafts/vol_01/candidates/chapter_001.md").unlink()
        drafting.gen_serial_chapters(workspace, resume_checkpoint=True)
    assert counts["audit"] == 2 and counts["writer"] == 1
