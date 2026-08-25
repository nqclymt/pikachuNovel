import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.llm_provider import (
    LLMCallFailed,
    LLMProvider,
    LLMResponseFormatError,
    _error_summary,
    _retry_delay_for_error,
    capture_llm_status,
)


class FakeAPIError(RuntimeError):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_stream = None

    def create(self, **_kwargs):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        self.last_stream = FakeStream([
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=outcome))]
            )
        ])
        return self.last_stream


class FakeStream:
    def __init__(self, items):
        self.items = items
        self.closed = False

    def __iter__(self):
        return iter(self.items)

    def close(self):
        self.closed = True


class FakeResponses:
    def __init__(self, text="Responses 流式结果", events=None):
        self.text = text
        self.events = events
        self.calls = []
        self.last_stream = None

    def create(self, **kwargs):
        self.calls.append(kwargs)
        midpoint = max(1, len(self.text) // 2)
        events = self.events or [
            SimpleNamespace(type="response.output_text.delta", delta=self.text[:midpoint]),
            SimpleNamespace(type="response.output_text.delta", delta=self.text[midpoint:]),
            SimpleNamespace(type="response.completed"),
        ]
        self.last_stream = FakeStream(events)
        return self.last_stream


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))
        self.responses = FakeResponses()
        self.closed = False

    def close(self):
        self.closed = True


class LLMProviderRetryTests(unittest.TestCase):
    def test_chat_completions_are_collected_from_stream(self):
        provider = self.provider(["分块输出成功"])

        result = provider.generate("prompt", max_retries=0)

        self.assertEqual(result, "分块输出成功")
        self.assertTrue(provider.client.chat.completions.last_stream.closed)

    def test_responses_protocol_streams_with_responses_parameters(self):
        provider = LLMProvider(
            model="test-model",
            api_key="test-key",
            wire_api="responses",
        )
        provider.client = FakeClient(["unused"])

        result = provider.generate(
            "prompt", is_json=True, max_tokens=42, max_retries=0
        )

        self.assertEqual(result, "Responses 流式结果")
        kwargs = provider.client.responses.calls[0]
        self.assertEqual(kwargs["input"], "prompt")
        self.assertEqual(kwargs["max_output_tokens"], 42)
        self.assertEqual(kwargs["text"], {"format": {"type": "json_object"}})
        self.assertTrue(kwargs["stream"])
        self.assertNotIn("temperature", kwargs)
        self.assertTrue(provider.client.responses.last_stream.closed)

    def test_responses_failed_and_incomplete_events_are_errors(self):
        for event_type, detail_name in (
            ("response.failed", "error"),
            ("response.incomplete", "incomplete_details"),
        ):
            with self.subTest(event_type=event_type):
                provider = LLMProvider(
                    model="test-model",
                    api_key="test-key",
                    wire_api="responses",
                )
                provider.client = FakeClient(["unused"])
                response = SimpleNamespace(**{detail_name: "provider stopped"})
                provider.client.responses.events = [
                    SimpleNamespace(type=event_type, response=response)
                ]

                with self.assertRaises(LLMCallFailed) as caught:
                    provider.generate("prompt", max_retries=0)

                self.assertIn("Responses API 流式生成未完成", str(caught.exception))
                self.assertTrue(provider.client.responses.last_stream.closed)

    def test_responses_stream_error_event_is_retryable_failure(self):
        provider = LLMProvider(
            model="test-model",
            api_key="test-key",
            wire_api="responses",
        )
        provider.client = FakeClient(["unused"])
        provider.client.responses.events = [
            SimpleNamespace(type="error", message="connection dropped")
        ]

        with self.assertRaises(LLMCallFailed) as caught:
            provider.generate("prompt", max_retries=0)

        self.assertIn("Responses API 流式传输错误", str(caught.exception))
        self.assertTrue(provider.client.responses.last_stream.closed)

    def test_responses_stream_without_completion_discards_partial_text(self):
        provider = LLMProvider(
            model="test-model",
            api_key="test-key",
            wire_api="responses",
        )
        provider.client = FakeClient(["unused"])
        provider.client.responses.events = [
            SimpleNamespace(type="response.output_text.delta", delta="partial")
        ]

        with self.assertRaises(LLMCallFailed) as caught:
            provider.generate("prompt", max_retries=0)

        self.assertIn("完成事件前结束", str(caught.exception))
        self.assertTrue(provider.client.responses.last_stream.closed)

    def test_cancelable_responses_request_uses_same_streaming_path(self):
        provider = LLMProvider(
            model="test-model",
            api_key="test-key",
            wire_api="responses",
        )
        client = FakeClient(["unused"])
        provider._create_client = lambda: client

        result = provider.generate_cancelable(
            "prompt", threading.Event(), max_tokens=31, max_retries=0
        )

        self.assertEqual(result, "Responses 流式结果")
        self.assertEqual(client.responses.calls[0]["max_output_tokens"], 31)
        self.assertTrue(client.responses.last_stream.closed)
        self.assertTrue(client.closed)

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

    def test_cloudflare_timeout_honors_bounded_retry_after(self):
        error = FakeAPIError(
            "origin_response_timeout",
            status_code=524,
            body={"retry_after": 120},
        )

        self.assertEqual(_retry_delay_for_error(error, 0), 120)
        self.assertIn("服务商响应超时", _error_summary(error))
        self.assertIn("120 秒", _error_summary(error))

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
            with self.assertRaises(LLMCallFailed):
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
