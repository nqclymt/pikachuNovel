import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from training.outline_builder import split_chapters


class OutlineBuilderChapterDetectionTests(unittest.TestCase):
    def _write(self, directory, content):
        path = Path(directory) / "novel.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_short_single_chapter_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "第1章 开始\n" + "正文内容。" * 30)

            _volumes, chapters = split_chapters(path)

            self.assertEqual(len(chapters), 1)

    def test_large_single_chapter_is_stopped_before_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "第1章 开始\n" + "正文内容。" * 10_000)

            with self.assertRaisesRegex(ValueError, "只识别到 1 章"):
                split_chapters(path)

    def test_large_file_without_detectable_chapters_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "没有标准章节标题。\n" + "正文内容。" * 10_000)

            with self.assertRaisesRegex(ValueError, "未识别到章节"):
                split_chapters(path)

    def test_large_single_chapter_can_be_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "第1章 开始\n" + "正文内容。" * 10_000)

            with patch.dict(os.environ, {"HARNESS_NOVEL_ALLOW_SINGLE_CHAPTER": "1"}):
                _volumes, chapters = split_chapters(path)

            self.assertEqual(len(chapters), 1)

    def test_large_multi_chapter_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "第1章 开始\n" + "第一章内容。" * 5_000
                + "\n第2章 继续\n" + "第二章内容。" * 5_000,
            )

            _volumes, chapters = split_chapters(path)

            self.assertEqual(len(chapters), 2)


if __name__ == "__main__":
    unittest.main()
