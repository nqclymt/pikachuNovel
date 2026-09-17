import json
import tempfile
import unittest
from pathlib import Path

from training.outline_builder import resegment


class FakeLLM:
    def __init__(self, fail_volume_merge_call=None):
        self.calls = []
        self.merge_calls = 0
        self.fail_volume_merge_call = fail_volume_merge_call

    def generate(self, prompt, **kwargs):
        self.calls.append(prompt)
        if "分卷边界判断专用摘要" in prompt:
            return "主要矛盾推进；阶段收束清晰；适合在本区间末尾形成卷边界。"
        if "严格按以下格式输出，每行一卷" in prompt:
            return "卷1：启程 | 第1-60章\n卷2：争锋 | 第61-120章\n卷3：终局 | 第121-180章"
        if "合并" in prompt or "卷纲" in prompt:
            self.merge_calls += 1
            if self.fail_volume_merge_call == self.merge_calls:
                raise RuntimeError("simulated connection drop")
            return f"卷纲内容{self.merge_calls}"
        return "完整大纲"


class ResegmentResumeTests(unittest.TestCase):
    def _make_outlines(self, root: Path):
        outlines = root / "outlines"
        arc_dir = outlines / "vol_01_全书" / "story_arcs"
        arc_dir.mkdir(parents=True)
        for index in range(45):
            start = index * 4 + 1
            end = start + 3
            (arc_dir / f"arc_{index + 1:03d}_ch{start:03d}_{end:03d}.md").write_text(
                f"【情节{index + 1}：第{start}-{end}章｜片段{index + 1}】\n"
                f"主要矛盾：第{start}-{end}章推进。\n地图变化与角色成长。",
                encoding="utf-8",
            )
        return outlines

    def test_resegment_resumes_after_volume_outline_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            outlines = self._make_outlines(Path(directory))
            first = FakeLLM(fail_volume_merge_call=2)
            with self.assertRaisesRegex(RuntimeError, "simulated connection drop"):
                resegment(str(outlines), llm=first)

            state = json.loads((outlines / "resegment_state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["virtual_volumes"]), 3)
            self.assertEqual(state["completed_volumes"], [1])
            self.assertEqual(len(list((outlines / ".resegment_cache").glob("chunk_*.md"))), 3)

            second = FakeLLM()
            self.assertTrue(resegment(str(outlines), llm=second))
            # 边界和3个预摘要均应复用；第二次不应再次调用这两类 prompt。
            self.assertFalse(any("分卷边界判断专用摘要" in prompt for prompt in second.calls))
            self.assertFalse(any("严格按以下格式输出，每行一卷" in prompt for prompt in second.calls))
            state = json.loads((outlines / "resegment_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "complete")
            self.assertFalse((outlines / "vol_01_全书").exists())
            volumes = [p for p in outlines.iterdir() if p.is_dir() and p.name.startswith("vol_")]
            self.assertEqual(len(volumes), 3)


if __name__ == "__main__":
    unittest.main()
