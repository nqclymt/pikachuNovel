import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.llm_provider import (
    LLMCallFailed,
    LLMProvider,
    LLMResponseFormatError,
    capture_llm_status,
)


class FakeAPIError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **_kwargs):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))

    def close(self):
        pass


class LLMProviderRetryTests(unittest.TestCase):
    def test_invalid_provider_response_fails_once_with_base_url_hint(self):
        provider = self.provider(["unused"])
        provider.base_url = "https://example.com"
        provider.client.chat.completions.create = Mock(
            return_value="not-an-openai-response"
        )

        with self.assertRaises(LLMCallFailed) as caught:
            provider.generate("hello")

        self.assertEqual(provider.client.chat.completions.create.call_count, 1)
        self.assertIsInstance(caught.exception.error, LLMResponseFormatError)
        self.assertIn("/v1", str(caught.exception))

    def provider(self, outcomes):
        provider = LLMProvider(model="test-model", api_key="test-key")
        provider.client = FakeClient(outcomes)
        return provider

    @patch("core.llm_provider.time.sleep")
    def test_generate_stops_after_bounded_attempts(self, sleep):
        provider = self.provider([FakeAPIError("offline")])

        with self.assertRaises(LLMCallFailed) as raised:
            provider.generate("prompt", max_retries=2)

        self.assertEqual(provider.client.chat.completions.calls, 3)
        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.5, 3.0])

    @patch("core.llm_provider.time.sleep")
    def test_generate_does_not_retry_deterministic_http_error(self, sleep):
        provider = self.provider([FakeAPIError("bad model", status_code=404)])

        with self.assertRaises(LLMCallFailed) as raised:
            provider.generate("prompt", max_retries=2)

        self.assertEqual(provider.client.chat.completions.calls, 1)
        self.assertEqual(raised.exception.attempts, 1)
        sleep.assert_not_called()

    def test_generate_cancelable_stops_after_bounded_attempts(self):
        provider = self.provider([FakeAPIError("offline")])
        clients = []

        def create_client():
            client = FakeClient([FakeAPIError("offline")])
            clients.append(client)
            return client

        provider._create_client = create_client
        cancel_event = threading.Event()
        with patch.dict(os.environ, {"HARNESS_NOVEL_LLM_RETRY_DELAY": "0"}):
            with self.assertRaises(FakeAPIError):
                provider.generate_cancelable("prompt", cancel_event, max_retries=2)

        self.assertEqual(len(clients), 3)
        self.assertEqual(sum(client.chat.completions.calls for client in clients), 3)

    @patch("core.llm_provider.time.sleep")
    def test_status_callback_receives_attempt_count(self, _sleep):
        provider = self.provider([FakeAPIError("offline")])
        messages = []

        with capture_llm_status(messages.append):
            with self.assertRaises(LLMCallFailed):
                provider.generate("prompt", max_retries=1)

        self.assertTrue(any("第 1/2 次调用" in message for message in messages))
        self.assertTrue(any("第 2/2 次调用" in message for message in messages))

    @patch("core.llm_provider.time.sleep")
    def test_retry_limit_environment_override_is_bounded(self, sleep):
        provider = self.provider([FakeAPIError("offline")])

        with patch.dict(os.environ, {"HARNESS_NOVEL_LLM_MAX_RETRIES": "99"}):
            with self.assertRaises(LLMCallFailed) as raised:
                provider.generate("prompt")

        self.assertEqual(raised.exception.attempts, 6)
        self.assertEqual(provider.client.chat.completions.calls, 6)


if __name__ == "__main__":
    unittest.main()
