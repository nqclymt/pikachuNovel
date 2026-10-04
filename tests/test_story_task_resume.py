import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.story_tasks import (
    StoryTaskBusy, StoryTaskStopped, idle_story_mutation, release_story_task,
    reserve_story_task, serialized_story_task, story_lock_path, story_task,
)
from core.workspace import NovelWorkspace
from webui.chapter_chat import ChapterOutlineChatManager
from webui.draft_chat import DraftChatManager


class StoryTaskResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("demo", "other"):
            NovelWorkspace(name, root_dir=self.root).ensure_dirs()
        self.ws = NovelWorkspace("demo", root_dir=self.root)
        self.drafts = DraftChatManager(self.root)
        self.outlines = ChapterOutlineChatManager(self.root)
        self.arc = {"idx": 1, "start_ch": 1, "end_ch": 2, "content": "fixture"}
        self.resume = {"can_resume": False, "completed": 0, "total": 2, "next_chapter": 1}
        for target, value in (
            ("_list_novel_story_arcs", [self.arc]),
            ("chapter_draft_resume_status", self.resume),
            ("chapter_outline_resume_status", dict(self.resume)),
            ("_finalized_chapter_boundary", 0),
        ):
            active = patch("training.adaptive_builder." + target, return_value=value)
            active.start()
            self.addCleanup(active.stop)

    def wait(self, manager, book="demo", status=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.job_status(book, 1, 1)
            if job["status"] in (status or {"completed", "failed", "stopped"}):
                return job
            time.sleep(0.01)
        self.fail(f"worker did not finish: {job}")

    @staticmethod
    def done():
        return {"adjustment_note": "done", "artifacts": [], "stopped": False}

    def test_restart_preserves_refinement_options_even_when_all_drafts_exist(self):
        self.resume.update(completed=2, next_chapter=None)
        generation = {
            "mode": "refine", "start_chapter": 1, "end_chapter": 2, "max_chapters": 2,
            "humanize": False, "humanize_strength": "deep", "regenerate_existing": True,
            "refinement_mode": "revise", "writing_instruction": "保留对白，只改叙述。",
        }
        self.drafts._persist_job("demo", 1, 1, {
            "id": "interrupted", "status": "running", "request": {
                "instruction": generation["writing_instruction"], "humanize": False, "humanize_strength": "deep",
            }, "generation": generation,
        })
        restored = DraftChatManager(self.root)
        self.assertEqual(restored.job_status("demo", 1, 1)["status"], "interrupted")
        self.assertTrue(restored.job_status("demo", 1, 1)["can_resume"])
        with patch("training.adaptive_builder.gen_serial_chapters", return_value=self.done()) as generate, \
                patch("training.adaptive_builder.route_chapter_draft_refinement") as route:
            restored.continue_incomplete("demo", 1, 1)
            self.assertEqual(self.wait(restored)["status"], "completed")
            called = generate.call_args.kwargs
            for key, value in generation.items():
                if key != "mode":
                    self.assertEqual(called[key], value)
            self.assertTrue(called["resume_checkpoint"])
            self.assertEqual(called["generation_id"], "interrupted")
            route.assert_not_called()

    def test_first_chapter_failure_can_resume_original_instruction_and_humanize_choice(self):
        with patch("training.adaptive_builder.gen_serial_chapters", side_effect=RuntimeError("fixture failed")):
            self.drafts.start_message("demo", 1, 1, "让对话有停顿。", humanize=False, humanize_strength="light")
            self.assertTrue(self.wait(self.drafts)["can_resume"])
        restored = DraftChatManager(self.root)
        previous_generation = restored._load_persisted_job("demo", 1, 1)["generation"]["generation_id"]
        with patch("training.adaptive_builder.gen_serial_chapters", return_value=self.done()) as generate:
            restored.continue_incomplete("demo", 1, 1)
            self.assertEqual(self.wait(restored)["status"], "completed")
            self.assertEqual(generate.call_args.kwargs["writing_instruction"], "让对话有停顿。")
            self.assertFalse(generate.call_args.kwargs["humanize"])
            self.assertEqual(generate.call_args.kwargs["humanize_strength"], "light")
            self.assertTrue(generate.call_args.kwargs["resume_checkpoint"])
            self.assertEqual(generate.call_args.kwargs["generation_id"], previous_generation)

    def test_outline_resume_preserves_original_refinement_mode(self):
        self.outlines._persist_job("demo", 1, 1, {
            "status": "failed", "request": {"instruction": "第二章先隐瞒身份", "mode": "refine"},
        })
        restored = ChapterOutlineChatManager(self.root)
        with patch("training.adaptive_builder.refine_chapter_outlines_serial", return_value=self.done()) as refine, \
                patch("training.adaptive_builder.gen_chapter_outlines_for_arc") as generate:
            restored.continue_incomplete("demo", 1, 1)
            self.assertEqual(self.wait(restored)["status"], "completed")
            self.assertEqual(refine.call_args.args[3], "第二章先隐瞒身份")
            generate.assert_not_called()

    def test_new_request_gets_new_generation_identity(self):
        with patch("training.adaptive_builder.gen_serial_chapters", return_value=self.done()) as generate:
            self.drafts.start_message("demo", 1, 1, "第一轮")
            self.assertEqual(self.wait(self.drafts)["status"], "completed")
            first_id = generate.call_args.kwargs["generation_id"]
            self.drafts.start_message("demo", 1, 1, "第二轮")
            self.assertEqual(self.wait(self.drafts)["status"], "completed")
            self.assertNotEqual(generate.call_args.kwargs["generation_id"], first_id)

    def test_same_book_waits_before_reading_context_other_book_can_run(self):
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        contexts = []

        def outlines(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test timed out")
            ready.set()
            return self.done()

        def drafts(ws, **kwargs):
            contexts.append((ws.name, ready.is_set()))
            return self.done()

        with patch("training.adaptive_builder.gen_chapter_outlines_for_arc", side_effect=outlines), \
                patch("training.adaptive_builder.gen_serial_chapters", side_effect=drafts), \
                patch("webui.chapter_chat._chapter_outlines_exist", return_value=False):
            self.outlines.start_message("demo", 1, 1, "生成")
            self.assertTrue(entered.wait(2))
            self.drafts.start_message("demo", 1, 1, "生成正文")
            self.drafts.start_message("other", 1, 1, "生成正文")
            self.assertEqual(self.wait(self.drafts, "other")["status"], "completed")
            self.assertEqual(contexts, [("other", False)])
            with self.assertRaisesRegex(ValueError, "先结束任务"):
                self.drafts.reset("demo", 1, 1)
            with self.assertRaisesRegex(ValueError, "先结束任务"):
                self.outlines.clear("demo", 1, 1)
            release.set()
            self.assertEqual(self.wait(self.drafts)["status"], "completed")
            self.wait(self.outlines)
            self.assertIn(("demo", True), contexts)

    def test_queued_task_can_pause_and_stop_without_model_call(self):
        ticket = reserve_story_task(self.ws)
        self.addCleanup(release_story_task, ticket)
        with patch("training.adaptive_builder.gen_serial_chapters", return_value=self.done()) as generate:
            self.drafts.start_message("demo", 1, 1, "生成")
            self.drafts.pause("demo", 1, 1)
            self.wait(self.drafts, status={"paused"})
            release_story_task(ticket)
            time.sleep(0.15)
            generate.assert_not_called()
            self.drafts.stop("demo", 1, 1)
            self.assertEqual(self.wait(self.drafts)["status"], "stopped")
            generate.assert_not_called()
        with idle_story_mutation(self.ws):
            pass

    def test_queued_task_resumes_after_pause(self):
        ticket = reserve_story_task(self.ws)
        self.addCleanup(release_story_task, ticket)
        with patch("training.adaptive_builder.gen_serial_chapters", return_value=self.done()) as generate:
            self.drafts.start_message("demo", 1, 1, "生成")
            self.drafts.pause("demo", 1, 1)
            self.wait(self.drafts, status={"paused"})
            release_story_task(ticket)
            self.drafts.resume("demo", 1, 1)
            self.assertEqual(self.wait(self.drafts)["status"], "completed")
            generate.assert_called_once()

    def test_fifo_queue_and_cancelled_middle_waiter(self):
        first = reserve_story_task(self.ws)
        middle = reserve_story_task(self.ws)
        last = reserve_story_task(self.ws)
        for ticket in (first, middle, last):
            self.addCleanup(release_story_task, ticket)
        order, errors = [], []
        stop_middle = threading.Event()

        def run(ticket, stop):
            try:
                with story_task(self.ws, ticket=ticket, stop_event=stop):
                    order.append(ticket.task_id)
            except StoryTaskStopped:
                errors.append(ticket.task_id)

        a = threading.Thread(target=run, args=(middle, stop_middle))
        b = threading.Thread(target=run, args=(last, threading.Event()))
        a.start()
        b.start()
        stop_middle.set()
        a.join(2)
        self.assertEqual(errors, [middle.task_id])
        self.assertFalse(order)
        release_story_task(first)
        b.join(2)
        self.assertEqual(order, [last.task_id])

    def test_worker_failure_releases_book_for_another_manager(self):
        with patch("training.adaptive_builder.gen_serial_chapters", side_effect=ValueError("bad fixture")):
            self.drafts.start_message("demo", 1, 1, "生成")
            self.assertEqual(self.wait(self.drafts)["status"], "failed")
        with patch("training.adaptive_builder.gen_chapter_outlines_for_arc", return_value=self.done()), \
                patch("webui.chapter_chat._chapter_outlines_exist", return_value=False):
            self.outlines.start_message("demo", 1, 1, "生成")
            self.assertEqual(self.wait(self.outlines)["status"], "completed")

    def test_reentrant_decorator_and_exception_release(self):
        @serialized_story_task
        def operation(ws, pause_event=None, stop_event=None, progress_callback=None):
            with story_task(ws):
                raise RuntimeError("fixture")

        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with story_task(self.ws):
                operation(self.ws)
        with idle_story_mutation(Path(self.ws.file_system)):
            pass

    def test_external_process_lock_blocks_reset_and_wait_can_stop(self):
        code = (
            "import sys; from core.cli_scheduler import FileLock; "
            "lock=FileLock(sys.argv[1]); assert lock.acquire(); "
            "print('ready', flush=True); sys.stdin.readline(); lock.release()"
        )
        child = subprocess.Popen([sys.executable, "-u", "-c", code, str(story_lock_path(self.ws))],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(ValueError, "先结束任务"):
                with idle_story_mutation(self.ws):
                    pass
            stop = threading.Event()
            timer = threading.Timer(0.15, stop.set)
            timer.start()
            try:
                with self.assertRaises(StoryTaskStopped):
                    with story_task(self.ws, stop_event=stop):
                        self.fail("entered locked book")
            finally:
                timer.join()
        finally:
            child.communicate("release\n", timeout=3)
        with idle_story_mutation(self.ws):
            pass

    def test_idle_mutation_rejects_new_queued_task_and_deleted_book_is_not_recreated(self):
        from webui.task_runner import WorkspaceStore
        with idle_story_mutation(self.ws):
            with self.assertRaises(StoryTaskBusy):
                reserve_story_task(self.ws)
            WorkspaceStore(self.root).delete_workspace("demo")
        self.assertFalse(Path(self.ws.root).exists())
        self.assertTrue(story_lock_path(self.ws).exists())
        with self.assertRaisesRegex(FileNotFoundError, "已被删除"):
            with story_task(self.ws):
                self.fail("entered a deleted book")
        self.assertFalse(Path(self.ws.root).exists())


if __name__ == "__main__":
    unittest.main()
