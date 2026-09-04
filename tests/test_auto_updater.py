import unittest
from pathlib import Path
from unittest.mock import patch

from webui.auto_updater import (
    AutoUpdateError,
    CHECKSUM_ASSET,
    EXECUTABLE_ASSET,
    _parse_checksum,
    _release_version,
    _replacement_script,
    _select_release_assets,
    auto_update_capability,
)


class AutoUpdaterTests(unittest.TestCase):
    def test_selects_exact_release_assets(self):
        release = {
            "assets": [
                {
                    "name": EXECUTABLE_ASSET,
                    "browser_download_url": "https://github.com/nqclymt/pikachuNovel/releases/download/v2.1.0/PikachuNovel.exe",
                },
                {
                    "name": CHECKSUM_ASSET,
                    "browser_download_url": "https://github.com/nqclymt/pikachuNovel/releases/download/v2.1.0/PikachuNovel.exe.sha256",
                },
            ]
        }
        exe_url, checksum_url = _select_release_assets(release)
        self.assertTrue(exe_url.endswith("/PikachuNovel.exe"))
        self.assertTrue(checksum_url.endswith("/PikachuNovel.exe.sha256"))

    def test_rejects_untrusted_asset_url(self):
        release = {
            "assets": [
                {"name": EXECUTABLE_ASSET, "browser_download_url": "https://example.com/PikachuNovel.exe"},
                {
                    "name": CHECKSUM_ASSET,
                    "browser_download_url": "https://github.com/nqclymt/pikachuNovel/releases/download/v2.1.0/PikachuNovel.exe.sha256",
                },
            ]
        }
        with self.assertRaises(AutoUpdateError):
            _select_release_assets(release)

    def test_parses_checksum_for_exact_executable_name(self):
        digest = "a" * 64
        self.assertEqual(_parse_checksum(f"{digest}  PikachuNovel.exe\n"), digest)
        with self.assertRaises(AutoUpdateError):
            _parse_checksum(f"{digest}  Other.exe\n")

    def test_release_version_accepts_v_prefix(self):
        self.assertEqual(_release_version({"tag_name": "v2.10.3"}), "2.10.3")
        with self.assertRaises(AutoUpdateError):
            _release_version({"tag_name": "latest"})

    def test_source_mode_does_not_auto_update(self):
        with patch("webui.auto_updater.os.name", "nt"), patch("webui.auto_updater.sys.frozen", False, create=True):
            capability = auto_update_capability()
        self.assertFalse(capability["auto_update_supported"])
        self.assertIn("源码", capability["auto_update_reason"])

    def test_replacement_script_quotes_paths_and_restarts(self):
        script = _replacement_script(
            Path("C:/Apps/Pikachu's Novel/PikachuNovel.exe"),
            Path("C:/Apps/Pikachu's Novel/.PikachuNovel.exe.update.tmp"),
            1234,
            '--workspace-root "C:\\Novel Data"',
        )
        self.assertIn("Wait-Process -Id $WaitPid", script)
        self.assertIn("Pikachu''s Novel", script)
        self.assertIn("Restore-Backup", script)
        self.assertIn("Start-PikachuNovel", script)


if __name__ == "__main__":
    unittest.main()