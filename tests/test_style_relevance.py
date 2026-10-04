"""Retrieval can decline irrelevant samples without a paid model call."""

import threading

import pytest

from core.llm_provider import LLMCallCancelled
from training.style_engine_v2 import build_scene_style_context, retrieve_diverse_style_examples


def sample(root, **changes):
    path = root / "style_library/samples/sample.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("晨光穿过枝叶，落在碧绿的苔藓上。", encoding="utf-8")
    row = {"id": "sample", "chapter": 1, "scene": 1,
           "path": "style_library/samples/sample.txt", "scene_type": "daily",
           "emotion": "neutral", "conflict_level": "low", "plot_function": "development",
           "information_function": "implicit_progress", "pacing": "medium",
           "ending_type": "continuation", "analysis_confidence": 1.0,
           "semantic_text": "晨光穿过枝叶，落在碧绿的苔藓上。"}
    row.update(changes)
    return row


def test_defaults_and_high_confidence_do_not_justify_an_unrelated_sample(tmp_path):
    item = sample(tmp_path)
    diagnostics = {}
    result = retrieve_diverse_style_examples(tmp_path, [item], "他完成了这件事。", diagnostics=diagnostics)
    assert result == []
    assert diagnostics == {"status": "no_relevant_examples", "considered": 1, "relevant": 0, "selected": 0}


def test_wrong_scene_with_only_one_common_emotion_is_not_a_match(tmp_path):
    item = sample(tmp_path, scene_type="action", emotion="suppressed_tension")
    query = {"scene_type": "dialogue", "emotion": "suppressed_tension", "scene_goal": "商议赎金。"}
    assert retrieve_diverse_style_examples(tmp_path, [item], query) == []


@pytest.mark.parametrize("item_fields,query,route", [
    ({"scene_type": "dialogue"}, {"scene_type": "dialogue"}, "relevance:scene_type=dialogue"),
    ({"emotion": "fear", "information_function": "foreshadowing"},
     {"scene_type": "investigation", "emotion": "fear", "information_function": "foreshadowing"},
     "relevance:purpose=emotion+information_function"),
    ({"semantic_text": "两人商议赎金数额。"}, {"scene_goal": "两人商议赎金数额。"}, "relevance:lexical=1.00"),
])
def test_supported_matching_routes_keep_examples(tmp_path, item_fields, query, route):
    item = sample(tmp_path, **item_fields)
    diagnostics = {}
    selected = retrieve_diverse_style_examples(tmp_path, [item], query, diagnostics=diagnostics)
    assert len(selected) == 1
    assert route in selected[0]["match_reasons"]
    assert diagnostics["status"] == "matched"


def test_no_match_keeps_profile_only_instruction_and_explains_trace(tmp_path):
    trace = []
    scene = {"scene": 1, "scene_goal": "商议赎金。", "style_retrieval_query": {"scene_type": "dialogue"}}
    context = build_scene_style_context(tmp_path, [sample(tmp_path)], [scene], trace=trace)
    assert "以章纲和作者画像为准" in context
    assert "晨光穿过枝叶" not in context
    assert trace[0]["examples"] == []
    assert trace[0]["retrieval"]["status"] == "no_relevant_examples"
    assert "最低匹配门槛" in trace[0]["reason"]


def test_trace_distinguishes_missing_files_from_irrelevant_content(tmp_path):
    item = sample(tmp_path, scene_type="dialogue", path="style_library/samples/absent.txt")
    diagnostics = {}
    assert retrieve_diverse_style_examples(tmp_path, [item], {"scene_type": "dialogue"}, diagnostics=diagnostics) == []
    assert diagnostics["status"] == "no_readable_examples"
    assert diagnostics["relevant"] == 1


def test_pre_cancelled_retrieval_raises_even_without_budget(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(LLMCallCancelled):
        retrieve_diverse_style_examples(tmp_path, [], "", max_chars=0, cancel_event=cancel)
