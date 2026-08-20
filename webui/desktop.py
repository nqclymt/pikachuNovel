"""Desktop window for the HarnessNovel web workspace."""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
from contextlib import closing
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _available_port(host: str, preferred: int = DEFAULT_PORT) -> int:
    """Return the preferred port when possible, otherwise an ephemeral port."""
    for port in (preferred, 0):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise RuntimeError("无法为桌面窗口分配本地端口。")


def _wait_until_ready(
    host: str,
    port: int,
    server: Any,
    timeout: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "should_exit", False):
            break
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"桌面服务启动超时：http://{host}:{port}")


def run_desktop(
    workspace_root: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    debug: bool = False,
) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "桌面窗口依赖 pywebview 未安装。请使用当前 Python 执行："
            "python -m pip install --upgrade \"harnessNovel[desktop]\""
        ) from exc
    try:
        import uvicorn
        from webui.app import create_app
    except ImportError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise RuntimeError(
            f"程序依赖导入失败（{missing}）。请使用当前 Python 重新安装或更新 HarnessNovel。"
        ) from exc

    selected_port = _available_port(host, port)
    config = uvicorn.Config(
        create_app(workspace_root=workspace_root),
        host=host,
        port=selected_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server_thread = threading.Thread(
        target=server.run,
        name="harness-novel-desktop-server",
        daemon=True,
    )
    server_thread.start()

    try:
        _wait_until_ready(host, selected_port, server)
        window = webview.create_window(
            "HarnessNovel 小说工作台",
            f"http://{host}:{selected_port}",
            width=1440,
            height=920,
            min_size=(1024, 700),
        )

        def stop_server() -> None:
            server.should_exit = True

        window.events.closed += stop_server
        webview.start(debug=debug)
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 HarnessNovel 桌面工作台")
    parser.add_argument("--host", default=DEFAULT_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="本地服务端口")
    parser.add_argument("--workspace-root", help="工作区根目录")
    parser.add_argument("--debug", action="store_true", help="启用桌面窗口调试模式")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        run_desktop(
            workspace_root=args.workspace_root,
            host=args.host,
            port=args.port,
            debug=args.debug,
        )
    except RuntimeError as exc:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, str(exc), "HarnessNovel 启动失败", 0x10)
        raise SystemExit(f"错误：{exc}") from exc


if __name__ == "__main__":
    main()
