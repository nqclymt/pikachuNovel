import json
import tempfile
import unittest
from pathlib import Path

from webui.task_runner import WorkspaceStore


class ReferenceReuseTests(unittest.TestCase):
    def _make_workspace(self, root: Path, name: str, complete: bool = False):
        base = root / name
        reference = base / "reference"
        fs = base / "file_system"
        reference.mkdir(parents=True)
        fs.mkdir(parents=True)
        (reference / "sample_novel.txt").write_text("第1章 A\n正文\n第2章 B\n正文\n", encoding="utf-8")
        (reference / "chapters").mkdir()
        (reference / "outlines").mkdir()
        if complete:
            (reference / "chapters" / "chapter_0001.txt").write_text("A", encoding="utf-8")
            (reference / "chapters" / "chapter_0002.txt").write_text("B", encoding="utf-8")
            cards = reference / "chapter_cards"
            cards.mkdir()
            (cards / "chapter_0001.json").write_text("{}", encoding="utf-8")
            (reference / "chapter_cards_index.json").write_text("{}", encoding="utf-8")
            (reference / "analysis_state.json").write_text(json.dumps({"chapter_cards": {"complete_count": 2}}), encoding="utf-8")
            (reference / "import_state.json").write_text(json.dumps({
                "source_name": "历史小说.txt",
                "processed_chapters": 2,
                "total_chapters": 2,
                "is_complete": True,
            }, ensure_ascii=False), encoding="utf-8")
            vol = reference / "outlines" / "vol_01_全书" / "story_arcs"
            vol.mkdir(parents=True)
            (vol / "arc_001_ch001_002.md").write_text("# 情节", encoding="utf-8")
        return base

    def test_completed_reference_can_be_listed_and_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_workspace(root, "历史作品", complete=True)
            target = self._make_workspace(root, "新作品", complete=False)
            (target / "reference" / "old.txt").write_text("old", encoding="utf-8")
            store = WorkspaceStore(root)

            items = store.reference_library("新作品")
            self.assertEqual([item["workspace"] for item in items], ["历史作品"])
            self.assertEqual(items[0]["chapter_count"], 2)

            result = store.apply_reference_from_workspace("新作品", "历史作品")
            self.assertTrue(result["applied"])
            self.assertFalse((target / "reference" / "old.txt").exists())
            self.assertTrue((target / "reference" / "chapter_cards" / "chapter_0001.json").is_file())
            summary = store.summary("新作品")
            self.assertTrue(summary["reference"]["is_complete"])
            self.assertEqual(summary["reference"]["processed_chapter_count"], 2)

    def test_clear_reference_analysis_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._make_workspace(root, "作品", complete=True)
            store = WorkspaceStore(root)

            result = store.clear_reference_analysis("作品")
            self.assertTrue(result["source_preserved"])
            self.assertTrue((base / "reference" / "sample_novel.txt").is_file())
            self.assertFalse((base / "reference" / "chapter_cards").exists())
            self.assertFalse((base / "reference" / "analysis_state.json").exists())
            self.assertFalse((base / "reference" / "import_state.json").exists())
            self.assertTrue((base / "reference" / "chapters").is_dir())
            self.assertEqual(list((base / "reference" / "chapters").iterdir()), [])
            summary = store.summary("作品")
            self.assertFalse(summary["reference"]["is_complete"])
            self.assertEqual(summary["reference"]["processed_chapter_count"], 0)


if __name__ == "__main__":
    unittest.main()
