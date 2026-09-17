import tempfile
import unittest
from pathlib import Path

from training.style_engine_v2 import (
    fallback_scene_plan,
    infer_scene_metadata,
    retrieve_diverse_style_examples,
    split_scenes_verbatim,
)


def _paragraph(prefix: str, n: int = 8) -> str:
    return "".join(f"{prefix}{i}。他停了一下，又把视线转回对面。" for i in range(n))


class StyleEngineV2Tests(unittest.TestCase):
    def test_scene_splitter_keeps_verbatim_contiguous_slices(self):
        body = "\n\n".join([
            _paragraph("屋里很安静", 10),
            _paragraph("两个人低声交谈", 10),
            "第二天清晨，" + _paragraph("他出了门", 10),
            _paragraph("街口有人拦住去路", 10),
        ])
        text = "第一章 旧事\n" + body
        scenes = split_scenes_verbatim(text, "第一章 旧事")
        self.assertGreaterEqual(len(scenes), 2)
        for scene in scenes:
            self.assertIn(scene["text"], body)
            self.assertGreater(scene["char_count"], 0)
        self.assertEqual(scenes[0]["position"], "opening")
        self.assertEqual(scenes[-1]["position"], "ending")

    def test_metadata_has_multidimensional_fields(self):
        metadata = infer_scene_metadata(
            "两个人表面闲聊，实际互相试探。她没有把真相说完，只给了一条线索。",
            semantic_hint="压抑的对话，部分揭露",
        )
        for key in (
            "scene_type", "emotion", "conflict_level", "plot_function",
            "information_function", "pacing", "ending_type",
            "style_tags", "analysis_confidence", "analysis_uncertainty",
        ):
            self.assertIn(key, metadata)

    def test_retrieval_uses_metadata_and_diversity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            samples = root / "style_library" / "samples"
            samples.mkdir(parents=True)
            items = []
            specs = [
                (1, "dialogue", "suppressed_tension", "medium", "partial_reveal", "两人低声试探，话只说一半。"),
                (1, "dialogue", "suppressed_tension", "medium", "partial_reveal", "同章另一场也在试探。"),
                (2, "dialogue", "suppressed_tension", "medium", "partial_reveal", "另一章里，两个人仍旧没有把话说透。"),
                (3, "action", "anger", "high", "implicit_progress", "拔剑追杀，动作很快。"),
            ]
            for idx, (chapter, scene_type, emotion, conflict, info, text) in enumerate(specs, start=1):
                path = samples / f"s{idx}.txt"
                path.write_text(text, encoding="utf-8")
                items.append({
                    "id": f"s{idx}", "chapter": chapter, "scene": idx,
                    "scene_type": scene_type, "emotion": emotion,
                    "conflict_level": conflict, "plot_function": "development",
                    "information_function": info, "pacing": "medium",
                    "analysis_confidence": 0.9,
                    "style_tags": [scene_type],
                    "semantic_text": text,
                    "path": f"style_library/samples/s{idx}.txt",
                })
            query = {
                "scene_type": "dialogue", "emotion": "suppressed_tension",
                "conflict_level": "medium", "plot_function": "development",
                "information_function": "partial_reveal", "pacing": "medium",
                "scene_goal": "两个人表面聊天实际上互相试探",
            }
            selected = retrieve_diverse_style_examples(root, items, query, max_samples=2, max_chars=2000)
            self.assertEqual(len(selected), 2)
            self.assertTrue(all(item["scene_type"] == "dialogue" for item in selected))
            self.assertEqual(len({item["chapter"] for item in selected}), 2)
            self.assertTrue(selected[0]["match_reasons"])

    def test_scene_plan_fallback_is_structured(self):
        outline = "开场醒来，发现处境异常。\n\n下人进门轻慢试探。\n\n婚书出现，主角察觉熟悉感。"
        scenes = fallback_scene_plan(outline, "本章压抑推进")
        self.assertGreaterEqual(len(scenes), 2)
        for scene in scenes:
            self.assertIn("scene_goal", scene)
            self.assertIn("style_retrieval_query", scene)
            self.assertIn("scene_type", scene["style_retrieval_query"])
            self.assertEqual(scene["planner_source"], "deterministic_fallback")


if __name__ == "__main__":
    unittest.main()
