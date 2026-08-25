import json
import tempfile
import unittest
from pathlib import Path

from training.reference_analyzer import ReferenceAnalyzer


class FailingOverallOutlineLLM:
    def __init__(self):
        self.calls = 0

    def generate(self, _prompt, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return "已生成的卷结构"
        raise RuntimeError("overall outline failed")


class ReferenceAnalyzerCheckpointTests(unittest.TestCase):
    def test_volume_structure_checkpoint_survives_overall_outline_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reference"
            volume_dir = output_dir / "outlines" / "vol_01_测试卷"
            arc_dir = volume_dir / "story_arcs"
            arc_dir.mkdir(parents=True)
            (arc_dir / "arc_001_ch001_002.md").write_text(
                "已闭合故事片段", encoding="utf-8"
            )

            llm = FailingOverallOutlineLLM()
            analyzer = ReferenceAnalyzer(
                root / "novel.txt", output_dir, llm=llm
            )
            analyzer.state = {"volumes": {}, "structure": {}}
            specs = [{
                "index": 1,
                "title": "测试卷",
                "directory": volume_dir,
                "directory_name": "vol_01_测试卷",
                "global_start": 1,
                "total_count": 2,
            }]

            with self.assertRaisesRegex(RuntimeError, "overall outline failed"):
                analyzer._build_structures(specs, target=2, total=2)

            state = json.loads(analyzer.state_path.read_text(encoding="utf-8"))
            self.assertTrue(state["volumes"]["1"]["structure_digest"])
            self.assertEqual(
                (volume_dir / "volume_outline.md").read_text(encoding="utf-8").strip(),
                "已生成的卷结构",
            )
            self.assertEqual(llm.calls, 2)


if __name__ == "__main__":
    unittest.main()
