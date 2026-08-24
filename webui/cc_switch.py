"""Read-only adapter for importing Codex providers from CC Switch."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.llm_provider import LLMProvider

try:
    import tomllib
except ImportError:  # Python 3.9/3.10 compatibility
    tomllib = None


@dataclass(frozen=True)
class CCSwitchProvider:
    id: str
    name: str
    model: str
    base_url: str
    api_key: str
    wire_api: str
    is_current: bool

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "is_current": self.is_current,
            "api_key_configured": bool(self.api_key),
            "importable": bool(self.model and self.base_url and self.api_key),
        }


def discover_cc_switch_database() -> Path | None:
    """Find the current CC Switch SQLite SSOT without modifying it."""
    candidates = [Path.home() / ".cc-switch" / "cc-switch.db"]
    legacy_home = os.getenv("HOME", "").strip()
    if legacy_home:
        candidates.append(Path(legacy_home).expanduser() / ".cc-switch" / "cc-switch.db")
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def _parse_fallback_toml(config_text: str) -> dict[str, str]:
    """Extract the small Codex subset needed on Python versions without tomllib."""
    values = {}
    section = ""
    assignments: dict[tuple[str, str], str] = {}
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        section_match = re.fullmatch(r"\[([^]]+)]", line)
        if section_match:
            section = section_match.group(1).strip()
            continue
        match = re.match(r"([A-Za-z0-9_]+)\s*=\s*(['\"])(.*?)\2", line)
        if match:
            assignments[(section, match.group(1))] = match.group(3)
    provider_name = assignments.get(("", "model_provider"), "")
    provider_section = f"model_providers.{provider_name}" if provider_name else ""
    values["model"] = assignments.get(("", "model"), "")
    values["base_url"] = (
        assignments.get((provider_section, "base_url"), "")
        or assignments.get(("", "base_url"), "")
    )
    values["wire_api"] = assignments.get((provider_section, "wire_api"), "")
    values["experimental_bearer_token"] = (
        assignments.get((provider_section, "experimental_bearer_token"), "")
        or assignments.get(("", "experimental_bearer_token"), "")
    )
    return values


def _parse_codex_config(settings: dict) -> dict[str, str]:
    config_text = settings.get("config") if isinstance(settings.get("config"), str) else ""
    if tomllib is None:
        return _parse_fallback_toml(config_text)
    try:
        parsed = tomllib.loads(config_text)
    except (TypeError, tomllib.TOMLDecodeError):
        return _parse_fallback_toml(config_text)
    provider_name = str(parsed.get("model_provider") or "").strip()
    providers = parsed.get("model_providers")
    provider = providers.get(provider_name, {}) if isinstance(providers, dict) else {}
    if not isinstance(provider, dict):
        provider = {}
    return {
        "model": str(parsed.get("model") or "").strip(),
        "base_url": str(provider.get("base_url") or parsed.get("base_url") or "").strip(),
        "wire_api": str(provider.get("wire_api") or "").strip(),
        "experimental_bearer_token": str(
            provider.get("experimental_bearer_token")
            or parsed.get("experimental_bearer_token")
            or ""
        ).strip(),
    }


def load_cc_switch_providers(database_path: Path | None = None) -> tuple[Path | None, list[CCSwitchProvider]]:
    path = database_path or discover_cc_switch_database()
    if path is None:
        return None, []
    path = path.resolve()
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        rows = connection.execute(
            "SELECT id, name, settings_config, is_current FROM providers "
            "WHERE app_type = 'codex' "
            "ORDER BY is_current DESC, COALESCE(sort_index, 999999), created_at, id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"无法读取 CC Switch 数据库：{exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()

    providers = []
    for provider_id, name, raw_settings, is_current in rows:
        try:
            settings = json.loads(raw_settings)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(settings, dict):
            continue
        codex = _parse_codex_config(settings)
        auth = settings.get("auth") if isinstance(settings.get("auth"), dict) else {}
        api_key = str(
            auth.get("OPENAI_API_KEY")
            or codex.get("experimental_bearer_token")
            or ""
        ).strip()
        model = codex.get("model", "")
        if not model:
            catalog = settings.get("modelCatalog")
            catalog_models = catalog.get("models") if isinstance(catalog, dict) else []
            if isinstance(catalog_models, list):
                for item in catalog_models:
                    if isinstance(item, dict) and (item.get("model") or item.get("id")):
                        model = str(item.get("model") or item.get("id")).strip()
                        break
        providers.append(
            CCSwitchProvider(
                id=str(provider_id),
                name=str(name),
                model=model,
                base_url=codex.get("base_url", ""),
                api_key=api_key,
                wire_api=codex.get("wire_api", ""),
                is_current=bool(is_current),
            )
        )
    return path, providers


def _base_url_candidates(base_url: str) -> list[str]:
    raw = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("CC Switch 供应商的 Base URL 无效。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CC Switch 供应商的 Base URL 不能包含凭据、查询参数或片段。")
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    if normalized.lower().endswith("/v1"):
        return [normalized]
    return [f"{normalized}/v1", normalized]


def validate_cc_switch_provider(provider: CCSwitchProvider) -> CCSwitchProvider:
    """Resolve a chat-compatible API root; nothing is persisted until this succeeds."""
    if not provider.api_key:
        raise ValueError(f"CC Switch 供应商“{provider.name}”没有 API Key。")
    if not provider.model:
        raise ValueError(f"CC Switch 供应商“{provider.name}”没有默认模型。")
    errors = []
    for base_url in _base_url_candidates(provider.base_url):
        try:
            result = LLMProvider(
                model=provider.model,
                base_url=base_url,
                api_key=provider.api_key,
                max_tokens=2,
            ).generate("仅回复 OK", max_retries=0, max_tokens=2)
            if result:
                return CCSwitchProvider(
                    id=provider.id,
                    name=provider.name,
                    model=provider.model,
                    base_url=base_url,
                    api_key=provider.api_key,
                    wire_api=provider.wire_api,
                    is_current=provider.is_current,
                )
        except Exception as exc:  # noqa: BLE001 - aggregate safe validation errors
            errors.append(f"{base_url}: {exc}")
    detail = errors[-1] if errors else "未返回内容"
    raise ValueError(
        f"CC Switch 供应商“{provider.name}”无法用于 Chat Completions，配置未导入。{detail}"
    )
