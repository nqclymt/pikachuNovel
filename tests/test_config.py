import os
import unittest
from unittest.mock import patch

from core.config import ConfigLoader


class ConfigWireAPITests(unittest.TestCase):
    def tearDown(self):
        ConfigLoader.reload()

    def test_wire_api_is_loaded_with_model_configuration(self):
        with patch.dict(
            os.environ,
            {
                "DATA_BUILDER_MODEL": "demo-model",
                "DATA_BUILDER_BASE_URL": "https://api.example.com/v1",
                "DATA_BUILDER_API_KEY": "secret-key",
                "DATA_BUILDER_WIRE_API": "responses",
            },
            clear=True,
        ):
            ConfigLoader._env = {}
            config = ConfigLoader.get_data_builder_config()

        self.assertEqual(config["wire_api"], "responses")


if __name__ == "__main__":
    unittest.main()
