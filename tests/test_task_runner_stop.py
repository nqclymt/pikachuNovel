import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.task_runner import TaskManager


class _Store:
    def __init__(self, root):
        self.root = Path(root)

    def workspace_path(self, workspace):
        return self.root / workspace


class TaskRunnerStopTests(unittest.TestCase):
    def test_frozen_app_uses_cli_dispatcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)

            with patch("webui.task_runner.sys.frozen", True, create=True):
                command = manager._build_command("workspace_init", "demo", {})

            self.assertEqual(command, [sys.executable, "--cli", "init", "demo"])

    def test_running_cli_task_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)
            command = [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(30)",
            ]
            with patch.object(manager, "_build_command", return_value=command):
                task = manager.create("workspace_init", "demo")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if task.status == "running" and task.id in manager._processes:
                    break
                time.sleep(0.02)
            else:
                self.fail("task did not start")

            stopped = manager.stop(task.id)
            self.assertEqual(stopped["status"], "stopping")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                task.status != "stopped"
                or task.workspace in manager._active_workspaces
            ):
                time.sleep(0.02)
            self.assertEqual(task.status, "stopped")
            self.assertIn("已经写入的内容均已保留", task.message)
            self.assertNotIn(task.workspace, manager._active_workspaces)


if __name__ == "__main__":
    unittest.main()
