"""Identify retained chapter batches even after their story arcs are removed."""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.workspace import NovelWorkspace


UNASSIGNED_BATCH = -1
ARC_FILE = re.compile(r"arc_(\d+)_ch(\d+)_(\d+)\.md")
CONVERSATION_FILE = re.compile(r"conversation_arc_(\d+)\.json")


def artifact_workspace(root: Path, name: str) -> NovelWorkspace:
    from webui.task_runner import WorkspaceStore

    store = WorkspaceStore(root)
    base = store.workspace_path(name)
    if not base.is_dir():
        raise FileNotFoundError(name)
    for relative in ("file_system", "file_system/chapters", "file_system/chapter_outlines",
                     "file_system/drafts", "file_system/system_panels", "file_system/finalized_chapters.json"):
        if not (base / relative).resolve().is_relative_to(base):
            raise ValueError("章节目录指向工作区外部，无法操作。")
    return NovelWorkspace(name, root_dir=str(store.root))


def chapter_files(fs: Path, volume: int) -> dict[str, dict[int, list[Path]]]:
    """Inventory only recognized generated files; no paths from chat are deleted."""
    vol = f"vol_{volume:02d}"
    layouts = [
        ("outlines", fs / "chapter_outlines" / vol, r"chapter_(\d+)\.md"),
        ("outlines", fs / "system_panels" / vol, r"chapter_(\d+)\.json"),
        ("drafts", fs / "chapters" / vol, r"(\d+)_第\d+章\.md"),
        ("drafts", fs / "chapters" / vol / "versions", r"(\d+)_第\d+章\.md_.+"),
        ("drafts", fs / "drafts" / vol / "raw_chapters", r"(\d+)_第\d+章\.raw\.md"),
        ("drafts", fs / "drafts" / vol / "raw_chapters" / "versions", r"(\d+)_第\d+章_.+\.raw\.md"),
        ("drafts", fs / "drafts" / vol / "editor_reviews", r"chapter_(\d+)_.+\.json"),
        ("drafts", fs / "drafts" / vol / "checkpoints", r"chapter_(\d+)\.json"),
        ("drafts", fs / "drafts" / vol / "candidates", r"chapter_(\d+)\.md"),
        ("drafts", fs / "drafts" / vol / "acceptance", r"chapter_(\d+)\.json"),
    ]
    result: dict[str, dict[int, list[Path]]] = {"outlines": {}, "drafts": {}}
    for kind, directory, pattern in layouts:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            match = re.fullmatch(pattern, path.name)
            if match and int(match[1]) > 0 and path.is_file():
                result[kind].setdefault(int(match[1]), []).append(path)
    return result


def _recorded_chapters(fs: Path, volume: int) -> dict[int, set[int]]:
    recorded: dict[int, set[int]] = {}
    vol = f"vol_{volume:02d}"
    patterns = [
        re.compile(rf"file_system/chapter_outlines/{vol}/chapter_(\d+)\.md"),
        re.compile(rf"file_system/chapters/{vol}/(\d+)_第\d+章\.md"),
    ]
    for directory in ("chapter_outlines", "chapters"):
        for path in (fs / directory / vol).glob("conversation_arc_*.json"):
            match = CONVERSATION_FILE.fullmatch(path.name)
            if not match:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            turns = payload.get("turns") if isinstance(payload, dict) else None
            for turn in turns if isinstance(turns, list) else []:
                artifacts = turn.get("artifacts") if isinstance(turn, dict) else None
                for artifact in artifacts if isinstance(artifacts, list) else []:
                    value = artifact.get("path") if isinstance(artifact, dict) else None
                    if not isinstance(value, str):
                        continue
                    for pattern in patterns:
                        chapter = pattern.fullmatch(value.replace("\\", "/"))
                        if chapter:
                            recorded.setdefault(int(match[1]), set()).add(int(chapter[1]))
    return recorded


def chapter_batches(fs: Path, volume: int) -> list[dict]:
    from webui.task_runner import story_arc_title

    if volume < 1:
        raise ValueError("卷号必须是正整数。")
    files = chapter_files(fs, volume)
    available = set(files["outlines"]) | set(files["drafts"])
    batches = []
    claimed: set[int] = set()
    for path in sorted((fs / "story_arcs" / f"vol_{volume:02d}").glob("arc_*.md")):
        match = ARC_FILE.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        idx, start, end = map(int, match.groups())
        if idx < 1 or start < 1 or end < start:
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            text = ""
        chapters = {ch for ch in available if start <= ch <= end}
        claimed.update(chapters)
        batches.append(dict(idx=idx, start_ch=start, end_ch=end, chapters=sorted(chapters),
                            title=story_arc_title(text[:1000]), missing_story_arc=not bool(text)))

    recorded = _recorded_chapters(fs, volume)
    live_ids = {batch["idx"] for batch in batches}
    owners: dict[int, set[int]] = {}
    for idx, chapters in recorded.items():
        if idx in live_ids or idx < 1:
            continue
        for chapter in (chapters & available) - claimed:
            owners.setdefault(chapter, set()).add(idx)
    for idx in sorted(recorded):
        chapters = sorted(ch for ch, ids in owners.items() if ids == {idx})
        if chapters:
            claimed.update(chapters)
            batches.append(dict(idx=idx, start_ch=min(chapters), end_ch=max(chapters),
                                chapters=chapters, title="保留的章节", missing_story_arc=True))
    unassigned = sorted(available - claimed)
    if unassigned:
        batches.append(dict(idx=UNASSIGNED_BATCH, start_ch=min(unassigned), end_ch=max(unassigned),
                            chapters=unassigned, title="未归属章节", missing_story_arc=True))
    return batches


def batch_has_files(fs: Path, volume: int, arc_idx: int, kind: str) -> bool:
    batch = next((b for b in chapter_batches(fs, volume) if b["idx"] == arc_idx), None)
    return bool(batch and set(batch["chapters"]) & set(chapter_files(fs, volume)[kind]))


def delete_batch_files(fs: Path, volume: int, arc_idx: int, kind: str) -> dict:
    batch = next((b for b in chapter_batches(fs, volume) if b["idx"] == arc_idx), None)
    if batch is None:
        raise ValueError("当前批次已不存在或无法确定所属章节，请刷新页面后选择保留的章节。")
    files = chapter_files(fs, volume)[kind]
    targets = [path for ch in batch["chapters"] for path in files.get(ch, [])]
    # Validate the complete deletion set before removing anything, including symlinks.
    root = fs.resolve()
    for path in targets:
        if not path.resolve().is_relative_to(root):
            raise ValueError("章节文件指向工作区外部，无法删除。")
    for path in targets:
        path.unlink(missing_ok=True)
    return {**batch, "deleted": len(targets)}
