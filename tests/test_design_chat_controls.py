import tempfile
import threading
import unittest
from pathlib import Path

from webui.design_chat import DesignChatManager


class DesignChatControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = DesignChatManager(Path(self.temp.name))
        self.key = ("demo", "concept")
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.cancel_event = threading.Event()
        self.stop_event = threading.Event()
        self.manager._jobs[self.key] = {
            "id": "job",
            "status": "running",
            "phase": "generating",
            "message": "",
            "pause_event": self.pause_event,
            "cancel_event": self.cancel_event,
            "stop_event": self.stop_event,
            "prompt_history": [],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_concept_job_can_pause_and_resume(self):
        paused = self.manager.pause("demo", "concept")
        self.assertEqual(paused["status"], "pausing")
        self.assertFalse(self.pause_event.is_set())
        self.assertTrue(self.cancel_event.is_set())

        resumed = self.manager.resume("demo", "concept")
        self.assertEqual(resumed["status"], "running")
        self.assertTrue(self.pause_event.is_set())
        self.assertFalse(self.cancel_event.is_set())

    def test_concept_job_can_stop(self):
        stopped = self.manager.stop("demo", "concept")
        self.assertEqual(stopped["status"], "stopping")
        self.assertTrue(self.stop_event.is_set())
        self.assertTrue(self.cancel_event.is_set())
        self.assertTrue(self.pause_event.is_set())


if __name__ == "__main__":
    unittest.main()
