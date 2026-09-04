"""Verified self-update support for the frozen Windows PikachuNovel executable."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from webui.update_checker import _version_key
from webui.version import APP_VERSION, UPDATE_API_URL, UPDATE_REPOSITORY


EXECUTABLE_ASSET = "PikachuNovel.exe"
CHECKSUM_ASSET = f"{EXECUTABLE_ASSET}.sha256"
TRUSTED_DOWNLOAD_PREFIX = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/"
MAX_EXECUTABLE_BYTES = 700 * 1024 * 1024
MAX_CHECKSUM_BYTES = 8 * 1024
_UPDATE_LOCK = threading.Lock()


class AutoUpdateError(RuntimeError):
    """A user-facing error that leaves the current executable untouched."""


def auto_update_capability() -> dict[str, Any]:
    if os.name != "nt":
        return {"auto_update_supported": False, "auto_update_reason": "自动替换仅支持 Windows。"}
    if not getattr(sys, "frozen", False):
        return {
            "auto_update_supported": False,
            "auto_update_reason": "当前是源码/Python 运行模式，请使用 pip 或 Git 更新。",
        }
    executable = Path(sys.executable)
    if executable.suffix.lower() != ".exe" or not executable.is_file():
        return {"auto_update_supported": False, "auto_update_reason": "未识别到可更新的打包 EXE。"}
    return {"auto_update_supported": True, "auto_update_reason": ""}


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "PikachuNovel-auto-updater",
        },
    )


def _fetch_latest_release(timeout: float = 8.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(_request(UPDATE_API_URL), timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise AutoUpdateError(f"无法获取最新版本信息：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise AutoUpdateError("GitHub 最新发布信息无效。")
    return payload


def _release_version(release: dict[str, Any]) -> str:
    tag = str(release.get("tag_name") or "").strip()
    version = tag[1:] if tag.lower().startswith("v") else tag
    if not _version_key(version):
        raise AutoUpdateError("最新 Release 的版本号格式无效。")
    return version


def _trusted_asset_url(asset: dict[str, Any], expected_name: str) -> str:
    if str(asset.get("name") or "") != expected_name:
        return ""
    url = str(asset.get("browser_download_url") or "").strip()
    if not url.startswith(TRUSTED_DOWNLOAD_PREFIX):
        raise AutoUpdateError(f"{expected_name} 的下载地址不是受信任的 GitHub Release 地址。")
    return url


def _select_release_assets(release: dict[str, Any]) -> tuple[str, str]:
    executable_url = ""
    checksum_url = ""
    assets = release.get("assets")
    if not isinstance(assets, list):
        assets = []
    for raw_asset in assets:
        if not isinstance(raw_asset, dict):
            continue
        name = str(raw_asset.get("name") or "")
        if name == EXECUTABLE_ASSET:
            executable_url = _trusted_asset_url(raw_asset, EXECUTABLE_ASSET)
        elif name == CHECKSUM_ASSET:
            checksum_url = _trusted_asset_url(raw_asset, CHECKSUM_ASSET)
    if not executable_url or not checksum_url:
        raise AutoUpdateError(
            f"最新 Release 必须同时包含 {EXECUTABLE_ASSET} 和 {CHECKSUM_ASSET}，无法安全自动更新。"
        )
    return executable_url, checksum_url


def _parse_checksum(text: str) -> str:
    for line in str(text or "").splitlines():
        matched = re.fullmatch(
            rf"([0-9a-fA-F]{{64}})\s+\*?{re.escape(EXECUTABLE_ASSET)}",
            line.strip(),
        )
        if matched:
            return matched.group(1).lower()
    raise AutoUpdateError("SHA256 校验文件中没有 PikachuNovel.exe 的有效哈希。")


def _read_checksum(url: str, timeout: float = 12.0) -> str:
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            payload = response.read(MAX_CHECKSUM_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise AutoUpdateError(f"下载 SHA256 校验文件失败：{exc}") from exc
    if len(payload) > MAX_CHECKSUM_BYTES:
        raise AutoUpdateError("SHA256 校验文件异常过大。")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AutoUpdateError("SHA256 校验文件不是有效的 ASCII 文本。") from exc
    return _parse_checksum(text)


def _download_executable(url: str, target: Path, expected_sha256: str, timeout: float = 120.0) -> int:
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response, target.open("wb") as handle:
            headers = getattr(response, "headers", {})
            raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
            if raw_length:
                try:
                    content_length = int(raw_length)
                except (TypeError, ValueError):
                    content_length = 0
                if content_length > MAX_EXECUTABLE_BYTES:
                    raise AutoUpdateError("更新包体积异常，已拒绝下载。")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_EXECUTABLE_BYTES:
                    raise AutoUpdateError("更新包超过允许的最大体积。")
                handle.write(chunk)
                digest.update(chunk)
    except AutoUpdateError:
        target.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        target.unlink(missing_ok=True)
        raise AutoUpdateError(f"下载新版本失败：{exc}") from exc
    if total == 0:
        target.unlink(missing_ok=True)
        raise AutoUpdateError("下载到的更新包为空。")
    actual = digest.hexdigest().lower()
    if actual != expected_sha256.lower():
        target.unlink(missing_ok=True)
        raise AutoUpdateError("新版本 SHA256 校验失败，已取消更新。")
    return total


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _ensure_target_writable(target: Path) -> None:
    if not target.is_file():
        raise AutoUpdateError("当前 EXE 文件不存在，无法自动替换。")
    probe = target.parent / f".pikachunovel-update-{os.getpid()}.probe"
    try:
        with probe.open("xb") as handle:
            handle.write(b"ok")
    except OSError as exc:
        raise AutoUpdateError("当前程序目录没有写入权限，请手动下载新版本替换 EXE。") from exc
    finally:
        probe.unlink(missing_ok=True)


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _replacement_script(target: Path, staged: Path, wait_pid: int, restart_args: str) -> str:
    return f'''$ErrorActionPreference = "Stop"
$Target = {_ps_literal(str(target))}
$Staged = {_ps_literal(str(staged))}
$Backup = "$Target.old"
$WaitPid = {int(wait_pid)}
$Arguments = {_ps_literal(restart_args)}
$Log = Join-Path $env:TEMP "PikachuNovel-update.log"
$ReplacementDone = $false

function Write-UpdateLog([string]$Message) {{
    Add-Content -LiteralPath $Log -Value ("{{0}} {{1}}" -f (Get-Date -Format o), $Message) -Encoding UTF8
}}

function Start-PikachuNovel {{
    if ([string]::IsNullOrWhiteSpace($Arguments)) {{
        return Start-Process -FilePath $Target -PassThru
    }}
    return Start-Process -FilePath $Target -ArgumentList $Arguments -PassThru
}}

function Restore-Backup {{
    if (-not (Test-Path -LiteralPath $Backup)) {{ return }}
    $Deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $Deadline) {{
        try {{
            if (Test-Path -LiteralPath $Target) {{ Remove-Item -LiteralPath $Target -Force }}
            Move-Item -LiteralPath $Backup -Destination $Target -Force
            return
        }} catch {{
            Start-Sleep -Milliseconds 500
        }}
    }}
    throw "Failed to restore previous executable."
}}

try {{
    Write-UpdateLog "Waiting for current PikachuNovel process $WaitPid to exit."
    try {{ Wait-Process -Id $WaitPid -Timeout 30 -ErrorAction SilentlyContinue }} catch {{ }}

    $Deadline = (Get-Date).AddSeconds(90)
    while ($true) {{
        try {{
            if (Test-Path -LiteralPath $Backup) {{ Remove-Item -LiteralPath $Backup -Force }}
            Move-Item -LiteralPath $Target -Destination $Backup -Force
            try {{
                Move-Item -LiteralPath $Staged -Destination $Target -Force
            }} catch {{
                Move-Item -LiteralPath $Backup -Destination $Target -Force
                throw
            }}
            $ReplacementDone = $true
            break
        }} catch {{
            if ((Get-Date) -ge $Deadline) {{ throw }}
            Start-Sleep -Milliseconds 500
        }}
    }}

    Write-UpdateLog "Executable replaced. Starting the new version."
    $NewProcess = Start-PikachuNovel
    Start-Sleep -Seconds 5
    $NewProcess.Refresh()
    if ($NewProcess.HasExited) {{
        Write-UpdateLog "New version exited during startup; rolling back."
        Restore-Backup
        Start-PikachuNovel | Out-Null
        exit 2
    }}

    Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue
    Write-UpdateLog "Update completed successfully."
    $ScriptDir = Split-Path -Parent $PSCommandPath
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ScriptDir -Force -ErrorAction SilentlyContinue
    exit 0
}} catch {{
    Write-UpdateLog ("Update failed: " + $_.Exception.Message)
    if ($ReplacementDone -and (Test-Path -LiteralPath $Backup)) {{
        try {{
            Restore-Backup
            Start-PikachuNovel | Out-Null
            Write-UpdateLog "Previous version restored and restarted."
        }} catch {{
            Write-UpdateLog ("Rollback failed: " + $_.Exception.Message)
        }}
    }}
    exit 1
}}
'''


def _launch_replacer(script_path: Path) -> None:
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script_path),
            ],
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise AutoUpdateError(f"无法启动 Windows 更新辅助进程：{exc}") from exc


def prepare_auto_update() -> dict[str, Any]:
    capability = auto_update_capability()
    if not capability["auto_update_supported"]:
        raise AutoUpdateError(str(capability["auto_update_reason"]))
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise AutoUpdateError("另一个更新操作正在进行中。")

    target = Path(sys.executable).resolve()
    staged: Path | None = None
    update_dir: Path | None = None
    helper_started = False
    try:
        _ensure_target_writable(target)
        release = _fetch_latest_release()
        version = _release_version(release)
        if _version_key(version) <= _version_key(APP_VERSION):
            raise AutoUpdateError("当前已经是最新版本。")
        executable_url, checksum_url = _select_release_assets(release)
        expected_sha256 = _read_checksum(checksum_url)

        update_dir = Path(tempfile.mkdtemp(prefix="PikachuNovel-update-"))
        downloaded = update_dir / EXECUTABLE_ASSET
        size = _download_executable(executable_url, downloaded, expected_sha256)

        staged = target.parent / f".{target.name}.update-{version}-{os.getpid()}.tmp"
        staged.unlink(missing_ok=True)
        shutil.copyfile(downloaded, staged)
        if _sha256_file(staged) != expected_sha256:
            raise AutoUpdateError("复制到程序目录后的更新包 SHA256 校验失败。")
        downloaded.unlink(missing_ok=True)

        restart_args = subprocess.list2cmdline(sys.argv[1:])
        script_path = update_dir / "apply-update.ps1"
        script_path.write_text(
            _replacement_script(target, staged, os.getpid(), restart_args),
            encoding="utf-8-sig",
        )
        _launch_replacer(script_path)
        helper_started = True
        return {
            "scheduled": True,
            "latest_version": version,
            "bytes": size,
            "message": f"PikachuNovel {version} 已下载并通过 SHA256 校验，程序将自动重启。",
        }
    finally:
        _UPDATE_LOCK.release()
        if not helper_started:
            if staged is not None:
                staged.unlink(missing_ok=True)
            if update_dir is not None:
                shutil.rmtree(update_dir, ignore_errors=True)