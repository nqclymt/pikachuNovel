"""Provider intervention must stop optional prose stages instead of passing as success."""

import json

import pytest

from core.llm_provider import LLMCallCancelled, LLMExecutionBlocked
from training.human_style_library import build_human_style_library, human_style_library_status
from training.prose_editor import edit_chapter_prose
from training.style_engine_v2 import plan_chapter_scenes


class ScriptedLLM:
    model = "blocked-test"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt, **kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response, ensure_ascii=False)


@pytest.mark.parametrize("stage", ["review", "edit", "verify"])
def test_prose_editor_propagates_provider_block_at_every_stage(stage):
    original = "他把欠条推回去。这说明他已经拒绝了这笔交易。"
    replacement = "他把欠条推回去。"
    issue = {"index": 0, "quote": "这说明他已经拒绝了这笔交易。",
             "category": "redundant_explanation", "scope": "local",
             "reason": "动作已经表达拒绝。", "instruction": "删除重复解释。"}
    responses = []
    if stage in {"edit", "verify"}:
        responses.append({"issues": [issue]})
    if stage == "verify":
        responses.append({"replacements": [{"index": 0, "original": original,
                                             "text": replacement}]})
    blocked = LLMExecutionBlocked("额度已用尽，恢复调度后继续。")
    responses.append(blocked)
    llm = ScriptedLLM(*responses)
    with pytest.raises(LLMExecutionBlocked) as caught:
        edit_chapter_prose(llm, original + "\n\n掌柜的手还压在账本上。")
    assert caught.value is blocked
    assert llm.calls == len(responses)


@pytest.mark.parametrize("failure", [LLMExecutionBlocked, LLMCallCancelled])
def test_scene_planner_propagates_intervention_without_fallback(failure):
    error = failure("用户需要先处理当前请求。")
    llm = ScriptedLLM(error)
    with pytest.raises(failure) as caught:
        plan_chapter_scenes(llm, "他拒绝掌柜的交易，然后离开。")
    assert caught.value is error
    assert llm.calls == 1


def test_profile_block_propagates_and_preserves_completed_local_samples(tmp_path):
    content = "\n\n".join(
        f"他放下手中的账本，第{index}次看向对面。“你先说。”掌柜敲了敲桌面，屋里安静下来。"
        for index in range(40)
    )
    source = tmp_path / "sample_novel.txt"
    source.write_text(content, encoding="utf-8")
    llm = ScriptedLLM(LLMExecutionBlocked("请重新登录 Antigravity。"))
    with pytest.raises(LLMExecutionBlocked):
        build_human_style_library(
            source, tmp_path,
            chapters=[{"title": "第一章", "content": content}],
            cards=[], llm=llm,
        )
    assert llm.calls == 1
    status = human_style_library_status(tmp_path)
    assert status["ready"] is True
    assert status["advanced_profile_ready"] is False
    assert source.read_text(encoding="utf-8") == content
