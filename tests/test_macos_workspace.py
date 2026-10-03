import os
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import _default_workspace_root


class MacWorkspaceTests(unittest.TestCase):
    def test_finder_launch_uses_documents_even_before_directory_exists(self):
        with patch.dict(os.environ, {"HARNESS_NOVEL_HOME": ""}), patch(
            "webui.app.sys.platform", "darwin"
        ), patch("webui.app.Path.exists", return_value=False):
            self.assertEqual(
                _default_workspace_root(), Path.home() / "Documents" / "my-novels"
            )

    def test_configured_workspace_takes_precedence_on_macos(self):
        with patch.dict(os.environ, {"HARNESS_NOVEL_HOME": "~/custom-novels"}), patch(
            "webui.app.sys.platform", "darwin"
        ):
            self.assertEqual(_default_workspace_root(), Path.home() / "custom-novels")


if __name__ == "__main__":
    unittest.main()
