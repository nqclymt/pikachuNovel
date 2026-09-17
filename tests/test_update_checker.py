import json
import unittest
from unittest.mock import patch

from webui.update_checker import _version_key, check_latest_release
from webui.version import APP_VERSION


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(
            {
                "tag_name": "v2.2.0",
                "name": "PikachuNovel v2.2.0",
                "html_url": "https://github.com/nqclymt/pikachuNovel/releases/tag/v2.2.0",
                "published_at": "2026-08-25T00:00:00Z",
                "draft": False,
                "prerelease": False,
            }
        ).encode("utf-8")


class UpdateCheckerTests(unittest.TestCase):
    def test_versions_are_compared_numerically(self):
        self.assertGreater(_version_key("v2.10.0"), _version_key("2.9.9"))
        self.assertEqual(_version_key("invalid"), ())

    def test_detects_new_stable_release(self):
        with patch("webui.update_checker.urllib.request.urlopen", return_value=_Response()):
            result = check_latest_release()

        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "2.2.0")
        self.assertIn("releases/tag/v2.2.0", result["release_url"])

    def test_network_failure_is_non_fatal(self):
        with patch("webui.update_checker.urllib.request.urlopen", side_effect=OSError("offline")):
            result = check_latest_release()

        self.assertFalse(result["update_available"])
        self.assertEqual(result["current_version"], APP_VERSION)
        self.assertEqual(result["latest_version"], APP_VERSION)


if __name__ == "__main__":
    unittest.main()
