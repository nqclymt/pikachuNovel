import os
import tempfile
import unittest
from types import SimpleNamespace

from training.adaptive_builder import _load_mechanics_decision_assets


class MechanicsDecisionContextTests(unittest.TestCase):
    def test_auto_mechanics_context_is_bounded_without_losing_core_design(self):
        with tempfile.TemporaryDirectory() as root:
            story_design = os.path.join(root, "story_design")
            os.makedirs(story_design, exist_ok=True)

            rough = "核心玩法：身份成长与势力博弈。\n" + ("粗略大纲关键内容。" * 300)
            long_mainline = "长线主线：主角逐级成长。\n" + ("长线推进内容。" * 500)
            stage_lines = ["# 舞台1：起点", "开局关键事件。"]
            stage_lines.extend("普通剧情细节填充。" for _ in range(30000))
            stage_lines.extend([
                "## 中期机制信号",
                "主角突破境界后需要持续追踪修为、资源、技能和伤势状态。",
                "该状态影响后续资源消耗与战斗结果。",
                "# 舞台17：终局",
                "终局完成最终身份变化。",
            ])
            stage_roadmap = "\n".join(stage_lines)

            for name, content in {
                "rough_outline.md": rough,
                "long_mainline.md": long_mainline,
                "stage_roadmap.md": stage_roadmap,
                "stage_outline.md": "阶段粗纲不应被自动重复拼入。" * 50000,
            }.items():
                with open(os.path.join(story_design, name), "w", encoding="utf-8") as handle:
                    handle.write(content)

            ws = SimpleNamespace(file_system=root)
            assets = _load_mechanics_decision_assets(ws)

            self.assertIn("核心玩法：身份成长与势力博弈。", assets["core_gameplay"])
            self.assertIn("长线主线：主角逐级成长。", assets["long_mainline"])
            self.assertIn("# 舞台1：起点", assets["stage_roadmap"])
            self.assertIn("# 舞台17：终局", assets["stage_roadmap"])
            self.assertIn("持续追踪修为、资源、技能和伤势状态", assets["stage_roadmap"])
            self.assertLess(len(assets["stage_roadmap"]), 18200)
            self.assertNotIn("阶段粗纲不应被自动重复拼入", assets["core_gameplay"])
            self.assertNotIn("阶段粗纲不应被自动重复拼入", assets["character_arcs"])


if __name__ == "__main__":
    unittest.main()
