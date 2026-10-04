"""No paid calls: acceptance requires complete checks and original-text evidence."""

import copy
import json
import re

import pytest

from core.chapter_acceptance import assess_chapter
from core.llm_provider import LLMCallCancelled, LLMExecutionBlocked


CHAPTER = "阿青把欠条推回去。\n掌柜仍低着头，没有自报来历。\n阿青空手走出了店门。"
OUTLINE = "阿青拒绝交易。章末阿青空手离开。"
MEMORY = "阿青不知道掌柜的真实身份。掌柜的真实身份本章不得揭露。"
SCENES = [{"scene": 1, "required_events": ["阿青拒绝交易。"],
           "hidden_information": ["掌柜的真实身份本章不得揭露。"],
           "ending_hook": "章末阿青空手离开。"}]
PENDING = [{"quote": "阿青把欠条推回去。", "reason": "这一回合的节奏可能重复。",
            "category": "repeated_beat", "scope": "planning"}]


def passing_payload(prompt):
    event_ids = re.findall(r'"event_id": "(event_[0-9a-f]{16})"', prompt)
    planning_ids = re.findall(r'"issue_id": "(planning_[0-9a-f]{16})"', prompt)
    return {
        "event_checks": [{"event_id": event_id, "status": "present",
                          "evidence": "阿青把欠条推回去。", "reason": "用退回欠条的行动拒绝交易。"}
                         for event_id in event_ids],
        "context_checks": [
            {"category": "hidden_information", "status": "clear",
             "evidence": "掌柜仍低着头，没有自报来历。", "constraint_quote": "掌柜的真实身份本章不得揭露。",
             "reason": "没有揭示掌柜身份。"},
            {"category": "character_knowledge", "status": "clear",
             "evidence": "掌柜仍低着头，没有自报来历。", "constraint_quote": "阿青不知道掌柜的真实身份。",
             "reason": "没有让阿青表现出提前得知身份。"},
            {"category": "ending_state", "status": "clear",
             "evidence": "阿青空手走出了店门。", "constraint_quote": "章末阿青空手离开。",
             "reason": "空手离开符合章末状态。"},
        ],
        "planning_issue_checks": [{"issue_id": issue_id, "status": "resolved",
                                   "category": "literary_preference", "evidence": "阿青把欠条推回去。",
                                   "constraint_quote": "", "reason": "该回合完成了拒绝交易的新事件。"}
                                  for issue_id in planning_ids],
        "style_warnings": [],
    }


class FakeGenerate:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def __call__(self, prompt):
        self.calls.append(prompt)
        payload = passing_payload(prompt)
        if self.mutate:
            self.mutate(payload)
        return json.dumps(payload, ensure_ascii=False)


def assess(generate, **overrides):
    inputs = {"chapter_text": CHAPTER, "chapter_outline": OUTLINE, "scene_plan": SCENES,
              "writing_requirements": "本轮对白减少，仍保留阿青拒绝交易的事实。", "memory_context": MEMORY}
    inputs.update(overrides)
    return assess_chapter(generate, **inputs)


def test_one_complete_evidenced_audit_passes_without_rewriting():
    generate = FakeGenerate()
    result = assess(generate)
    assert result["status"] == "passed"
    assert result["blocking_issues"] == []
    assert result["warnings"] == []
    assert len(result["chapter_sha256"]) == 64
    assert len(generate.calls) == 1
    assert CHAPTER in generate.calls[0]
    assert "本轮对白减少" in generate.calls[0]
    assert "章末状态" in generate.calls[0]


def test_event_ids_remain_stable_on_reorder_and_duplicate_scene_events():
    original = assess(FakeGenerate())["required_events"][0]
    repeated = assess(FakeGenerate(), scene_plan=[{"required_events": []}, *SCENES, *SCENES])
    assert len(repeated["required_events"]) == 1
    assert repeated["required_events"][0]["event_id"] == original["event_id"]
    assert repeated["required_events"][0]["scenes"] == [2, 3]


def test_explicit_missing_event_with_known_id_blocks_without_invented_evidence():
    def missing(payload):
        payload["event_checks"][0].update(status="missing", evidence="", reason="没有发生拒绝交易的事件。")
    result = assess(FakeGenerate(missing), chapter_text=CHAPTER.replace("阿青把欠条推回去。\n", ""))
    assert result["status"] == "needs_review"
    assert result["blocking_issues"][0]["category"] == "missing_required_event"
    assert result["blocking_issues"][0]["evidence"] == ""


@pytest.mark.parametrize("dimension", ["hidden_information", "character_knowledge", "ending_state"])
def test_clear_context_violation_requires_author_review(dimension):
    def violation(payload):
        row = next(row for row in payload["context_checks"] if row["category"] == dimension)
        row.update(status="violation", reason="该原文与所引用的上下文约束直接冲突。")
    result = assess(FakeGenerate(violation))
    assert result["status"] == "needs_review"
    assert result["blocking_issues"][0]["dimension"] == dimension
    assert result["blocking_issues"][0]["category"] in {"fact_conflict", "premature_reveal"}


def test_literary_preference_and_uncertainty_are_warnings_not_blockers():
    def uncertain(payload):
        payload["event_checks"][0].update(status="uncertain", reason="这处退回动作的交易含义不明确。")
        payload["planning_issue_checks"][0].update(status="unresolved", reason="节奏仍可能重复，但不涉及剧情事实错误。")
        payload["style_warnings"].append({"evidence": "阿青空手走出了店门。", "reason": "可以考虑另一种收束节奏。"})
    result = assess(FakeGenerate(uncertain), planning_issues=PENDING)
    assert result["status"] == "passed"
    assert result["blocking_issues"] == []
    assert len(result["warnings"]) == 3


def test_missing_context_uses_not_applicable_without_fabricating_constraints():
    def no_constraints(payload):
        for row in payload["context_checks"]:
            row.update(status="not_applicable", constraint_quote="", reason="输入未明确规定这一类事实边界。")
    result = assess(FakeGenerate(no_constraints), chapter_outline="", scene_plan=[], memory_context="")
    assert result["status"] == "passed"
    assert result["required_events"] == []


def corrupt(payload, kind):
    row = payload["event_checks"][0]
    if kind == "missing_event":
        payload["event_checks"] = []
    elif kind == "duplicate_event":
        payload["event_checks"].append(copy.deepcopy(row))
    elif kind == "unknown_event":
        row["event_id"] = "event_invented"
    elif kind == "fake_evidence":
        row["evidence"] = "他接受交易并收下了欠条。"
    elif kind == "empty_present_evidence":
        row["evidence"] = ""
    elif kind == "invalid_status":
        row["status"] = "passed"
    elif kind == "empty_reason":
        row["reason"] = ""
    elif kind == "missing_context":
        payload["context_checks"].pop()
    elif kind == "unknown_context":
        payload["context_checks"][0]["category"] = "unrelated_style"
    elif kind == "invented_constraint":
        payload["context_checks"][0]["constraint_quote"] = "阿青此时已经知道掌柜身份。"
    elif kind == "unanchored_fact":
        payload["context_checks"][0].update(status="violation", constraint_quote="")
    elif kind == "fake_warning":
        payload["style_warnings"] = [{"evidence": "正文并没有出现这句话。", "reason": "句式机械。"}]


@pytest.mark.parametrize("kind", ["missing_event", "duplicate_event", "unknown_event", "fake_evidence",
                                 "empty_present_evidence", "invalid_status", "empty_reason", "missing_context",
                                 "unknown_context", "invented_constraint", "unanchored_fact", "fake_warning"])
def test_invalid_or_incomplete_audits_never_pass(kind):
    generate = FakeGenerate(lambda payload: corrupt(payload, kind))
    with pytest.raises(RuntimeError, match="验收结果无效"):
        assess(generate)
    assert len(generate.calls) == 1


def test_pending_planning_issues_cannot_be_omitted():
    generate = FakeGenerate(lambda payload: payload.update(planning_issue_checks=[]))
    with pytest.raises(RuntimeError, match="planning_issue_checks 未覆盖"):
        assess(generate, planning_issues=PENDING)


def test_planning_fact_conflict_needs_explicit_context_evidence():
    def conflict(payload):
        payload["planning_issue_checks"][0].update(status="unresolved", category="fact_conflict",
                                                  constraint_quote="", reason="不能仅凭审读意见断言事实错误。")
    with pytest.raises(RuntimeError, match="真实约束证据"):
        assess(FakeGenerate(conflict), planning_issues=PENDING)


def test_planning_missing_event_must_agree_with_event_check():
    def missing(payload):
        payload["planning_issue_checks"][0].update(status="unresolved", category="missing_required_event",
                                                  event_id=payload["event_checks"][0]["event_id"], evidence="")
    with pytest.raises(RuntimeError, match="明确判为 missing"):
        assess(FakeGenerate(missing), planning_issues=PENDING)


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"event_checks": []'])
def test_malformed_responses_raise_clear_error(raw):
    with pytest.raises(RuntimeError, match="验收结果无效"):
        assess(lambda prompt: raw)


def test_duplicate_json_status_cannot_override_an_earlier_failure():
    def generate(prompt):
        raw = json.dumps(passing_payload(prompt), ensure_ascii=False)
        return raw.replace('"status": "present"', '"status": "missing", "status": "present"', 1)

    with pytest.raises(RuntimeError, match="重复定义了 status"):
        assess(generate)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_constants_are_rejected(constant):
    def generate(prompt):
        raw = json.dumps(passing_payload(prompt), ensure_ascii=False)
        return raw[:-1] + ', "score": ' + constant + '}'

    with pytest.raises(RuntimeError, match="不支持的数值"):
        assess(generate)


def test_empty_planning_issue_is_rejected_before_generating():
    generate = FakeGenerate()
    with pytest.raises(RuntimeError, match="审读问题条目无效"):
        assess(generate, planning_issues=["  "])
    assert not generate.calls


@pytest.mark.parametrize("failure", [LLMCallCancelled, LLMExecutionBlocked])
def test_provider_cancellation_and_blocking_propagate_without_retry(failure):
    calls = []
    error = failure("需要先处理当前调用。")

    def generate(prompt):
        calls.append(prompt)
        raise error

    with pytest.raises(failure) as caught:
        assess(generate)
    assert caught.value is error
    assert len(calls) == 1


def test_large_context_is_rejected_before_model_call():
    generate = FakeGenerate()
    with pytest.raises(RuntimeError, match="未截断剧情事实"):
        assess(generate, memory_context="已经确认的事实。" * 15000)
    assert generate.calls == []
