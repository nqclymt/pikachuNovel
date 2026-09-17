"""Run JavaScript behavior tests from the normal Python test-discovery entry point."""
from pathlib import Path
import shutil
import subprocess
import unittest


class WorkflowPresetFrontendTests(unittest.TestCase):
    def test_node_workflow_presets(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for frontend behavior tests")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "--test", str(root / "tests" / "test_workflow_presets.js")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
