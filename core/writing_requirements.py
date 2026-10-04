"""Load required writing resources and keep user style priorities consistent."""

from pathlib import Path


class WritingRequirementsError(RuntimeError):
    """A required writing resource is unavailable or corrupt."""


def read_default_writing_guide() -> str:
    """Read the installed/frozen core resource, independent of the working directory."""
    path = Path(__file__).resolve().with_name("system_prompt.md")
    try:
        guide = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as exc:
        raise WritingRequirementsError(
            "缺少必需的默认写作规范资源 core/system_prompt.md。"
            "请重新安装完整程序，或在打包时收录该文件。"
        ) from exc
    except UnicodeError as exc:
        raise WritingRequirementsError(
            "默认写作规范 core/system_prompt.md 不是有效的 UTF-8 文本，请修复文件或重新安装程序。"
        ) from exc
    except OSError as exc:
        raise WritingRequirementsError(
            "无法读取默认写作规范 core/system_prompt.md，请检查文件权限或重新安装完整程序。"
        ) from exc
    if not guide:
        raise WritingRequirementsError(
            "必需的默认写作规范 core/system_prompt.md 内容为空，请恢复文件或重新安装完整程序。"
        )
    return guide


def compose_writing_requirements(persistent_guide, writing_instruction="") -> str:
    """Compose style constraints without promoting a style preference into story facts."""
    current = str(writing_instruction or "").strip()
    persistent = str(persistent_guide or "").strip()
    return (
        "【写作要求优先级】\n"
        "涉及文风、叙述方式、段落和表达形式时，按以下顺序处理冲突："
        "本轮写作要求 > 持久用户规范 > 参考风格。"
        "本轮未要求改变的部分继续遵循持久规范；参考风格仅补充未指定的表达习惯。\n"
        "这些优先级只调整表达，不得据此改动已确立的人物事实、动机、时间线、"
        "信息边界、章纲必写事件或已发生的剧情；不得为模仿参考风格搬入参考小说的情节。\n\n"
        "【本轮写作要求】\n"
        f"{current or '（未额外指定，沿用持久规范和当前任务约束。）'}\n\n"
        "【持久写作规范】\n"
        f"{persistent or '（未提供额外持久规范。）'}"
    )
