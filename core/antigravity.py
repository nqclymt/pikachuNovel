"""Official agy headless protocol, without credential access or API emulation.

Protocol: https://www.antigravity.google/docs/cli/headless/
Agent schema: https://www.antigravity.google/docs/subagents/
"""

import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from core.llm_provider import LLMExecutionBlocked
from core.cli_scheduler import (
    SchedulerPaused, check_cancelled, pause_scheduler, resume_scheduler,
    scheduler_slot, scheduler_status,
)

_AGENT_NAME = "pikachu-novel"
_AGENT_SPEC = """---
name: pikachu-novel
description: Chinese fiction writing, planning and targeted editing for PikachuNovel.
tools: []
mainAgent: true
subagent: false
commandExecutionPolicy: "off"
mcpServers: []
skills: []
plugins: []
---
你是小说创作助手。外层应用负责规划、长期记忆和文件保存。
仅根据本轮提供的设定、人物状态、上下文和任务生成结果，不假设其他会话的事实。
不要使用任何工具，不要访问文件、运行命令、浏览网页或委派子任务。
严格遵循本轮的输出格式：要求 JSON 时只输出 JSON；要求正文时只输出正文。
不要添加说明、实现计划、完成总结或写作建议。保留人物动机、视角、时间线和必写事件。
修改已有正文时遵循指定范围，不自行扩写未要求的章节或改变既定情节。
"""
_ERROR_MESSAGES = {
    "missing": "未找到官方 agy CLI。请安装 Antigravity CLI，或在设置中填写可执行文件路径。",
    "auth": "Antigravity 登录已失效或尚未登录。请在设置中点击“启动/登录 agy”完成 Google 登录，再点击恢复调度。",
    "quota": "Antigravity 额度或调用频率已受限。已暂停后续请求，请等待额度恢复后点击恢复调度。",
    "timeout": "Antigravity 请求超时，已终止本次调用；未接收不完整正文。",
    "protocol": "Antigravity 返回了不兼容或不完整的结果；请更新官方 CLI 后重试。",
    "process": "Antigravity 调用失败。请在设置中启动 agy，检查模型、Agent 和网络配置后重试。",
}


class AntigravityError(LLMExecutionBlocked):
    def __init__(self, category, message=None):
        self.category = category
        super().__init__(message or _ERROR_MESSAGES.get(category, _ERROR_MESSAGES["process"]))


def _notify(callback, message):
    if callback:
        try:
            callback(message)
        except Exception:
            pass


def _resolve_cli(cli_path=""):
    configured = str(cli_path or "").strip()
    if configured:
        found = shutil.which(configured) or str(Path(configured).expanduser())
    else:
        found = shutil.which("agy.exe" if os.name == "nt" else "agy")
        if not found:
            default = (Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "agy" / "bin" / "agy.exe"
                       if os.name == "nt" else Path.home() / ".local" / "bin" / "agy")
            found = str(default)
    path = Path(found).expanduser().resolve()
    if not path.is_file():
        raise AntigravityError("missing")
    # The official installer provides a native binary. Do not let Windows silently
    # invoke cmd.exe for a batch shim, where model/agent strings become shell input.
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise AntigravityError("missing", "请填写官方 agy.exe 的路径；不支持 .cmd、.bat 或 PowerShell 包装脚本。")
    if os.name != "nt" and not os.access(str(path), os.X_OK):
        raise AntigravityError("missing")
    return str(path)


def _process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _system_proxy():
    """Return the OS HTTPS/HTTP proxy without reading browser credentials."""
    try:
        if os.name == "nt" and hasattr(urllib.request, "getproxies_registry"):
            proxies = urllib.request.getproxies_registry()
        else:
            proxies = urllib.request.getproxies()
    except (OSError, ValueError):
        return ""
    if not isinstance(proxies, dict):
        return ""
    return str(proxies.get("https") or proxies.get("http") or proxies.get("all") or "").strip()


def _agy_environment():
    """Build an account-login environment and bridge the desktop system proxy."""
    env = os.environ.copy()
    # This backend intentionally uses the CLI's signed-in Google account.
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
        env.pop(key, None)

    explicit = str(env.get("ANTIGRAVITY_PROXY") or "").strip()
    inherited = next((str(env.get(key) or "").strip() for key in (
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"
    ) if str(env.get(key) or "").strip()), "")
    proxy = explicit or inherited
    source = "configured" if proxy else ""
    if not proxy:
        proxy = _system_proxy()
        source = "system" if proxy else ""
    if proxy and "://" not in proxy:
        proxy = f"http://{proxy}"
    if proxy and ("\r" in proxy or "\n" in proxy or "\x00" in proxy):
        proxy = ""
        source = ""
    if proxy and (explicit or not inherited):
        # Go's HTTP transport honors these variables. Set both cases because
        # third-party launchers around agy are not guaranteed to use Go's casing.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
    return env, source


def _apple_script_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def launch_antigravity(cli_path=""):
    """Open the interactive official CLI in a visible terminal for login/setup."""
    path = _resolve_cli(cli_path)
    env, proxy_source = _agy_environment()
    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                [path], cwd=str(Path.home()), env=env, close_fds=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        elif sys.platform == "darwin":
            assignments = " ".join(
                f"{key}={shlex.quote(env[key])}"
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if env.get(key)
            )
            command = "unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GEMINI_BASE_URL; "
            command += f"env {assignments} {shlex.quote(path)}" if assignments else shlex.quote(path)
            script = (
                'tell application "Terminal"\n'
                "activate\n"
                f"do script {_apple_script_string(command)}\n"
                "end tell"
            )
            proc = subprocess.Popen(
                ["/usr/bin/osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        else:
            terminals = (
                ("x-terminal-emulator", ["-e", path]),
                ("gnome-terminal", ["--", path]),
                ("konsole", ["-e", path]),
                ("xterm", ["-e", path]),
            )
            selected = next(((binary, args) for binary, args in terminals if shutil.which(binary)), None)
            if not selected:
                raise AntigravityError("process", "未找到可用的图形终端，请在系统终端中运行 agy。")
            proc = subprocess.Popen(
                selected[1], cwd=str(Path.home()), env=env,
                start_new_session=True, close_fds=True,
            )
    except AntigravityError:
        raise
    except OSError as error:
        raise AntigravityError("process", "无法打开 agy 登录窗口，请检查终端和执行权限。") from error
    return {
        "opened": True,
        "path": path,
        "pid": int(proc.pid),
        "proxy": proxy_source,
    }


class _ProcessJob:
    """Keep Windows descendants contained even if the CLI or its caller crashes."""

    def __init__(self, proc):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot create CLI process job")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "Cannot configure CLI process job")
        if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(proc._handle))):
            kernel.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "Cannot contain CLI child processes")
        self.handle = handle
        self.kernel = kernel

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _terminate_tree(proc):
    if os.name == "nt":
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def probe_antigravity(cli_path=""):
    try:
        path = _resolve_cli(cli_path)
    except AntigravityError as error:
        return {"installed": False, "path": "", "version": "", "error": str(error)}
    try:
        proc = subprocess.Popen([path, "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_process_options())
        job = None
        try:
            job = _ProcessJob(proc)
            try:
                stdout, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                return {"installed": True, "path": path, "version": "", "error": "agy 版本检测超时。"}
        finally:
            if job:
                job.close()
            _terminate_tree(proc)
            proc.stdout.close()
            proc.stderr.close()
        # Expose only a version identifier, never arbitrary terminal diagnostics.
        match = re.search(rb"\b(?:v)?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?\b", stdout[:4096])
        version = match.group().decode("ascii") if match else ""
        error = "" if proc.returncode == 0 and version else "无法读取 agy 版本，请确认这是官方 Headless CLI。"
        return {"installed": True, "path": path, "version": version, "error": error}
    except OSError:
        return {"installed": False, "path": path, "version": "", "error": "agy 无法启动，请检查文件和执行权限。"}


def _failure_category(text):
    text = text.lower()
    if re.search(r"authentication required|unauthenticated|not authenticated|not (?:logged|signed) in|login required|sign.in required|please log in|invalid_grant|invalid credentials|token.*expired|expired.*token|gemini_api_key.*(?:not set|missing)|\b401\b", text):
        return "auth"
    if re.search(r"resource.?exhausted|quota|rate.?limit|too many requests|insufficient (?:credits|balance)|credits?.*(?:exhausted|insufficient)|\b429\b", text):
        return "quota"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "process"


def _run_process(argv, prompt, cwd, timeout, is_json, cancel_event, status_callback):
    env, _ = _agy_environment()
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_process_options())
    except OSError as error:
        raise AntigravityError("process", "无法启动 agy，请检查 CLI 路径和执行权限。") from error
    try:
        job = _ProcessJob(proc)
    except OSError as error:
        _terminate_tree(proc)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
        raise AntigravityError("process", "无法为 Antigravity 创建受控进程，请检查系统进程权限后重试。") from error
    events = queue.Queue(maxsize=64)
    stop = threading.Event()
    workers = []

    def put(kind, data):
        while not stop.is_set():
            try:
                events.put((kind, data), timeout=0.1)
                return
            except queue.Full:
                pass

    def read_pipe(pipe, kind):
        try:
            while not stop.is_set():
                chunk = pipe.read1(65536)
                if not chunk:
                    break
                put(kind, chunk)
        except (OSError, ValueError):
            pass
        finally:
            put(kind, None)

    def write_prompt():
        try:
            payload = {"event": "user", "message": {"content": prompt}}
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    for kind, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        thread = threading.Thread(target=read_pipe, args=(pipe, kind), daemon=True)
        thread.start()
        workers.append(thread)
    deadline = time.monotonic() + timeout
    buffer = b""
    diagnostic = b""
    ended = set()
    result = None
    initialized = False
    output_bytes = 0
    announced = False

    def consume(line):
        nonlocal result, initialized, announced
        if not line.strip():
            return
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AntigravityError("protocol") from None
        if not isinstance(event, dict):
            raise AntigravityError("protocol")
        kind = event.get("event")
        if kind == "init":
            details = event.get("init")
            if initialized or not isinstance(details, dict) or not isinstance(details.get("tools"), list):
                raise AntigravityError("protocol")
            # Check before submitting the novel prompt. Custom agents must use the
            # same output-only contract, regardless of global CLI permission rules.
            if details["tools"]:
                raise AntigravityError("protocol", "所选 Antigravity Agent 启用了工具。小说输出模式要求 Agent 配置 tools: []；本次未提交正文。")
            initialized = True
            thread = threading.Thread(target=write_prompt, daemon=True)
            thread.start()
            workers.append(thread)
            _notify(status_callback, "Antigravity 已就绪，正在生成结果。")
        elif kind == "result":
            if result is not None or not isinstance(event.get("result"), dict):
                raise AntigravityError("protocol")
            result = event["result"]
        elif kind == "step_update":
            details = event.get("step_update") or {}
            if not isinstance(details, dict):
                raise AntigravityError("protocol")
            if details.get("step_type") == "tool":
                raise AntigravityError("protocol", "Antigravity 尝试执行工具操作，已终止本次输出任务。")
            if not announced and details.get("step_type") == "agent_response":
                _notify(status_callback, "Antigravity 正在输出，等待完整结果确认后保存。")
                announced = True

    try:
        while len(ended) < 2 or proc.poll() is None:
            check_cancelled(cancel_event)
            if time.monotonic() > deadline:
                raise AntigravityError("timeout")
            try:
                kind, chunk = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                ended.add(kind)
                continue
            if kind == "stderr":
                diagnostic = (diagnostic + chunk)[-16384:]
                continue
            output_bytes += len(chunk)
            if output_bytes > 32 * 1024 * 1024:
                raise AntigravityError("protocol", "Antigravity 输出超过安全大小限制，已终止本次请求。")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                consume(line)
        if buffer:
            consume(buffer)
        check_cancelled(cancel_event)
        diagnostic_text = diagnostic.decode("utf-8", errors="replace")
        if proc.returncode != 0 or (result and result.get("status") != "SUCCESS"):
            detail = diagnostic_text + " " + str((result or {}).get("error", ""))
            category = _failure_category(detail)
            if "gemini_api_key" in detail.lower() and category == "auth":
                raise AntigravityError("auth", "此后端使用 Google 登录。请在 agy 设置中取消 modelProvider: gemini，完成登录后点击恢复调度。")
            raise AntigravityError(category)
        if not initialized or not result or result.get("status") != "SUCCESS":
            # Auth can fail before init and before the structured protocol starts.
            category = _failure_category(diagnostic_text)
            raise AntigravityError(category if category in ("auth", "quota") else "protocol")
        if is_json and "structured_output" in result:
            response = result["structured_output"]
            if not isinstance(response, dict):
                raise AntigravityError("protocol")
            return json.dumps(response, ensure_ascii=False)
        response = result.get("response")
        if not isinstance(response, str) or not response.strip():
            raise AntigravityError("protocol")
        if is_json:
            try:
                if not isinstance(json.loads(response), dict):
                    raise ValueError("object required")
            except ValueError:
                raise AntigravityError("protocol", "Antigravity 未返回有效 JSON 对象，已拒绝保存。") from None
        return response
    finally:
        stop.set()
        job.close()
        _terminate_tree(proc)
        for thread in workers:
            thread.join(timeout=1)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass


def run_antigravity(prompt, *, model="", cli_path="", cli_agent="", cli_effort="medium",
                    timeout=600, is_json=False, cancel_event=None, status_callback=None):
    check_cancelled(cancel_event)
    path = _resolve_cli(cli_path)
    effort = str(cli_effort or "medium").strip().lower()
    if effort not in ("low", "medium", "high"):
        raise AntigravityError("process", "Antigravity 推理强度仅支持 low、medium、high。")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise AntigravityError("process", "Antigravity 超时时间必须大于 0。")
    agent = str(cli_agent or _AGENT_NAME).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", agent):
        raise AntigravityError("process", "Antigravity Agent 请填写名称，不能填写路径或命令参数。")
    argv = [path, "--input-format", "stream-json", "--output-format", "stream-json",
            "--agent", agent, "--effort", effort, "--print-timeout", f"{math.ceil(timeout)}s"]
    selected_model = str(model or "").strip()
    if selected_model:
        if selected_model.startswith("-") or len(selected_model) > 256 or any(ord(c) < 32 for c in selected_model):
            raise AntigravityError("process", "Antigravity 模型请填写 agy models 中的模型名称，不能填写命令参数。")
        argv.extend(["--model", selected_model])
    if is_json:
        argv.extend(["--json-schema", '{"type":"object"}'])
    safe_callback = lambda message: _notify(status_callback, message)
    try:
        with scheduler_slot(cancel_event, safe_callback):
            try:
                _notify(status_callback, "正在启动 Antigravity 独立写作会话。")
                with tempfile.TemporaryDirectory(prefix="pikachu-agy-") as temporary:
                    cwd = Path(temporary)
                    definition = cwd / ".agents" / "agents" / _AGENT_NAME / "agent.md"
                    definition.parent.mkdir(parents=True)
                    definition.write_text(_AGENT_SPEC, encoding="utf-8")
                    return _run_process(argv, str(prompt), cwd, float(timeout), is_json,
                                        cancel_event, status_callback)
            except AntigravityError as error:
                if error.category in ("auth", "quota"):
                    pause_scheduler(error.category, str(error))
                raise
    except SchedulerPaused as error:
        raise AntigravityError(error.category, str(error)) from None
    except OSError as error:
        raise AntigravityError("process", "Antigravity 临时目录或调度状态无法读写，请检查本机目录权限后重试。") from error
