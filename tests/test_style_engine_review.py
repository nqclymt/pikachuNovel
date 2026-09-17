"""Regression tests for the actual planner -> retrieval -> writer -> local editor chain."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.llm_provider import LLMCallCancelled
from core.prompt_loader import PromptLoader
from training import adaptive_builder as drafting
from training import human_style_library as library
from training import style_engine_v2 as engine

OUTLINE = "【第1章 章纲】\n\n# 故事线\n醒来+来客试探+送客\n\n# 单章节奏\n平静转不安\n\n# 单章简介\n主人醒来，屋里寂静。\n来客进门，说了半句就停下。\n主人送客，门外天色已暗。"
PLAN = {"scenes": [{"scene_goal": "来客进门后试探主人", "hidden_information": ["来客的真实来意"], "new_information": ["来客想借书"], "style_retrieval_query": {"scene_type": "dialogue", "emotion": "suppressed_tension", "information_function": "partial_reveal", "conflict_level": "low", "pacing": "medium"}}]}


class ProfileLLM:
    def generate(self, prompt, **kwargs):
        return json.dumps({"voice_summary": "停顿随人物动作发生。", "narrative": {"pov": "有限视角", "time_handling": "按场景需要停留"}, "dialogue": {"subtext": "保留没有说完的意图"}}, ensure_ascii=False)


class RecordingLLM:
    def __init__(self, payload=PLAN):
        self.payload, self.calls, self.cancelable = payload, [], False

    def generate(self, prompt, **kwargs):
        self.calls.append(prompt)
        return json.dumps(self.payload, ensure_ascii=False)

    def generate_cancelable(self, prompt, event, **kwargs):
        self.cancelable = True
        return self.generate(prompt, **kwargs)


def build_fixture(root):
    source = root / "sample_novel.txt"
    source.write_text("测试参考原文", encoding="utf-8")
    chapters = [{"title": "第一章", "content": "第一章\n" + "\n".join(f"对话样本{i}：客人把杯子转过半圈，问了个并不重要的问题。主人看着门口，没有直接回答。" for i in range(26))},
                {"title": "第二章", "content": "第二章\n" + "\n".join(f"街头见闻{i}：街口的伙计正收摊，一辆车停在巷外，赶车人伸手扶住帽子。" for i in range(32))}]
    library.build_human_style_library(source, root, chapters=chapters, llm=ProfileLLM())
    return source, chapters


class StyleEngineReviewTests(unittest.TestCase):
    def setUp(self):
        PromptLoader._prompts.clear()

    def test_real_template_reaches_model(self):
        llm = RecordingLLM()
        result = engine.plan_chapter_scenes(llm, OUTLINE)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result[0]["planner_source"], "llm")
        self.assertEqual(result[0]["hidden_information"], ["来客的真实来意"])

    def test_template_error_is_not_hidden_as_fallback(self):
        llm = RecordingLLM()
        with patch.object(PromptLoader, "load", side_effect=KeyError("broken template")):
            with self.assertRaises(KeyError):
                engine.plan_chapter_scenes(llm, OUTLINE)
        self.assertFalse(llm.calls)

    def test_invalid_model_scene_uses_explicit_fallback(self):
        result = engine.plan_chapter_scenes(RecordingLLM({"scenes": [{"scene_goal": ""}]}), OUTLINE)
        self.assertTrue(all(s["planner_source"] == "deterministic_fallback" for s in result))
        self.assertTrue(all(s.get("planner_error") for s in result))

    def test_markdown_fallback_does_not_turn_headings_into_scenes(self):
        result = engine.fallback_scene_plan(OUTLINE, "后续大结局决战，不属于本章")
        goals = "\n".join(s["scene_goal"] for s in result)
        for bad in ("故事线", "单章节奏", "单章简介", "大结局"):
            self.assertNotIn(bad, goals)
        self.assertIn("来客进门", goals)
        self.assertEqual(len(result), 3)

    def test_local_metadata_ignores_other_scenes(self):
        local = "他整理桌上的茶具，窗外雨声很轻。"
        a = engine.infer_scene_metadata(local)
        b = engine.infer_scene_metadata(local, semantic_hint="决战攻击追杀威胁生死" * 20)
        self.assertEqual(a, b)
        self.assertEqual(a["analysis_source"], "heuristic")

    def test_pre_cancel_never_calls_model(self):
        event, llm = threading.Event(), RecordingLLM()
        event.set()
        with self.assertRaises(LLMCallCancelled):
            engine.plan_chapter_scenes(llm, OUTLINE, cancel_event=event)
        self.assertFalse(llm.calls)

    def test_provider_cancellation_propagates(self):
        llm = RecordingLLM()
        with patch.object(llm, "generate_cancelable", side_effect=LLMCallCancelled("stopped")):
            with self.assertRaises(LLMCallCancelled):
                engine.plan_chapter_scenes(llm, OUTLINE, cancel_event=threading.Event())

    def test_planner_uses_cancelable_provider(self):
        llm = RecordingLLM()
        engine.plan_chapter_scenes(llm, OUTLINE, cancel_event=threading.Event())
        self.assertTrue(llm.cancelable)

    def test_oversized_plot_fails_without_truncation_or_request(self):
        llm = RecordingLLM()
        with self.assertRaises(ValueError):
            engine.plan_chapter_scenes(llm, "事实" * 31000)
        self.assertFalse(llm.calls)

    def test_source_offsets_are_original_crlf_offsets(self):
        text = "  第一章 测试\r\n\r\n" + "\r\n".join(("屋里的访客已经离开。" * 30, "茶水渐渐凉了。" * 30, "第二天清晨，" + "他走进院子。" * 45, "门关上了。"))
        scenes = engine.split_scenes_verbatim(text, "第一章 测试")
        self.assertGreaterEqual(len(scenes), 2)
        for scene in scenes:
            self.assertEqual(text[scene["source_start"]:scene["source_end"]], scene["text"])
        self.assertTrue(scenes[-1]["text"].endswith("门关上了。"))

    def test_long_paragraph_is_not_one_unbounded_example(self):
        scenes = engine.split_scenes_verbatim("他翻过门口的石阶，听见屋里传来杯盘碰撞的声音。" * 180)
        self.assertGreater(len(scenes), 1)
        self.assertTrue(all(s["text"].endswith("。") for s in scenes))

    def test_single_newline_paragraph_stats_survive_chapter_join(self):
        first = "一二三。\n四五六。"
        second = "七八九。\n甲乙丙。"
        metrics = library._style_metrics(first + "\n\n" + second)
        self.assertEqual(metrics["paragraph_count"], 4)
        self.assertEqual(metrics["paragraph_avg"], 4)

    def test_repeated_token_cosine_identity(self):
        text = "试探试探试探，没有说完，没有说完。"
        self.assertAlmostEqual(engine._lexical_similarity(text, text), 1.0)

    def test_small_budget_and_missing_or_escaped_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "outside.txt").write_text("不应读取", encoding="utf-8")
            samples = root / "style_library" / "samples"
            samples.mkdir(parents=True)
            (samples / "valid.txt").write_text("他推门。她点头。" * 200, encoding="utf-8")
            items = [{"id": str(i), "path": path, "chapter": 1, "scene": i, "style_tags": [{"bad": "value"}], "analysis_confidence": "not-a-number"} for i, path in enumerate(["outside.txt", "../outside.txt", "style_library/samples/missing.txt", "style_library/samples/valid.txt"])]
            selected = engine.retrieve_diverse_style_examples(root, items, "对话", max_chars=19)
            self.assertLessEqual(sum(len(s["text"]) for s in selected), 19)
            self.assertTrue(all(s["path"].endswith("valid.txt") for s in selected))
            self.assertEqual(engine.retrieve_diverse_style_examples(root, items, "对话", max_chars=0), [])
            self.assertEqual(engine.retrieve_diverse_style_examples(root, items, "对话", max_samples=0), [])

    def test_excerpt_never_ends_inside_a_quote(self):
        self.assertEqual(engine._bounded_source_excerpt("门开了。“他还没回来。你明天再来。”", 15), "门开了。")

    def test_total_scene_context_budget_and_dedup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_fixture(root)
            index = json.loads((root / "style_library/index.json").read_text(encoding="utf-8"))
            trace = []
            plans = [{"scene": i, "scene_goal": "对话", "style_retrieval_query": {"scene_type": "dialogue"}} for i in range(1, 9)]
            rendered = engine.build_scene_style_context(root, index["samples"], plans, max_total_chars=2500, trace=trace)
            self.assertLessEqual(len(rendered), 2500)
            ids = [x["id"] for row in trace for x in row["examples"]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_empty_profile_cannot_be_qualitative(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, chapters = build_fixture(root)
            result = library.build_human_style_library(source, root, chapters=chapters, force=True, llm=RecordingLLM({}))
            self.assertTrue(result["ready"])
            self.assertFalse(result["advanced_profile_ready"])

    def test_failed_rebuild_preserves_live_index_and_samples(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, chapters = build_fixture(root)
            path = root / "style_library/index.json"
            old = path.read_bytes()
            with patch.object(library, "_write_text", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    library.build_human_style_library(source, root, chapters=chapters, force=True)
            self.assertEqual(path.read_bytes(), old)
            self.assertTrue(library.human_style_library_status(root)["ready"])

    def test_mismatched_profile_not_injected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_fixture(root)
            index = json.loads((root / "style_library/index.json").read_text(encoding="utf-8"))
            path = root / index["profile_path"]
            profile = json.loads(path.read_text(encoding="utf-8"))
            profile["source_digest"] = "different-book"
            path.write_text(json.dumps(profile), encoding="utf-8")
            self.assertFalse(library.load_human_style_profile(root))
            self.assertFalse(library.human_style_library_status(root)["advanced_profile_ready"])

    def test_naturalization_does_not_flag_normal_punctuation_or_length(self):
        profile = {"human_style_library": True}
        text = "这不是为了求情，而是另有缘由——他还没解释清楚。"
        self.assertEqual(drafting._select_naturalization_paragraphs(["第1章", text], profile), [])

    def test_atomic_chapter_write_failure_keeps_previous_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chapter.md"
            path.write_text("旧正文", encoding="utf-8")
            with patch("os.replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    drafting._write_generated_chapter(str(path), "新正文")
            self.assertEqual(path.read_text(encoding="utf-8"), "旧正文")

    def test_writer_chain_calls_real_planner_template_and_preserves_voice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / "reference"
            reference.mkdir()
            source, _ = build_fixture(reference)
            fs = root / "file_system"
            outlines = fs / "chapter_outlines/vol_01"
            outlines.mkdir(parents=True)
            (outlines / "chapter_001.md").write_text(OUTLINE, encoding="utf-8")
            ws = SimpleNamespace(file_system=str(fs), reference=str(reference), reference_sample=str(source), reference_chapters=str(reference / "chapters"))
            # A single long paragraph with a normal dash must not be forcibly split or wholly rewritten.
            long_line = "".join(chr(0x4e20 + i) for i in range(230)) + "。这不是为了求情，而是另有缘由——他仍没有说。"
            prose = "第1章 访客\n\n" + long_line + "\n\n客人离开了。"
            llm = RecordingLLM()
            def route(prompt, **kwargs):
                llm.calls.append(prompt)
                if "你是长篇小说的 Scene Planner" in prompt:
                    return json.dumps(PLAN, ensure_ascii=False)
                if "Chinese commercial-fiction line editor" in prompt:
                    return '{"replacements":[]}'
                return prose
            llm.generate = route
            replacements = {"_load_volume_outline_context": "现有全书设计", "_get_lite_llm": llm, "_get_humanize_llm": llm,
                            "_finalized_chapter_numbers": set(), "_find_story_arc_for_chapter": "主人会客后送客。",
                            "_aligned_reference_chapter_context": {}, "system_panel_status": {"enabled": False},
                            "retrieve_world_knowledge": {"context": "事实哨兵：主人尚未知道来客身份。", "hits": [], "snapshot_path": None}}
            with contextlib.ExitStack() as stack:
                for name, value in replacements.items():
                    stack.enter_context(patch.object(drafting, name, return_value=value))
                stack.enter_context(patch.object(drafting, "_repair_chapter_style", side_effect=AssertionError("whole chapter rewrite forbidden")))
                result = drafting.gen_serial_chapters(ws, max_chapters=1, humanize=True)
            self.assertEqual(len(result["artifacts"]), 1)
            saved = (fs / "chapters/vol_01/001_第1章.md").read_text(encoding="utf-8").strip()
            self.assertEqual(saved, prose)
            self.assertEqual(sum("你是长篇小说的 Scene Planner" in p for p in llm.calls), 1)
            writer = next(p for p in llm.calls if "你是一个专业的网文小说家" in p)
            self.assertIn("事实哨兵", writer)
            self.assertIn("来客的真实来意", writer)
            self.assertIn("dialogue.subtext", writer)
            self.assertIn("目标 Scene 1 的写法案例", writer)
            self.assertNotIn("战斗动作使用", writer)
            self.assertTrue((fs / "drafts/vol_01/raw_chapters/001_第1章.raw.md").exists())
            traces = list((fs / "drafts/vol_01/style_traces").glob("*.json"))
            trace = json.loads(traces[0].read_text(encoding="utf-8"))
            self.assertEqual(trace["planner_sources"], ["llm"])
            self.assertLessEqual(trace["style_context_chars"], 18000)
            self.assertTrue(trace["retrieval"][0]["examples"])


if __name__ == "__main__":
    unittest.main()
