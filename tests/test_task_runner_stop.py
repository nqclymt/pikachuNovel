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
    def test_stop_cleans_up_descendant_that_keeps_stdout_open(self):
        """An agy-like child must not outlive the stopped Python task."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heartbeat = root / "heartbeat.txt"
            child = root / "fake_agent.py"
            child.write_text(
                "import pathlib, sys, time\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "for _ in range(400):\n"
                "    with path.open('a') as stream: stream.write('x')\n"
                "    time.sleep(.05)\n",
                encoding="utf-8",
            )
            parent = root / "parent.py"
            parent.write_text(
                "import subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                "child.wait()\n",
                encoding="utf-8",
            )
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)
            command = [sys.executable, str(parent), str(child), str(heartbeat)]
            with patch.object(manager, "_build_command", return_value=command):
                task = manager.create("workspace_init", "demo")
            try:
                deadline = time.monotonic() + 5
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(heartbeat.exists(), "descendant did not start")
                manager.stop(task.id)
                deadline = time.monotonic() + 8
                while task.workspace in manager._active_workspaces and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(task.status, "stopped")
                self.assertNotIn(task.workspace, manager._active_workspaces)
                size = heartbeat.stat().st_size
                time.sleep(.2)
                self.assertEqual(heartbeat.stat().st_size, size)
            finally:
                process = manager._processes.get(task.id)
                if process is not None:
                    from webui.task_runner import _stop_process_tree
                    _stop_process_tree(process)

    def test_child_observes_cooperative_cancel_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)
            command = [sys.executable, "-c",
                       "import os,pathlib,time; marker=pathlib.Path(os.environ['HARNESS_NOVEL_CANCEL_FILE']); "
                       "print('ready', flush=True)\n"
                       "while not marker.exists(): time.sleep(.02)\n"
                       "print('cooperative cancellation', flush=True)"]
            with patch.object(manager, "_build_command", return_value=command):
                task = manager.create("workspace_init", "demo")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if Path(task.log_path).exists() and "ready" in Path(task.log_path).read_text(encoding="utf-8"):
                    break
                time.sleep(.02)
            manager.stop(task.id)
            deadline = time.monotonic() + 5
            while task.workspace in manager._active_workspaces and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(task.status, "stopped")
            self.assertIn("cooperative cancellation", Path(task.log_path).read_text(encoding="utf-8"))

    def test_child_process_chinese_output_remains_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)
            command = [sys.executable, "-c", "print('中文日志：执行完成')"]
            with patch.object(manager, "_build_command", return_value=command):
                task = manager.create("workspace_init", "demo")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                task.status in {"queued", "running"}
                or task.id in manager._processes
            ):
                time.sleep(0.02)

            log = Path(task.log_path).read_text(encoding="utf-8")
            self.assertEqual(task.status, "succeeded")
            self.assertIn("中文日志：执行完成", log)
            self.assertNotIn("\ufffd", log)

    def test_invalid_child_output_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)
            command = [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes([255])); sys.stdout.flush()",
            ]
            with patch.object(manager, "_build_command", return_value=command):
                task = manager.create("workspace_init", "demo")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                task.status in {"queued", "running"}
                or task.id in manager._processes
            ):
                time.sleep(0.02)

            log = Path(task.log_path).read_text(encoding="utf-8")
            self.assertEqual(task.status, "failed")
            self.assertIn("UnicodeDecodeError", log)
            self.assertNotIn("\ufffd", log)

    def test_provider_timeout_has_actionable_task_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(_Store(root), root / "tasks", uploads=None)
            command = [
                sys.executable,
                "-c",
                "print('HTTP 524: origin_response_timeout'); raise SystemExit(1)",
            ]
            with patch.object(manager, "_build_command", return_value=command):
                task = manager.create("workspace_init", "demo")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                task.status in {"queued", "running"}
                or task.id in manager._processes
            ):
                time.sleep(0.02)

            self.assertEqual(task.status, "failed")
            self.assertIn("模型服务商响应超时", task.message)
            self.assertIn("已保留进度", task.message)

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
