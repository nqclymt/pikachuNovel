"""Writing policy must survive packaging and retain explicit user priorities."""

import ast
import fnmatch
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest

from core import writing_requirements as requirements


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_source_default_guide_contains_real_writing_rules():
    guide = requirements.read_default_writing_guide()
    assert "开头规则" in guide
    assert "正文推进规则" in guide
    assert len(guide) > 1000


def test_frozen_resource_location_does_not_depend_on_cwd(tmp_path, monkeypatch):
    bundled_core = tmp_path / "_MEIexample" / "core"
    bundled_core.mkdir(parents=True)
    (bundled_core / "system_prompt.md").write_text("\ufeff  冻结包里的中文规范\n保留内部换行。  ", encoding="utf-8")
    monkeypatch.setattr(requirements, "__file__", str(bundled_core / "writing_requirements.py"))
    monkeypatch.chdir(tmp_path)
    assert requirements.read_default_writing_guide() == "冻结包里的中文规范\n保留内部换行。"


@pytest.mark.parametrize("content,reason", [(None, "缺少"), (b" \r\n\t", "内容为空"), (b"\xff\xfe\x00", "UTF-8")])
def test_invalid_required_resource_stops_with_actionable_error(tmp_path, monkeypatch, content, reason):
    monkeypatch.setattr(requirements, "__file__", str(tmp_path / "writing_requirements.py"))
    if content is not None:
        (tmp_path / "system_prompt.md").write_bytes(content)
    with pytest.raises(requirements.WritingRequirementsError, match=reason) as caught:
        requirements.read_default_writing_guide()
    assert "core/system_prompt.md" in str(caught.value)


def test_composed_policy_keeps_current_and_persistent_instructions_without_changing_them():
    current = "这一章用第三人称，保留阿青已经知道的秘密。\n对白减少。"
    persistent = "平时使用第一人称。\n\n保留长短句的自然变化。"
    result = requirements.compose_writing_requirements(persistent, current)
    assert current in result
    assert persistent in result
    assert result.index(current) < result.index(persistent)
    assert "本轮写作要求 > 持久用户规范 > 参考风格" in result
    assert "不得据此改动" in result
    assert "章纲必写事件" in result
    assert "不得为模仿参考风格搬入参考小说的情节" in result


def test_empty_requirements_do_not_invent_user_preferences():
    result = requirements.compose_writing_requirements(None)
    assert "未额外指定" in result
    assert "未提供额外持久规范" in result
    assert "None" not in result


@pytest.mark.parametrize("filename", ["HarnessNovel.spec", "PikachuNovel-macos.spec"])
def test_desktop_builds_include_readable_root_writing_resources(filename, monkeypatch):
    """Evaluate the real spec's resource list without running PyInstaller or an EXE."""
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda name: ([], [], [])
    hooks.collect_submodules = lambda name: []
    monkeypatch.setitem(sys.modules, "PyInstaller", ModuleType("PyInstaller"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", ModuleType("PyInstaller.utils"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    captured = {}

    def analysis(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=kwargs["datas"])

    noop = lambda *args, **kwargs: SimpleNamespace()
    runpy.run_path(str(PROJECT_ROOT / "packaging" / filename), init_globals={
        "SPECPATH": str(PROJECT_ROOT / "packaging"),
        "Analysis": analysis, "PYZ": noop, "EXE": noop,
        "COLLECT": noop, "BUNDLE": noop,
    })
    resources = {Path(source).resolve(): destination for source, destination in captured["datas"]}
    for name in ("system_prompt.md", "agents.md"):
        source = PROJECT_ROOT / "core" / name
        assert resources.get(source.resolve()) == "core"
        assert source.is_file()
    assert (PROJECT_ROOT / "core" / "system_prompt.md").read_text(encoding="utf-8").strip()


def test_python_distribution_includes_root_writing_resources():
    tree = ast.parse((PROJECT_ROOT / "setup.py").read_text(encoding="utf-8"))
    setup_call = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "setup")
    package_data = ast.literal_eval(next(keyword.value for keyword in setup_call.keywords
                                        if keyword.arg == "package_data"))
    for name in ("system_prompt.md", "agents.md"):
        assert any(fnmatch.fnmatch(name, pattern) for pattern in package_data["core"])
