"""Web settings contract; no installed CLI, credentials or model calls needed."""

import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from webui import app as webapp
from webui.cc_switch import CCSwitchProvider


class AntigravitySettingsTests(unittest.TestCase):
    def setUp(self):
        # Settings routes do not need a real novel workspace or the user's home.
        with patch.object(webapp, "WebRuntime"):
            self.client = TestClient(webapp.create_app())
        self.addCleanup(self.client.close)

    def test_config_exposes_cli_fields_without_exposing_stored_api_key(self):
        values = {
            "ADAPTIVE_BUILDER_LITE_BACKEND": "antigravity_cli",
            "ADAPTIVE_BUILDER_LITE_CLI_PATH": "C:/Apps/agy.exe",
            "ADAPTIVE_BUILDER_LITE_CLI_AGENT": "novelist",
            "ADAPTIVE_BUILDER_LITE_CLI_EFFORT": "high",
            "ADAPTIVE_BUILDER_LITE_API_KEY": "stored-secret",
        }
        with patch.object(webapp, "_read_env", return_value=([], values)):
            response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        group = response.json()["groups"]["adaptive_builder_lite"]
        self.assertEqual(group["backend"], "antigravity_cli")
        self.assertEqual(group["cli_path"], "C:/Apps/agy.exe")
        self.assertEqual(group["cli_agent"], "novelist")
        self.assertEqual(group["cli_effort"], "high")
        self.assertTrue(group["api_key_configured"])
        self.assertNotIn("stored-secret", response.text)
        self.assertEqual(response.json()["groups"]["data_builder"]["backend"], "openai")

    def test_switch_and_clear_defaults_preserve_existing_api_secret(self):
        values = {
            "ADAPTIVE_BUILDER_LITE_BACKEND": "antigravity_cli",
            "ADAPTIVE_BUILDER_LITE_MODEL": "",
            "ADAPTIVE_BUILDER_LITE_CLI_PATH": "",
            "ADAPTIVE_BUILDER_LITE_CLI_AGENT": "",
            "ADAPTIVE_BUILDER_LITE_CLI_EFFORT": "medium",
            "ADAPTIVE_BUILDER_LITE_API_KEY": "",
        }
        with patch.object(webapp, "_update_env") as update, patch(
            "core.config.ConfigLoader.activate"
        ) as activate, patch.object(webapp, "_read_env", return_value=([], {})):
            response = self.client.put("/api/config", json={"values": values})
        self.assertEqual(response.status_code, 200)
        expected = {key: value for key, value in values.items() if not key.endswith("API_KEY")}
        update.assert_called_once_with(expected)
        activate.assert_called_once_with(expected)

    def test_invalid_settings_do_not_partially_write(self):
        for key, value in [
            ("DATA_BUILDER_BACKEND", "unknown"),
            ("DATA_BUILDER_CLI_EFFORT", "extreme"),
            ("DATA_BUILDER_CLI_PATH", "agy.exe\nOTHER_KEY=oops"),
        ]:
            with self.subTest(key=key), patch.object(webapp, "_update_env") as update:
                response = self.client.put("/api/config", json={"values": {key: value}})
            self.assertEqual(response.status_code, 400)
            update.assert_not_called()

    def test_installation_probe_never_generates_and_uses_unsaved_path(self):
        state = {"paused": False, "running": False, "waiting": 0}
        with patch("core.antigravity.probe_antigravity", return_value={
            "installed": False, "path": "", "version": "", "error": "未检测到 agy",
        }) as probe, patch("core.antigravity.scheduler_status", return_value=state), patch.object(
            webapp, "LLMProvider"
        ) as provider:
            response = self.client.get("/api/config/antigravity/status", params={"cli_path": "C:/Apps/agy.exe"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["installed"])
        probe.assert_called_once_with(cli_path="C:/Apps/agy.exe")
        provider.assert_not_called()

    def test_connection_test_uses_unsaved_form_without_api_key_and_bounds_wait(self):
        provider = MagicMock()
        provider.generate_cancelable.return_value = "连接成功"
        caller_thread = threading.get_ident()
        worker_threads = []

        def generate(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            self.assertIsInstance(kwargs["cancel_event"], threading.Event)
            self.assertEqual(kwargs["max_retries"], 0)
            return "连接成功"

        provider.generate_cancelable.side_effect = generate
        with patch("core.antigravity.scheduler_status", return_value={"paused": False}), patch.object(
            webapp, "LLMProvider", return_value=provider
        ) as constructor, patch.object(webapp.threading, "Timer") as timer, patch.object(
            webapp, "_update_env"
        ) as update:
            response = self.client.post("/api/config/antigravity/test", json={
                "model": "", "cli_path": "C:/Apps/agy.exe", "cli_agent": "novelist", "cli_effort": "high",
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        constructor.assert_called_once_with(
            backend="antigravity_cli", model="", cli_path="C:/Apps/agy.exe", cli_agent="novelist", cli_effort="high",
        )
        self.assertEqual(provider.timeout, 60.0)
        self.assertEqual(timer.call_args.args[0], 65.0)
        timer.return_value.cancel.assert_called_once()
        self.assertNotEqual(worker_threads[0], caller_thread)
        update.assert_not_called()

    def test_paused_test_requires_explicit_resume(self):
        with patch("core.antigravity.scheduler_status", return_value={
            "paused": True, "message": "额度已用完",
        }), patch.object(webapp, "LLMProvider") as provider:
            response = self.client.post("/api/config/antigravity/test", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("额度", response.json()["detail"])
        provider.assert_not_called()
        with patch("core.antigravity.resume_scheduler", return_value={"paused": False}) as resume:
            response = self.client.post("/api/config/antigravity/resume", json={})
        self.assertEqual(response.status_code, 200)
        resume.assert_called_once_with()

    def test_cli_failure_is_shown_as_actionable_client_error(self):
        provider = MagicMock()
        provider.generate_cancelable.side_effect = RuntimeError("请在终端运行 agy 完成 Google 登录")
        with patch("core.antigravity.scheduler_status", return_value={"paused": False}), patch.object(
            webapp, "LLMProvider", return_value=provider
        ):
            response = self.client.post("/api/config/antigravity/test", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Google 登录", response.json()["detail"])

    def test_cc_switch_import_resets_previous_cli_backend(self):
        provider = CCSwitchProvider(
            id="demo", name="Demo", model="demo-model", base_url="https://example.com/v1",
            api_key="secret", wire_api="responses", is_current=True,
        )
        with patch.object(webapp, "load_cc_switch_providers", return_value=("fixture.db", [provider])), patch.object(
            webapp, "validate_cc_switch_provider", return_value=provider
        ), patch.object(webapp, "_update_env") as update, patch(
            "core.config.ConfigLoader.activate"
        ), patch.object(webapp, "_read_env", return_value=([], {})):
            response = self.client.post("/api/config/cc-switch/import", json={
                "assignments": {"adaptive_builder_lite": "demo"},
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(update.call_args.args[0]["ADAPTIVE_BUILDER_LITE_BACKEND"], "openai")


if __name__ == "__main__":
    unittest.main()
