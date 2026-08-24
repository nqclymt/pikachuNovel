import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.cc_switch import (
    CCSwitchProvider,
    load_cc_switch_providers,
    validate_cc_switch_provider,
)


class CCSwitchAdapterTests(unittest.TestCase):
    def test_loads_codex_provider_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cc-switch.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE providers ("
                "id TEXT, app_type TEXT, name TEXT, settings_config TEXT, "
                "is_current INTEGER, sort_index INTEGER, created_at INTEGER)"
            )
            settings = {
                "auth": {"OPENAI_API_KEY": "secret-key"},
                "config": (
                    'model_provider = "custom"\n'
                    'model = "demo-model"\n'
                    '[model_providers.custom]\n'
                    'base_url = "https://api.example.com"\n'
                    'wire_api = "responses"\n'
                ),
            }
            connection.execute(
                "INSERT INTO providers VALUES (?, 'codex', ?, ?, 1, 0, 1)",
                ("provider-1", "Demo", json.dumps(settings)),
            )
            connection.commit()
            connection.close()

            path, providers = load_cc_switch_providers(database)

            self.assertEqual(path, database.resolve())
            self.assertEqual(len(providers), 1)
            self.assertEqual(providers[0].model, "demo-model")
            self.assertEqual(providers[0].base_url, "https://api.example.com")
            self.assertEqual(providers[0].api_key, "secret-key")
            public = providers[0].public()
            self.assertNotIn("api_key", public)
            self.assertTrue(public["api_key_configured"])

    def test_validation_tries_normalized_v1_url_first(self):
        provider = CCSwitchProvider(
            id="provider-1",
            name="Demo",
            model="demo-model",
            base_url="https://api.example.com",
            api_key="secret-key",
            wire_api="responses",
            is_current=True,
        )
        calls = []

        class FakeProvider:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def generate(self, *_args, **_kwargs):
                return "OK"

        with patch("webui.cc_switch.LLMProvider", FakeProvider):
            resolved = validate_cc_switch_provider(provider)

        self.assertEqual(resolved.base_url, "https://api.example.com/v1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["api_key"], "secret-key")


if __name__ == "__main__":
    unittest.main()
