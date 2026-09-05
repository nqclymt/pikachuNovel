"""Read-only adapter for importing compatible providers from CC Switch."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.llm_provider import (
    LLMProvider,
    WIRE_API_CHAT,
    WIRE_API_RESPONSES,
    normalize_wire_api,
)


SUPPORTED_APP_TYPES = ("codex", "claude-desktop", "grokbuild")
SOURCE_LABELS = {
    "codex": "Codex",
    "claude-desktop": "Claude Desktop",
    "grokbuild": "Grok Build",
}

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
    source_type: str = "codex"

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "is_current": self.is_current,
            "source_type": self.source_type,
            "source_label": SOURCE_LABELS.get(self.source_type, self.source_type),
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


def _parse_claude_desktop_config(settings: dict, provider_name: str) -> dict[str, str]:
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    model = str(
        env.get("OPENAI_MODEL")
        or env.get("ANTHROPIC_MODEL")
        or settings.get("model")
        or ""
    ).strip()
    if not model and str(provider_name or "").strip().lower().startswith("grok"):
        model = str(provider_name).strip()
    return {
        "model": model,
        "base_url": str(
            env.get("OPENAI_BASE_URL")
            or env.get("ANTHROPIC_BASE_URL")
            or settings.get("base_url")
            or ""
        ).strip(),
        "api_key": str(
            env.get("OPENAI_API_KEY")
            or env.get("ANTHROPIC_AUTH_TOKEN")
            or env.get("ANTHROPIC_API_KEY")
            or ""
        ).strip(),
        "wire_api": str(settings.get("wire_api") or "").strip(),
    }


def _parse_grokbuild_fallback(config_text: str) -> dict[str, str]:
    default_match = re.search(r'(?m)^\s*default\s*=\s*[\"\'](.*?)[\"\']\s*$', config_text)
    model_name = default_match.group(1).strip() if default_match else ""
    sections: dict[str, dict[str, str]] = {}
    current = ""
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        section_match = re.fullmatch(r'\[model\.[\"\'](.*?)[\"\']\]', line)
        if section_match:
            current = section_match.group(1).strip()
            sections.setdefault(current, {})
            continue
        match = re.match(r'([A-Za-z0-9_]+)\s*=\s*[\"\'](.*?)[\"\']', line)
        if current and match:
            sections[current][match.group(1)] = match.group(2)
    if not model_name and sections:
        model_name = next(iter(sections))
    entry = sections.get(model_name, {})
    return {
        "model": str(entry.get("model") or model_name).strip(),
        "base_url": str(entry.get("base_url") or "").strip(),
        "api_key": str(entry.get("api_key") or "").strip(),
        "wire_api": str(entry.get("wire_api") or "").strip(),
    }


def _parse_grokbuild_config(settings: dict) -> dict[str, str]:
    config_text = settings.get("config") if isinstance(settings.get("config"), str) else ""
    if not config_text:
        return {"model": "", "base_url": "", "api_key": "", "wire_api": ""}
    if tomllib is None:
        return _parse_grokbuild_fallback(config_text)
    try:
        parsed = tomllib.loads(config_text)
    except (TypeError, tomllib.TOMLDecodeError):
        return _parse_grokbuild_fallback(config_text)
    models = parsed.get("models") if isinstance(parsed.get("models"), dict) else {}
    model_name = str(models.get("default") or "").strip()
    model_table = parsed.get("model") if isinstance(parsed.get("model"), dict) else {}
    if not model_name and model_table:
        model_name = str(next(iter(model_table))).strip()
    entry = model_table.get(model_name, {}) if model_name else {}
    if not isinstance(entry, dict):
        entry = {}
    return {
        "model": str(entry.get("model") or model_name).strip(),
        "base_url": str(entry.get("base_url") or "").strip(),
        "api_key": str(entry.get("api_key") or "").strip(),
        "wire_api": str(entry.get("wire_api") or "").strip(),
    }


def load_cc_switch_providers(database_path: Path | None = None) -> tuple[Path | None, list[CCSwitchProvider]]:
    path = database_path or discover_cc_switch_database()
    if path is None:
        return None, []
    path = path.resolve()
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        placeholders = ",".join("?" for _ in SUPPORTED_APP_TYPES)
        rows = connection.execute(
            "SELECT id, app_type, name, settings_config, is_current FROM providers "
            f"WHERE app_type IN ({placeholders}) "
            "ORDER BY is_current DESC, COALESCE(sort_index, 999999), created_at, id",
            SUPPORTED_APP_TYPES,
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"无法读取 CC Switch 数据库：{exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()

    providers = []
    for provider_id, app_type, name, raw_settings, is_current in rows:
        try:
            settings = json.loads(raw_settings)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(settings, dict):
            continue

        if app_type == "codex":
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
            parsed_provider = {
                "model": model,
                "base_url": codex.get("base_url", ""),
                "api_key": api_key,
                "wire_api": codex.get("wire_api", ""),
            }
        elif app_type == "claude-desktop":
            parsed_provider = _parse_claude_desktop_config(settings, str(name))
        elif app_type == "grokbuild":
            parsed_provider = _parse_grokbuild_config(settings)
        else:
            continue

        providers.append(
            CCSwitchProvider(
                id=str(provider_id),
                name=str(name),
                model=parsed_provider.get("model", ""),
                base_url=parsed_provider.get("base_url", ""),
                api_key=parsed_provider.get("api_key", ""),
                wire_api=parsed_provider.get("wire_api", ""),
                is_current=bool(is_current),
                source_type=str(app_type),
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
    """Resolve a streaming-compatible protocol and API root before persisting it."""
    if not provider.api_key:
        raise ValueError(f"CC Switch 供应商“{provider.name}”没有 API Key。")
    if not provider.model:
        raise ValueError(f"CC Switch 供应商“{provider.name}”没有默认模型。")
    requested_wire_api = str(provider.wire_api or "").strip()
    wire_apis = (
        [normalize_wire_api(requested_wire_api)]
        if requested_wire_api
        else [WIRE_API_RESPONSES, WIRE_API_CHAT]
    )
    validation_prompt = (
        "这是 PikachuNovel 的流式长请求兼容性验证。请用中文输出两句话，"
        "总长度至少四十个汉字，并以“兼容验证完成”结尾。"
    )
    errors = []
    for base_url in _base_url_candidates(provider.base_url):
        for wire_api in wire_apis:
            try:
                result = LLMProvider(
                    model=provider.model,
                    base_url=base_url,
                    api_key=provider.api_key,
                    max_tokens=96,
                    wire_api=wire_api,
                ).generate(validation_prompt, max_retries=0, max_tokens=96)
                if len(result.strip()) >= 20:
                    return CCSwitchProvider(
                        id=provider.id,
                        name=provider.name,
                        model=provider.model,
                        base_url=base_url,
                        api_key=provider.api_key,
                        wire_api=wire_api,
                        is_current=provider.is_current,
                        source_type=provider.source_type,
                    )
                errors.append(f"{base_url} [{wire_api}]: 返回内容过短")
            except Exception as exc:  # noqa: BLE001 - aggregate safe validation errors
                errors.append(f"{base_url} [{wire_api}]: {exc}")
    detail = errors[-1] if errors else "未返回内容"
    raise ValueError(
        f"CC Switch 供应商“{provider.name}”未通过流式长请求验证，配置未导入。{detail}"
    )
