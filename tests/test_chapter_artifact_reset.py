import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.workspace import NovelWorkspace
from webui.arc_chat import ArcsChatManager
from webui.chapter_artifacts import chapter_batches
from webui.chapter_chat import ChapterOutlineChatManager
from webui.draft_chat import DraftChatManager
from webui.task_runner import WorkspaceStore


class ChapterArtifactResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HARNESS_NOVEL_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ws = NovelWorkspace("demo", root_dir=self.root)
        self.ws.ensure_dirs()
        self.fs = Path(self.ws.file_system)
        self.outlines = ChapterOutlineChatManager(self.root)
        self.drafts = DraftChatManager(self.root)

    def write(self, relative, content="fixture"):
        path = self.fs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def record(self, kind, idx, chapters):
        paths = [f"file_system/chapter_outlines/vol_01/chapter_{ch:03d}.md" if kind == "chapter_outlines"
                 else f"file_system/chapters/vol_01/{ch:03d}_第{ch}章.md" for ch in chapters]
        return self.write(f"{kind}/vol_01/conversation_arc_{idx}.json", json.dumps({
            "turns": [{"role": "assistant", "artifacts": [{"path": path} for path in paths]}],
        }))

    def chapter(self, ch, volume=1):
        vol = f"vol_{volume:02d}"
        return {
            "outline": self.write(f"chapter_outlines/{vol}/chapter_{ch:03d}.md"),
            "panel": self.write(f"system_panels/{vol}/chapter_{ch:03d}.json", "{}"),
            "draft": self.write(f"chapters/{vol}/{ch:03d}_第{ch}章.md"),
            "version": self.write(f"chapters/{vol}/versions/{ch:03d}_第{ch}章.md_previous"),
            "raw": self.write(f"drafts/{vol}/raw_chapters/{ch:03d}_第{ch}章.raw.md"),
            "raw_version": self.write(f"drafts/{vol}/raw_chapters/versions/{ch:03d}_第{ch}章_previous.raw.md"),
            "review": self.write(f"drafts/{vol}/editor_reviews/chapter_{ch:03d}_previous.json", "{}"),
        }

    def test_reset_after_upstream_arc_reset_preserves_other_volume_and_kind(self):
        self.write("story_arcs/vol_01/arc_001_ch001_001.md")
        first, other = self.chapter(1), self.chapter(1, volume=2)
        self.record("chapter_outlines", 1, [1])
        self.record("chapters", 1, [1])
        ArcsChatManager(self.root).reset("demo", 1)

        summary = WorkspaceStore(self.root)._volume_details(Path(self.ws.root), self.fs)
        recovered = next(v for v in summary if v["volume"] == 1)["arcs"][0]
        self.assertEqual(recovered["idx"], 1)
        self.assertTrue(recovered["missing_story_arc"])
        self.assertTrue(self.outlines.history("demo", 1, 1)["has_outlines"])
        self.assertTrue(self.drafts.history("demo", 1, 1)["has_drafts"])

        result = self.outlines.reset("demo", 1, 1)
        self.assertEqual(result["deleted"], 2)
        self.assertFalse(first["outline"].exists())
        self.assertFalse(first["panel"].exists())
        self.assertTrue(first["draft"].exists())
        result = self.drafts.reset("demo", 1, 1)
        self.assertEqual(result["deleted"], 5)
        self.assertTrue(all(not p.exists() for p in first.values()))
        self.assertTrue(all(p.exists() for p in other.values()))
        self.assertEqual(chapter_batches(self.fs, 1), [])

    def test_empty_arc_still_supplies_chapter_boundaries(self):
        self.write("story_arcs/vol_01/arc_001_ch001_001.md", "")
        first, second = self.chapter(1), self.chapter(2)
        self.outlines.reset("demo", 1, 1)
        self.drafts.reset("demo", 1, 1)
        self.assertTrue(all(not p.exists() for p in first.values()))
        self.assertTrue(all(p.exists() for p in second.values()))

    def test_sparse_history_does_not_delete_chapters_between_recorded_outputs(self):
        first, middle, last = self.chapter(1), self.chapter(2), self.chapter(3)
        self.record("chapters", 1, [1, 3])
        result = self.drafts.reset("demo", 1, 1)
        self.assertEqual(result["chapters"], [1, 3])
        self.assertFalse(first["draft"].exists())
        self.assertFalse(last["draft"].exists())
        self.assertTrue(all(p.exists() for p in middle.values()))

    def test_chapter_reset_can_recover_from_draft_history_after_old_reset_cleared_chat(self):
        first = self.chapter(1)
        self.write("chapter_outlines/vol_01/conversation_arc_1.json", '{"turns": []}')
        self.record("chapters", 1, [1])
        self.outlines.reset("demo", 1, 1)
        self.assertFalse(first["outline"].exists())
        self.assertTrue(first["draft"].exists())

    def test_unassigned_files_remain_deletable_without_any_history(self):
        first = self.chapter(1)
        self.assertEqual(chapter_batches(self.fs, 1)[0]["idx"], -1)
        self.assertTrue(self.drafts.history("demo", 1, -1)["has_drafts"])
        self.drafts.reset("demo", 1, -1)
        self.assertTrue(first["outline"].exists())
        self.assertFalse(first["raw_version"].exists())
        self.outlines.reset("demo", 1, -1)
        self.assertEqual(chapter_batches(self.fs, 1), [])

    def test_invalid_batch_fails_without_clearing_history_or_deleting_files(self):
        first = self.chapter(1)
        conversation = self.record("chapter_outlines", 7, [])
        original = conversation.read_bytes()
        with self.assertRaisesRegex(ValueError, "批次"):
            self.outlines.reset("demo", 1, 7)
        with self.assertRaisesRegex(ValueError, "批次"):
            self.drafts.reset("demo", 1, 7)
        self.assertEqual(conversation.read_bytes(), original)
        self.assertTrue(all(p.exists() for p in first.values()))

    def test_live_arc_ownership_wins_over_stale_history(self):
        first, second = self.chapter(1), self.chapter(2)
        self.record("chapters", 1, [1, 2])
        self.write("story_arcs/vol_01/arc_002_ch002_002.md")
        self.drafts.reset("demo", 1, 1)
        self.assertFalse(first["draft"].exists())
        self.assertTrue(all(p.exists() for p in second.values()))

    def test_untrusted_artifact_paths_do_not_determine_deletion_scope(self):
        first = self.chapter(1)
        self.write("chapters/vol_01/conversation_arc_1.json", json.dumps({"turns": [{"artifacts": [
            {"path": "file_system/chapters/vol_02/001_第1章.md"},
            {"path": "../file_system/chapters/vol_01/001_第1章.md"},
            {"path": 1},
        ]}]}))
        with self.assertRaises(ValueError):
            self.drafts.reset("demo", 1, 1)
        self.assertTrue(all(p.exists() for p in first.values()))

    def test_running_generation_blocks_reset_even_for_another_batch(self):
        first = self.chapter(1)
        for manager, jobs in ((self.outlines, self.outlines._jobs), (self.drafts, self.drafts._jobs)):
            jobs[("demo", 1, 2)] = {"status": "running"}
            with self.assertRaisesRegex(ValueError, "先结束任务"):
                manager.reset("demo", 1, -1)
        self.assertTrue(all(p.exists() for p in first.values()))

    def test_draft_reset_clears_only_selected_finalization_flags(self):
        self.chapter(1)
        self.chapter(2)
        self.record("chapters", 1, [1])
        final = self.write("finalized_chapters.json", json.dumps({"version": 2, "drafts": {
            "vol_01": {"1": {"finalized": True}, "2": {"finalized": True}},
        }}))
        self.drafts.reset("demo", 1, 1)
        self.assertEqual(set(json.loads(final.read_text())["drafts"]["vol_01"]), {"2"})

    def test_reset_uses_manager_root_instead_of_global_workspace_environment(self):
        first = self.chapter(1)
        with patch.dict(os.environ, {"HARNESS_NOVEL_HOME": str(self.root / "different-root")}):
            self.drafts.reset("demo", 1, -1)
        self.assertFalse(first["draft"].exists())
        self.assertFalse((self.root / "different-root").exists())


if __name__ == "__main__":
    unittest.main()
