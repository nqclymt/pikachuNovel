import tempfile
import unittest
from pathlib import Path

from core.text_encoding import decode_text_bytes, read_text_file
from webui.task_runner import WorkspaceStore


class TextEncodingTests(unittest.TestCase):
    SAMPLE = "第一章 测试\n这是中文内容。"

    def test_decodes_common_chinese_encodings(self):
        cases = (
            (self.SAMPLE.encode("utf-8-sig"), "UTF-8 with BOM"),
            (self.SAMPLE.encode("utf-16"), "UTF-16 LE"),
            (self.SAMPLE.encode("utf-32"), "UTF-32 LE"),
            (self.SAMPLE.encode("gb18030"), "GB18030/GBK"),
        )
        for raw, expected_encoding in cases:
            with self.subTest(encoding=expected_encoding):
                text, encoding = decode_text_bytes(raw)
                self.assertEqual(text, self.SAMPLE)
                self.assertEqual(encoding, expected_encoding)
                self.assertFalse(text.startswith("\ufeff"))

    def test_read_text_file_and_workspace_preview_detect_gbk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "demo"
            workspace.mkdir()
            source = workspace / "guide.txt"
            source.write_bytes(self.SAMPLE.encode("gb18030"))

            text, encoding = read_text_file(source)
            preview = WorkspaceStore(root).read_file("demo", "guide.txt")

            self.assertEqual(text, self.SAMPLE)
            self.assertEqual(encoding, "GB18030/GBK")
            self.assertEqual(preview["content"], self.SAMPLE)


if __name__ == "__main__":
    unittest.main()
