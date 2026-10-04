import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.story_memory import (
    MAX_CONTEXT_CHARS, StoryMemoryError, load_story_memory_context,
    previous_chapter_paths, previous_panel_path,
)


class FakeExtractor:
    def __init__(self):
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        match = re.search(r"当前来源：第(\d+)卷第(\d+)章已发布正文】\n([\s\S]*?)\n\n要求：", prompt)
        volume, chapter, text = match.groups()
        return json.dumps({
            "summary": {"text": text, "evidence": [text]},
            "updates": [{"kind": "state", "entity": "测试人物", "key": f"事件{volume}-{chapter}",
                         "value": text, "status": "active", "evidence": text}],
        }, ensure_ascii=False)


class StoryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = SimpleNamespace(file_system=str(self.root))
        self.generate = FakeExtractor()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def chapter(self, volume, chapter, text):
        return self.write(f"chapters/vol_{volume:02d}/{chapter:03d}_第{chapter}章.md", text)

    def panel(self, volume, chapter):
        return self.write(f"system_panels/vol_{volume:02d}/chapter_{chapter:03d}.json", '{"panel": {"level": 3}}')

    def memory(self, volume, chapter):
        return load_story_memory_context(self.ws, volume, chapter, self.generate)

    def test_predecessors_cross_volume_and_ignore_future_raw_and_versions(self):
        one = self.chapter(1, 10, "一卷末前一章。")
        two = self.chapter(1, 11, "一卷末章。")
        current = self.chapter(2, 1, "新卷首章。")
        self.chapter(2, 2, "当前卷的未来。")
        self.chapter(3, 1, "未来卷。")
        self.write("chapters/vol_01/versions/012_第12章.md", "旧版本。")
        self.write("drafts/vol_01/raw_chapters/012_第12章.raw.md", "未发布。")
        self.assertEqual(previous_chapter_paths(self.ws, 2, 1), [one, two])
        self.assertEqual(previous_chapter_paths(self.ws, 2, 2), [two, current])
        self.assertEqual(previous_chapter_paths(self.ws, 2, 1, limit=0), [])

    def test_panel_predecessor_cross_volume(self):
        old = self.panel(1, 8)
        current = self.panel(2, 1)
        self.panel(2, 3)
        self.panel(3, 1)
        self.assertEqual(previous_panel_path(self.ws, 2, 1), old)
        self.assertEqual(previous_panel_path(self.ws, 2, 2), current)
        self.assertIsNone(previous_panel_path(self.ws, 1, 1))

    def test_missing_immediate_panel_does_not_reuse_older_state(self):
        self.panel(1, 8)
        self.write("chapter_outlines/vol_01/chapter_009.md", "章纲")
        with self.assertRaisesRegex(StoryMemoryError, "第1卷第9章"):
            previous_panel_path(self.ws, 2, 10)
        expected = self.panel(1, 9)
        self.assertEqual(previous_panel_path(self.ws, 2, 10), expected)

    def test_invalid_previous_panel_does_not_reset_to_initial_state(self):
        path = self.panel(1, 9)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(StoryMemoryError, "格式无效"):
            previous_panel_path(self.ws, 2, 10)

    def test_no_previous_chapter_does_not_generate_or_read_future(self):
        self.chapter(1, 1, "第一章尚不作为自身历史。")
        self.assertEqual(self.memory(1, 1), "")
        self.assertEqual(self.generate.calls, [])
        self.assertFalse((self.root / "story_memory").exists())

    def test_cache_and_incremental_chapters(self):
        self.chapter(1, 1, "林舟拿走铜钥匙。")
        first = self.memory(1, 2)
        self.assertIn("第1卷第1章", first)
        self.assertIn("原文：林舟拿走铜钥匙。", first)
        self.assertEqual(self.memory(1, 2), first)
        self.assertEqual(len(self.generate.calls), 1)
        self.chapter(1, 2, "林舟打开石门。")
        self.assertIn("林舟打开石门。", self.memory(1, 3))
        self.assertEqual(len(self.generate.calls), 2)
        self.assertIn("林舟拿走铜钥匙。", self.generate.calls[-1])

    def test_old_modification_reextracts_descendants_only(self):
        self.chapter(1, 1, "林舟出发。")
        changed = self.chapter(1, 2, "林舟手持旧剑。")
        self.chapter(2, 1, "林舟来到山门。")
        self.memory(2, 2)
        changed.write_text("林舟手持新刀。", encoding="utf-8")
        context = self.memory(2, 2)
        self.assertEqual(len(self.generate.calls), 5)
        self.assertNotIn("旧剑", context)
        self.assertIn("新刀", context)
        self.assertIn("新刀", self.generate.calls[-1])

    def test_deleted_predecessor_invalidates_descendants_and_removes_memory(self):
        deleted = self.chapter(1, 1, "林舟收下秘密礼物。")
        self.chapter(1, 2, "林舟到达渡口。")
        self.memory(1, 3)
        deleted.unlink()
        context = self.memory(1, 3)
        self.assertEqual(len(self.generate.calls), 3)
        self.assertNotIn("秘密礼物", context)
        self.assertNotIn("秘密礼物", self.generate.calls[-1])

    def test_future_cached_memory_never_leaks_into_earlier_chapter(self):
        self.chapter(1, 1, "林舟尚未见过守卫。")
        self.chapter(1, 2, "守卫揭开王族身世。")
        self.memory(1, 3)
        early = self.memory(1, 2)
        self.assertNotIn("王族", early)
        self.assertEqual(len(self.generate.calls), 2)

    def test_invalid_evidence_rejected_without_creating_cache(self):
        self.chapter(1, 1, "林舟手臂流血。")
        invalid = {"summary": {"text": "林舟受伤。", "evidence": ["林舟手臂流血。"]},
                   "updates": [{"kind": "injury", "entity": "林舟", "key": "手臂",
                                "value": "已经痊愈", "status": "resolved", "evidence": "伤口全部愈合。"}]}
        with self.assertRaisesRegex(StoryMemoryError, "证据不在源正文"):
            load_story_memory_context(self.ws, 1, 2, lambda _: json.dumps(invalid))
        self.assertFalse((self.root / "story_memory" / "vol_01" / "chapter_001.json").exists())

    def test_invalid_summary_and_unknown_kind_rejected(self):
        self.chapter(1, 1, "林舟手臂流血。")
        for invalid in ("not JSON", '{"summary": {"text": "推测", "evidence": ["不存在的引文"]}, "updates": []}',
                        '{"summary": {"text": "受伤", "evidence": ["林舟手臂流血。"]}, "updates": [{"kind": "plan", "status": "active"}]}'):
            with self.subTest(invalid=invalid), self.assertRaises(StoryMemoryError):
                load_story_memory_context(self.ws, 1, 2, lambda _: invalid)

    def test_state_change_replaces_same_key_with_latest_sourced_fact(self):
        self.chapter(1, 1, "林舟左臂受伤。")
        self.chapter(1, 2, "林舟左臂痊愈。")
        def extractor(prompt):
            payload = json.loads(self.generate(prompt))
            update = payload["updates"][0]
            update.update(kind="injury", key="左臂", status="resolved" if "痊愈" in update["value"] else "active")
            return json.dumps(payload)
        context = load_story_memory_context(self.ws, 1, 3, extractor)
        self.assertEqual(context.count("[伤势/"), 1)
        self.assertIn("[伤势/resolved]", context)
        self.assertIn("第1卷第2章；原文：林舟左臂痊愈。", context)

    def test_cancel_or_quota_exception_propagates_without_cache(self):
        self.chapter(1, 1, "林舟出发。")
        class Interrupted(Exception):
            pass
        def interrupted(_):
            raise Interrupted("provider paused")
        with self.assertRaises(Interrupted):
            load_story_memory_context(self.ws, 1, 2, interrupted)
        self.assertFalse((self.root / "story_memory").exists())

    def test_atomic_write_failure_preserves_old_cache(self):
        chapter = self.chapter(1, 1, "林舟出发。")
        self.memory(1, 2)
        path = self.root / "story_memory" / "vol_01" / "chapter_001.json"
        original = path.read_bytes()
        chapter.write_text("林舟返回。", encoding="utf-8")
        with patch("core.story_memory.os.replace", side_effect=OSError("simulated full disk")):
            with self.assertRaises(OSError):
                self.memory(1, 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_cache_is_rebuilt_from_source(self):
        self.chapter(1, 1, "林舟出发。")
        self.memory(1, 2)
        path = self.root / "story_memory" / "vol_01" / "chapter_001.json"
        path.write_text("broken", encoding="utf-8")
        self.assertIn("林舟出发。", self.memory(1, 2))
        self.assertEqual(len(self.generate.calls), 2)

    def test_rendered_context_budget_keeps_complete_evidence_entries(self):
        for chapter in range(1, 61):
            self.chapter(1, chapter, f"第{chapter}场事件：" + "林舟记录了一段很长的未解决线索。" * 15)
        context = self.memory(1, 61)
        self.assertLessEqual(len(context), MAX_CONTEXT_CHARS)
        self.assertIn("长度限制", context)
        self.assertIn("第1卷第60章", context)


if __name__ == "__main__":
    unittest.main()
