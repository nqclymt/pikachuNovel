import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.prompt_loader import PromptLoader
from training.adaptive_builder import (
    _aligned_reference_chapter_context,
    _naturalize_paragraph_batch,
    _reference_anchor_prompt_text,
    _reference_style_profile,
    _reference_style_profile_text,
    _select_naturalization_paragraphs,
)


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def generate(self, prompt, temperature=0.7, is_json=False, **kwargs):
        self.prompt = prompt
        self.is_json = is_json
        return json.dumps(self.payload, ensure_ascii=False)


class ReferenceStyleNaturalizationTests(unittest.TestCase):
    def test_reference_style_profile_uses_statistics_without_leaking_source_text(self):
        with tempfile.TemporaryDirectory() as root:
            sample = Path(root) / "sample_novel.txt"
            unique = "独一无二的参考原句绝不能进入风格画像"
            sample.write_text(
                "第1章 开端\n"
                f"{unique}。他停了一会儿，才把门推开。\n\n"
                "“你来了？”屋里的人问。\n\n"
                "雨落得很密。脚步声一阵近，一阵远。\n"
                "第2章 后续\n"
                "天亮以后，街上渐渐有了人声。有人叫卖，有人赶路。\n\n"
                "“走吧。”他说。\n",
                encoding="utf-8",
            )
            ws = SimpleNamespace(reference_sample=str(sample))
            profile = _reference_style_profile(ws)
            rendered = _reference_style_profile_text(profile)
            self.assertGreater(profile["sentence_count"], 4)
            self.assertGreater(profile["sentence_avg"], 0)
            self.assertGreater(profile["paragraph_avg"], 0)
            self.assertNotIn(unique, rendered)
            self.assertIn("statistical style baseline", rendered)

    def test_selection_targets_outlier_paragraphs_instead_of_whole_chapter(self):
        profile = {
            "sentence_avg": 22.0,
            "sentence_std": 7.0,
            "short_sentence_ratio": 0.18,
            "long_sentence_ratio": 0.12,
            "paragraph_avg": 100.0,
            "paragraph_std": 45.0,
            "dialogue_ratio": 0.2,
        }
        paragraphs = [
            "第1章 测试",
            "他沿着街边慢慢走过去，雨水从屋檐滴下来，路边摊贩已经开始收拾东西。",
            "他看。\n他走。\n他停。\n他想。\n他回头。\n他又看。",
            "掌柜把账本合上，抬头问了句来意。他没有急着回答，只先看了眼门外。",
            "不是因为害怕，而是因为命运的齿轮已经开始转动。",
        ]
        selected = _select_naturalization_paragraphs(paragraphs, profile, "standard", 1)
        self.assertIn(2, selected)
        self.assertIn(4, selected)
        self.assertNotIn(0, selected)
        self.assertLess(len(selected), len(paragraphs))

    def test_local_batch_only_replaces_requested_targets(self):
        paragraphs = ["第1章 测试", "前文保持不动。", "需要调整的目标段落。", "后文保持不动。"]
        llm = _FakeLLM({"replacements": [{"index": 2, "text": "调整后的目标段落，更自然，也保留原意。"}]})
        applied = _naturalize_paragraph_batch(
            llm,
            paragraphs,
            [2],
            "Reference statistical style baseline.",
            "Keep facts unchanged.",
            "standard",
        )
        self.assertEqual(applied, 1)
        self.assertEqual(paragraphs[1], "前文保持不动。")
        self.assertEqual(paragraphs[3], "后文保持不动。")
        self.assertIn("调整后的目标段落", paragraphs[2])
        self.assertTrue(llm.is_json)
        self.assertIn('"index": 2', llm.prompt)

    def test_aligned_reference_context_uses_same_volume_progress_and_rhythm_card(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            reference = root_path / "reference"
            chapters = reference / "chapters"
            volume_dir = chapters / "vol_01_demo"
            cards = reference / "chapter_cards"
            volume_dir.mkdir(parents=True)
            cards.mkdir(parents=True)
            (chapters / "_volumes.json").write_text(
                json.dumps([{"title": "demo", "dir": "vol_01_demo"}], ensure_ascii=False),
                encoding="utf-8",
            )
            (volume_dir / "001_第一章.md").write_text("第一章\n\n第一章人工段落。", encoding="utf-8")
            (volume_dir / "002_第二章.md").write_text(
                "第二章\n\n第二章人工叙述先慢后快。\n\n人物开口以后，动作跟着发生。",
                encoding="utf-8",
            )
            (cards / "chapter_0002.json").write_text(
                json.dumps({
                    "chapter": 2,
                    "chapter_rhythm": {"core_content": "铺垫+冲突", "emotion_tone": "紧张"},
                    "story_line": "铺垫+冲突+钩子",
                    "highlights": ["章末钩子"],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            ws = SimpleNamespace(reference=str(reference), reference_chapters=str(chapters))
            anchor = _aligned_reference_chapter_context(ws, 1, 2, 2)
            rendered = _reference_anchor_prompt_text(anchor)
            self.assertEqual(anchor["chapter"], 2)
            self.assertIn("第二章人工叙述", anchor["text"])
            self.assertEqual(anchor["rhythm"]["story_line"], "铺垫+冲突+钩子")
            self.assertIn("动作与对白如何交替", rendered)

    def test_naturalize_prompt_renders_literal_json_example(self):
        rendered = PromptLoader.load(
            "naturalize_paragraphs",
            strength="standard",
            style_profile="profile",
            writing_guide="guide",
            style_anchor="human sample",
            paragraph_records="[]",
        )
        self.assertIn('{"replacements":[{"index":12', rendered)
        self.assertIn("human sample", rendered)


if __name__ == "__main__":
    unittest.main()
