import json
import tempfile
import unittest
from pathlib import Path

from training.human_style_library import (
    build_human_style_library,
    continuous_sample_windows,
    format_human_style_context,
    human_style_library_status,
    retrieve_human_style_context,
)


class FakeStyleLLM:
    def generate(self, prompt, temperature=0.2, is_json=False, **kwargs):
        
        payload = {
            "voice_summary": "叙述贴近人物行动，用具体反应推进，不抢先总结。",
            "narrative_distance": "有限贴近人物，不频繁跳出场景评判。",
            "viewpoint_control": "单场景维持主要观察者。",
            "sentence_rhythm": "长短句自然交错，关键动作后允许短句落点。",
            "paragraph_rhythm": "一段完成一个动作或一个交流回合，长短不刻意齐整。",
            "dialogue_style": "对白偏口语，信息藏在反应和追问中。",
            "dialogue_action_link": "对白之间穿插小动作与观察，不连续堆说话标签。",
            "action_style": "动作按发生顺序写，及时给结果反馈。",
            "psychology_style": "少直接解释，多用动作和注意力变化体现。",
            "exposition_style": "设定跟着人物需要逐步出现。",
            "scene_entry": "从正在发生的动作或具体感官进入。",
            "scene_transition": "利用人物行动和时间推进自然转场。",
            "information_release": "先给现象，再在人物需要时补解释。",
            "conflict_escalation": "先小摩擦，再通过选择和后果加压。",
            "emotion_expression": "情绪主要藏在动作、停顿和对白里。",
            "imagery_and_diction": "修饰克制，以具体名词动词为主。",
            "chapter_opening": "快速落到人物当前处境。",
            "chapter_ending": "以未解决的新变化或动作截断。",
            "deliberate_irregularities": "段落长度不齐，对话回合偶尔突然收短。",
            "reproduction_rules": ["对白后优先接具体反应", "设定只在当前行动需要时解释"],
            "avoid_patterns": ["不要每段总结人物心理"],
            "scene_playbooks": {"battle": "动作、位置和结果连续推进。", "dialogue": "追问与反应交替。"},
        }
        return json.dumps(payload, ensure_ascii=False)


class FailingStyleLLM:
    def generate(self, prompt, temperature=0.2, is_json=False, **kwargs):
        raise RuntimeError("profile service unavailable")


def chapter_text(seed: str, repeats: int = 34) -> str:
    paragraphs = []
    for index in range(repeats):
        paragraphs.append(
            f"{seed}{index}。他没有急着解释，只把手里的东西放回桌面，抬眼看了对面一会儿。"
            f"“你先说。”旁边的人敲了敲桌角，声音不高。屋里有人动了一下，又很快安静下来。"
        )
    return "\n\n".join(paragraphs)


class HumanStyleLibraryTests(unittest.TestCase):
    def test_continuous_windows_are_literal_source_slices(self):
        text = chapter_text("连续样本文字", 70)
        windows = continuous_sample_windows(text, "第一章")
        self.assertGreaterEqual(len(windows), 2)
        for item in windows:
            self.assertIn(item["text"], text)
            self.assertNotIn("---", item["text"])

    def test_build_and_scene_retrieval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample_novel.txt"
            source.write_text("人工参考小说", encoding="utf-8")
            chapters = [
                {"title": "第一章", "content": chapter_text("客厅里的谈话", 36)},
                {"title": "第二章", "content": chapter_text("长剑交手后的动作", 38)},
                {"title": "第三章", "content": chapter_text("安静的日常相处", 35)},
            ]
            cards = [
                {"chapter": 1, "chapter_outline_600": "两个人持续对话、询问、回答", "chapter_rhythm": {"core_content": "人物对话"}},
                {"chapter": 2, "chapter_outline_600": "双方战斗交手，连续出手攻击并分出结果", "chapter_rhythm": {"core_content": "战斗动作"}},
                {"chapter": 3, "chapter_outline_600": "吃饭闲聊的日常互动", "chapter_rhythm": {"core_content": "日常生活"}},
            ]
            status = build_human_style_library(
                source,
                root,
                chapters=chapters,
                cards=cards,
                llm=FakeStyleLLM(),
            )
            self.assertTrue(status["ready"])
            self.assertGreaterEqual(status["sample_count"], 3)
            self.assertEqual(status, human_style_library_status(root))

            index = json.loads((root / "style_library" / "index.json").read_text(encoding="utf-8"))
            for item in index["samples"]:
                sample = (root / item["path"]).read_text(encoding="utf-8").strip()
                original = chapters[item["chapter"] - 1]["content"]
                self.assertIn(sample, original)

            context = retrieve_human_style_context(root, "本章是正面战斗，双方连续出手攻击，最后分出胜负")
            self.assertTrue(context["ready"])
            self.assertIn("battle", context["target_scene_types"])
            self.assertIn("battle", context["samples"][0]["scene_types"])
            rendered = format_human_style_context(context)
            self.assertIn("人工文笔库", rendered)
            self.assertIn("作者文笔画像", rendered)
            self.assertIn("连续人工样本", rendered)
            self.assertIn("动作、位置和结果连续推进", rendered)

    def test_profile_failure_keeps_sample_library_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample_novel.txt"
            source.write_text("人工参考小说", encoding="utf-8")
            chapters = [
                {"title": "第一章", "content": chapter_text("人物对话和动作", 36)},
                {"title": "第二章", "content": chapter_text("长剑交手和危机", 38)},
            ]
            status = build_human_style_library(
                source,
                root,
                chapters=chapters,
                llm=FailingStyleLLM(),
            )
            self.assertTrue(status["ready"])
            self.assertFalse(status["advanced_profile_ready"])
            self.assertEqual(status["profile_mode"], "metrics_only")
            self.assertGreaterEqual(status["sample_count"], 2)
            context = retrieve_human_style_context(root, "人物对话后发生战斗")
            self.assertTrue(context["ready"])
            self.assertTrue(context["samples"])
            rendered = format_human_style_context(context)
            self.assertIn("连续人工样本", rendered)
            self.assertIn("全书统计画像", rendered)

            upgraded = build_human_style_library(
                source,
                root,
                chapters=chapters,
                llm=FakeStyleLLM(),
                force=False,
            )
            self.assertTrue(upgraded["ready"])
            self.assertTrue(upgraded["advanced_profile_ready"])
            self.assertEqual(upgraded["sample_count"], status["sample_count"])

    def test_first_chapter_humiliation_prefers_opening_humiliation_sample(self):
        item = {
            "scene_types": ["emotion", "reveal"],
            "semantic_text": "主角当众受辱，被众人嘲讽为废物，身份落差明显",
            "position": "opening",
            "char_count": 1700,
        }
        from training.human_style_library import _retrieval_score, _target_scene_types
        query = "第1章，主角受辱，被下人轻贱嘲讽，废物身份落差，情绪压抑"
        tags = _target_scene_types(query)
        self.assertIn("humiliation", tags)
        self.assertIn("opening", tags)
        self.assertGreater(_retrieval_score(item, tags, query), 20)


if __name__ == "__main__":
    unittest.main()
