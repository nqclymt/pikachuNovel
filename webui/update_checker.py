"""Small, bounded GitHub Release update checker."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from webui.version import APP_VERSION, UPDATE_API_URL, UPDATE_RELEASES_URL


def _version_key(value: str) -> tuple[int, ...]:
    """Compare release versions without accepting arbitrary tag text."""
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?", str(value or "").strip(), re.I)
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def _release_version(release: dict[str, Any]) -> str:
    tag = str(release.get("tag_name") or "").strip()
    return tag[1:] if tag.lower().startswith("v") else tag


def check_latest_release(timeout: float = 3.0) -> dict[str, Any]:
    """Return update metadata; network failures are represented, never raised."""
    result: dict[str, Any] = {
        "current_version": APP_VERSION,
        "latest_version": APP_VERSION,
        "update_available": False,
        "release_url": UPDATE_RELEASES_URL,
        "release_name": "",
        "published_at": "",
        "error": "",
    }
    request = urllib.request.Request(
        UPDATE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "PikachuNovel-update-checker"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
            return result
        latest = _release_version(payload)
        if not _version_key(latest):
            return result
        result.update(
            {
                "latest_version": latest,
                "update_available": _version_key(latest) > _version_key(APP_VERSION),
                "release_url": str(payload.get("html_url") or UPDATE_RELEASES_URL),
                "release_name": str(payload.get("name") or ""),
                "published_at": str(payload.get("published_at") or ""),
            }
        )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        result["error"] = str(exc)
    return result
