import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from openai import OpenAI
from core.text_utils import normalize_text
from core.prompt_trace import record_prompt

# 不值得重试的 HTTP 状态码（认证/余额等确定性错误）
_NO_RETRY_CODES = {400, 401, 402, 403, 404, 405, 422}
_DEFAULT_RETRY_DELAY = 1.5
_MAX_RETRIES_LIMIT = 5
_WAIT_LOG_INTERVAL = 10.0
_STATUS_CALLBACK = ContextVar("harness_novel_llm_status_callback", default=None)


class LLMCallCancelled(RuntimeError):
    """模型请求被用户主动取消。"""


class LLMResponseFormatError(RuntimeError):
    """服务商返回了非 OpenAI 兼容的响应结构。"""


class LLMCallFailed(RuntimeError):
    """The provider exhausted its bounded attempts without a response."""

    def __init__(self, model, attempts, error):
        self.model = model
        self.attempts = attempts
        self.error = error
        super().__init__(
            f"模型 {model} 调用失败，已尝试 {attempts} 次：{error}"
        )


def _resolve_max_retries(requested: int) -> int:
    """Resolve a bounded retry count shared by synchronous and cancelable calls."""
    raw = os.getenv("HARNESS_NOVEL_LLM_MAX_RETRIES")
    try:
        value = int(raw) if raw is not None and raw.strip() else int(requested)
    except (TypeError, ValueError):
        value = int(requested)
    return max(0, min(_MAX_RETRIES_LIMIT, value))


def _retry_delay(attempt: int) -> float:
    """Return an exponential backoff delay, configurable but always bounded."""
    try:
        base = float(os.getenv("HARNESS_NOVEL_LLM_RETRY_DELAY", str(_DEFAULT_RETRY_DELAY)))
    except (TypeError, ValueError):
        base = _DEFAULT_RETRY_DELAY
    base = max(0.0, min(30.0, base))
    return min(30.0, base * (attempt + 1))


def _error_status(error) -> str:
    status_code = getattr(error, "status_code", None)
    return f"HTTP {status_code}: " if status_code else ""


def _response_content(response, base_url) -> str:
    """Extract chat content and turn proxy route mistakes into an actionable error."""
    choices = getattr(response, "choices", None)
    if not choices:
        response_type = type(response).__name__
        raise LLMResponseFormatError(
            "服务商返回的不是 OpenAI 兼容的 chat.completions 响应"
            f"（实际类型：{response_type}）。请检查 Base URL；多数服务商要求以 /v1 结尾。"
            f"当前地址：{base_url or '未设置'}"
        )
    return normalize_text(choices[0].message.content)


@contextmanager
def capture_llm_status(callback):
    """Expose provider attempt and wait status to the current Web job."""
    token = _STATUS_CALLBACK.set(callback)
    try:
        yield
    finally:
        _STATUS_CALLBACK.reset(token)


def _report_status(message: str) -> None:
    print(message)
    callback = _STATUS_CALLBACK.get()
    if callback is not None:
        callback(message)


class LLMProvider:
    """OpenAI 兼容接口的轻量封装。

    只负责真实 API 调用与有界重试；调用失败时抛出明确异常，避免上层把
    API 故障误判成模型格式错误并继续重复请求。
    """

    def __init__(self, model="mock-model", base_url=None, api_key=None, max_tokens=None):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.max_tokens = max_tokens
        try:
            self.timeout = max(
                30.0,
                float(os.getenv("HARNESS_NOVEL_LLM_TIMEOUT", "600")),
            )
        except (TypeError, ValueError):
            self.timeout = 600.0
        self.client = self._create_client() if self.api_key else None

    def _create_client(self):
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            # 由本封装统一控制重试次数，避免 SDK 内部重试时无法及时响应暂停/结束。
            max_retries=0,
        )

    def generate(self, prompt, temperature=0.7, is_json=False, max_retries=2, max_tokens=None):
        """调用大语言模型生成内容。

        成功返回归一化后的文本；未配置 api_key 或 API 调用失败（重试耗尽 /
        401/402/403 等确定性错误）时返回空字符串并打印警告。
        """
        record_prompt(prompt, self.model)
        if not self.client:
            print("[LLMProvider] 未配置 api_key，无法调用模型，返回空内容。")
            return ""

        response_format = {"type": "json_object"} if is_json else None
        messages = [{"role": "user", "content": prompt}]
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if is_json:
            kwargs["response_format"] = response_format

        max_retries = _resolve_max_retries(max_retries)
        total_attempts = max_retries + 1
        attempts_made = 0
        last_error = None
        for attempt in range(total_attempts):
            attempts_made = attempt + 1
            _report_status(
                f"[LLMProvider] 正在进行第 {attempts_made}/{total_attempts} 次调用：{self.model}"
            )
            try:
                response = self.client.chat.completions.create(**kwargs)
                return _response_content(response, self.base_url)
            except Exception as e:
                last_error = e
                status_code = getattr(e, 'status_code', None)
                if isinstance(e, LLMResponseFormatError):
                    _report_status(
                        f"[LLMProvider] 第 {attempts_made}/{total_attempts} 次调用失败，"
                        f"响应格式错误，不再重试：{e}"
                    )
                    break
                if status_code in _NO_RETRY_CODES:
                    _report_status(
                        f"[LLMProvider] 第 {attempts_made}/{total_attempts} 次调用失败，"
                        f"HTTP {status_code} 不可重试：{e}"
                    )
                    break
                if attempt < max_retries:
                    delay = _retry_delay(attempt)
                    _report_status(
                        f"[LLMProvider] 第 {attempts_made}/{total_attempts} 次调用失败；"
                        f"{delay:g} 秒后重试。错误：{e}"
                    )
                    if delay:
                        time.sleep(delay)
                else:
                    _report_status(
                        f"[LLMProvider] 第 {attempts_made}/{total_attempts} 次调用失败，"
                        f"已达到重试上限。错误：{e}"
                    )

        raise LLMCallFailed(self.model, attempts_made, last_error)

    def generate_cancelable(
        self,
        prompt,
        cancel_event,
        temperature=0.7,
        is_json=False,
        max_tokens=None,
        max_retries=2,
    ):
        """执行可取消、可重试的请求；取消后不会返回未完成内容。"""
        record_prompt(prompt, self.model)
        if not self.api_key:
            return ""
        max_retries = _resolve_max_retries(max_retries)
        total_attempts = max_retries + 1
        for attempt in range(total_attempts):
            if cancel_event is not None and cancel_event.is_set():
                raise LLMCallCancelled("模型请求已取消")
            _report_status(
                f"[LLMProvider] 正在进行第 {attempt + 1}/{total_attempts} 次调用：{self.model}"
            )
            done = threading.Event()
            outcome = {}
            client = self._create_client()

            def request():
                try:
                    kwargs = {
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": max_tokens or self.max_tokens,
                    }
                    if is_json:
                        kwargs["response_format"] = {"type": "json_object"}
                    response = client.chat.completions.create(**kwargs)
                    outcome["result"] = _response_content(response, self.base_url)
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    done.set()

            threading.Thread(target=request, name="llm-cancelable-call", daemon=True).start()
            started_at = time.monotonic()
            next_wait_log = started_at + _WAIT_LOG_INTERVAL
            while not done.wait(0.1):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        client.close()
                    except Exception:
                        pass
                    raise LLMCallCancelled("模型请求已取消")

                now = time.monotonic()
                if now >= next_wait_log:
                    elapsed = int(now - started_at)
                    _report_status(
                        f"[LLMProvider] 第 {attempt + 1}/{total_attempts} 次调用仍在等待，"
                        f"已等待约 {elapsed} 秒..."
                    )
                    next_wait_log = now + _WAIT_LOG_INTERVAL

            try:
                client.close()
            except Exception:
                pass
            if "error" not in outcome:
                return outcome.get("result", "")

            error = outcome["error"]
            status_code = getattr(error, "status_code", None)
            if (
                isinstance(error, LLMResponseFormatError)
                or status_code in _NO_RETRY_CODES
                or attempt >= max_retries
            ):
                _report_status(
                    f"[LLMProvider] 第 {attempt + 1}/{total_attempts} 次调用失败，"
                    f"不再重试：{_error_status(error)}{error}"
                )
                raise error

            wait_seconds = _retry_delay(attempt)
            _report_status(
                f"[LLMProvider] 第 {attempt + 1}/{total_attempts} 次调用失败；"
                f"{wait_seconds:g} 秒后重试。错误：{_error_status(error)}{error}"
            )
            if cancel_event is not None:
                if cancel_event.wait(wait_seconds):
                    raise LLMCallCancelled("模型请求已取消")
            else:
                threading.Event().wait(wait_seconds)

        # 循环仅为类型检查器保留；正常情况下成功返回或抛出最后一次异常。
        return ""
