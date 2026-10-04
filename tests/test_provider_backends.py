"""Integration boundaries: CLI login is not an API key and roles stay separate."""

import os
import threading
import unittest
from unittest.mock import patch

from core.config import ConfigLoader
from core.llm_provider import LLMProvider, provider_configured
from training.adaptive_builder import _get_humanize_llm, _get_lite_llm, _get_llm


class ProviderBackendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        ConfigLoader._env = {}

    def tearDown(self):
        self.env.stop()
        ConfigLoader.reload()

    def test_old_config_remains_api_and_cli_needs_no_key(self):
        self.assertFalse(provider_configured({}))
        self.assertTrue(provider_configured({"api_key": "old-key"}))
        self.assertTrue(provider_configured({"backend": "antigravity_cli"}))
        with self.assertRaises(ValueError):
            provider_configured({"backend": "unknown"})
        self.assertEqual(ConfigLoader.get_data_builder_config()["backend"], "openai")

    def test_role_config_loads_cli_options_and_creates_provider_without_sdk(self):
        ConfigLoader._env = {
            "ADAPTIVE_BUILDER_BACKEND": "antigravity_cli",
            "ADAPTIVE_BUILDER_LITE_BACKEND": "antigravity_cli",
            "ADAPTIVE_BUILDER_LITE_CLI_PATH": "C:/agy/agy.exe",
            "ADAPTIVE_BUILDER_LITE_CLI_AGENT": "novelist",
            "ADAPTIVE_BUILDER_LITE_CLI_EFFORT": "high",
        }
        with patch("core.llm_provider.OpenAI") as client:
            planner, writer, editor = _get_llm(), _get_lite_llm(), _get_humanize_llm()
        client.assert_not_called()
        self.assertEqual(planner.backend, "antigravity_cli")
        self.assertEqual(writer.cli_path, "C:/agy/agy.exe")
        self.assertEqual(editor.cli_agent, "novelist")
        self.assertEqual(editor.cli_effort, "high")
        self.assertEqual(editor.model, "")

    def test_switching_editor_to_cli_does_not_inherit_api_model_or_credentials(self):
        ConfigLoader._env = {
            "ADAPTIVE_BUILDER_LITE_MODEL": "gateway-only-model",
            "ADAPTIVE_BUILDER_LITE_API_KEY": "private-key",
            "ADAPTIVE_BUILDER_LITE_BASE_URL": "https://gateway.example/v1",
            "HUMANIZE_BUILDER_BACKEND": "antigravity_cli",
        }
        editor = _get_humanize_llm()
        self.assertEqual(editor.backend, "antigravity_cli")
        self.assertEqual(editor.model, "")
        self.assertIsNone(editor.api_key)
        self.assertEqual(editor.base_url, "")

    def test_explicit_api_editor_does_not_inherit_cli_model(self):
        ConfigLoader._env = {
            "ADAPTIVE_BUILDER_LITE_MODEL": "agy-only-model",
            "ADAPTIVE_BUILDER_LITE_BACKEND": "antigravity_cli",
            "HUMANIZE_BUILDER_API_KEY": "editor-api-key",
            "HUMANIZE_BUILDER_MODEL": "editor-model",
        }
        with patch("core.llm_provider.OpenAI"):
            editor = _get_humanize_llm()
        self.assertEqual(editor.backend, "openai")
        self.assertEqual(editor.model, "editor-model")
        self.assertEqual(editor.api_key, "editor-api-key")

    def test_explicit_cli_editor_blank_model_uses_its_own_default(self):
        ConfigLoader._env = {
            "ADAPTIVE_BUILDER_LITE_BACKEND": "antigravity_cli",
            "ADAPTIVE_BUILDER_LITE_MODEL": "writer-only-model",
            "ADAPTIVE_BUILDER_LITE_CLI_AGENT": "writer-only-agent",
            "HUMANIZE_BUILDER_BACKEND": "antigravity_cli",
        }
        editor = _get_humanize_llm()
        self.assertEqual(editor.model, "")
        self.assertEqual(editor.cli_agent, "")

    def test_cli_dispatch_supports_cancel_and_never_creates_api_client(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-use"}):
            with patch("core.llm_provider.OpenAI") as client:
                provider = LLMProvider(backend="antigravity_cli", cli_effort="high")
                with patch("core.antigravity.run_antigravity", return_value="中文正文") as run:
                    self.assertEqual(provider.generate("写一章", is_json=True), "中文正文")
                    self.assertEqual(run.call_args.kwargs["model"], "")
                    self.assertTrue(run.call_args.kwargs["is_json"])
                    cancel = threading.Event()
                    provider.generate_cancelable("继续", cancel)
                    self.assertIs(run.call_args.kwargs["cancel_event"], cancel)
                client.assert_not_called()

    def test_cli_failure_is_not_retried_or_switched_to_api(self):
        from core.antigravity import AntigravityError

        error = AntigravityError("quota", "限额测试")
        with patch("core.antigravity.run_antigravity", side_effect=error) as run:
            provider = LLMProvider(backend="antigravity_cli")
            with self.assertRaises(AntigravityError):
                provider.generate("写一章", max_retries=5)
        self.assertEqual(run.call_count, 1)

    def test_reference_analysis_cli_is_serial_without_api_key(self):
        from training.reference_analyzer import run_reference_analysis

        ConfigLoader._env = {"DATA_BUILDER_BACKEND": "antigravity_cli"}
        with patch("training.reference_analyzer.ReferenceAnalyzer") as analyzer:
            run_reference_analysis("unused.txt", "unused-output", batch_size=20)
        self.assertEqual(analyzer.call_args.kwargs["max_workers"], 1)
        self.assertEqual(analyzer.call_args.kwargs["llm"].backend, "antigravity_cli")


if __name__ == "__main__":
    unittest.main()
