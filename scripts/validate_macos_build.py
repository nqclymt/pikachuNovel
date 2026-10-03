"""Smoke-test a frozen Mac desktop without using personal settings or LLM APIs."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此验证需要在 macOS 上运行。")
    app = args.app.resolve(strict=True)
    executable = app / "Contents" / "MacOS" / "PikachuNovel"
    resources = app / "Contents" / "Resources"
    for name in ("prose_review", "prose_local_edit", "prose_edit_verify"):
        packaged = resources / "core" / "prompts" / name / "prompt.txt"
        source = Path(__file__).resolve().parents[1] / "core" / "prompts" / name / "prompt.txt"
        if packaged.read_bytes() != source.read_bytes():
            raise RuntimeError(f"打包后的提示词与源码不一致：{name}")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    def request(path: str, body: dict | None = None) -> str:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(base_url + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            return response.read().decode("utf-8")

    with tempfile.TemporaryDirectory(prefix="pikachu-mac-test-") as directory:
        root = Path(directory)
        profile = root / "profile"
        profile.mkdir()
        env = os.environ.copy()
        env.update(HOME=str(profile), HARNESS_NOVEL_HOME=str(root / "workspaces"))
        with (root / "desktop.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(executable), "--port", str(port)],
                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 45
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"桌面程序提前退出：{process.returncode}")
                    try:
                        if json.loads(request("/api/health"))["status"] == "ok":
                            break
                    except (URLError, TimeoutError, OSError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("桌面服务启动超时。")
                    time.sleep(0.25)

                assert "/assets/workflow-presets.js" in request("/")
                assert "human_style_pipeline_revision" in request("/assets/wizard-v0.js")
                assert "WorkflowPromptPresets" in request("/assets/workflow-presets.js")
                task = json.loads(request("/api/tasks", {
                    "type": "workspace_init", "workspace": "苹果打包测试", "args": {},
                }))
                deadline = time.monotonic() + 30
                while task["status"] in {"queued", "running", "stopping"} and time.monotonic() < deadline:
                    time.sleep(0.2)
                    task = json.loads(request(f"/api/tasks/{task['id']}"))
                if task["status"] != "succeeded":
                    raise RuntimeError(f"冻结程序的后台任务失败：{task}")
                task_logs = json.loads(request(f"/api/tasks/{task['id']}/logs"))["content"]
                assert "\ufffd" not in task_logs, "后台任务日志存在乱码"
                assert "工作空间「苹果打包测试」已创建" in task_logs, "未收到后台 CLI 的中文输出"
                assert (root / "workspaces" / "苹果打包测试" / "file_system").is_dir()
                time.sleep(2)
                if process.poll() is not None:
                    raise RuntimeError("桌面程序在服务启动后异常退出。")
                print("macOS 冒烟检查通过：提示词、桌面进程、静态资源、后台 CLI、中文路径及日志。")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
