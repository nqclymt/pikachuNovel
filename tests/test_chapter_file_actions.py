import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webui.task_runner import WorkspaceStore


class ChapterFileActionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = WorkspaceStore(self.root)
        self.workspace = "demo"
        self.chapter = self.root / self.workspace / "file_system" / "chapters" / "vol_01" / "001_第1章.md"
        self.chapter.parent.mkdir(parents=True, exist_ok=True)
        self.chapter.write_text("第一章正文", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_chapter_file_path_accepts_generated_chapter(self):
        resolved = self.store.chapter_file_path(
            self.workspace, "file_system/chapters/vol_01/001_第1章.md"
        )
        self.assertEqual(resolved, self.chapter.resolve())

    def test_chapter_file_path_rejects_non_chapter_files(self):
        with self.assertRaises(ValueError):
            self.store.chapter_file_path(self.workspace, "file_system/story_design/stage_outline.md")
        with self.assertRaises(ValueError):
            self.store.chapter_file_path(self.workspace, "../outside.md")

    @unittest.skipUnless(os.name == "nt", "Windows Explorer action")
    def test_reveal_chapter_uses_explorer_selection(self):
        with mock.patch("webui.task_runner.subprocess.Popen") as popen:
            result = self.store.reveal_chapter_file(
                self.workspace, "file_system/chapters/vol_01/001_第1章.md"
            )
        self.assertEqual(result["revealed"], "true")
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "explorer.exe")
        self.assertTrue(args[1].startswith("/select,"))


if __name__ == "__main__":
    unittest.main()
