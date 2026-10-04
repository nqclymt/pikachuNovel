import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from core.story_tasks import release_story_task, reserve_story_task, story_lock_path
from core.workspace import NovelWorkspace
from webui import app as webapp
from webui.task_runner import WorkspaceStore
from webui.draft_chat import DraftChatManager


class StoryMutationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = NovelWorkspace("demo", root_dir=self.root)
        self.ws.ensure_dirs()
        self.runtime = MagicMock()
        self.runtime.store = WorkspaceStore(self.root)
        self.runtime.draft_chat = DraftChatManager(self.root)
        self.runtime._chat_manager_busy.return_value = False
        self.runtime.tasks.delete_workspace_records.return_value = {"removed_tasks": 0}
        self.runtime.delete_workspace.side_effect = lambda name: webapp.WebRuntime.delete_workspace(self.runtime, name)
        with patch.object(webapp, "WebRuntime", return_value=self.runtime):
            self.client = TestClient(webapp.create_app())
        self.addCleanup(self.client.close)

    def calls(self):
        base = "/api/workspaces/demo"
        return [
            ("put", base + "/file", {"path": "file_system/chapters/vol_01/001_fixture.md", "content": "new text"}),
            ("post", base + "/finalized-chapters", {"kind": "drafts", "volume": 1, "chapter": 1, "finalized": True}),
            ("post", base + "/chapters/system-panel", {"mode": "disabled"}),
            ("post", base + "/arcs/1/reset", {}),
        ]

    def test_all_story_mutations_reject_queued_or_running_book_without_writing(self):
        ticket = reserve_story_task(self.ws)
        self.addCleanup(release_story_task, ticket)
        with patch("training.adaptive_builder.set_chapter_finalized") as finalize, \
                patch("training.adaptive_builder.configure_system_panel") as panel:
            for method, route, payload in self.calls():
                with self.subTest(route=route):
                    response = getattr(self.client, method)(route, json=payload)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn("先结束任务", response.json()["detail"])
            finalize.assert_not_called()
            panel.assert_not_called()
        self.runtime.arcs_chat.reset.assert_not_called()
        self.assertFalse((Path(self.ws.file_system) / "chapters").exists())

    def test_idle_book_mutations_work_and_keep_manager_workspace_root(self):
        self.runtime.arcs_chat.reset.return_value = {"reset": True}
        with patch("training.adaptive_builder.set_chapter_finalized", return_value={}) as finalize, \
                patch("training.adaptive_builder.configure_system_panel", return_value={"enabled": False}) as panel:
            for method, route, payload in self.calls():
                with self.subTest(route=route):
                    response = getattr(self.client, method)(route, json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(Path(finalize.call_args.args[0].root), Path(self.ws.root))
            self.assertEqual(Path(panel.call_args.args[0].root), Path(self.ws.root))
        saved = Path(self.ws.file_system) / "chapters/vol_01/001_fixture.md"
        self.assertEqual(saved.read_text(encoding="utf-8"), "new text")
        self.runtime.arcs_chat.reset.assert_called_once_with("demo", 1)

    def test_invalid_save_remains_actionable_bad_request(self):
        response = self.client.put("/api/workspaces/demo/file", json={"path": "file_system/example.md", "content": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("文本", response.json()["detail"])

    def test_busy_writing_guide_reset_is_a_conflict_not_internal_error(self):
        ticket = reserve_story_task(self.ws)
        self.addCleanup(release_story_task, ticket)
        response = self.client.delete("/api/workspaces/demo/drafts/writing-guide")
        self.assertEqual(response.status_code, 409)

    def test_workspace_delete_holds_external_lock_without_locking_deleted_files(self):
        response = self.client.delete("/api/workspaces/demo")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(Path(self.ws.root).exists())
        self.assertTrue(story_lock_path(self.ws).is_file())
        self.assertEqual(self.runtime.store.list_workspaces(), [])
        self.runtime.tasks.end_workspace_delete.assert_called_once_with("demo")

    def test_workspace_delete_rejects_book_task_and_releases_delete_gate(self):
        ticket = reserve_story_task(self.ws)
        self.addCleanup(release_story_task, ticket)
        response = self.client.delete("/api/workspaces/demo")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Path(self.ws.root).is_dir())
        self.runtime.tasks.end_workspace_delete.assert_called_once_with("demo")


if __name__ == "__main__":
    unittest.main()
