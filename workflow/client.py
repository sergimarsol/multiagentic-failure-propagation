"""Small OpenAI-compatible HTTP client. Each complete() makes exactly one request."""

from dataclasses import asdict, dataclass
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import PLAN_SCHEMA


class ModelRequestError(RuntimeError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen2.5:7b"
    temperature: float = 0.0
    max_tokens: int = 1536
    timeout_seconds: float = 120.0
    plan_format: str = "json_schema"

    def __post_init__(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url must be an HTTP(S) endpoint without credentials, query or fragment")
        if not self.model or self.max_tokens <= 0 or self.timeout_seconds <= 0:
            raise ValueError("model, positive max_tokens and positive timeout are required")
        if self.plan_format not in {"json_schema", "json_object", "prompt_only"}:
            raise ValueError("plan_format must be json_schema, json_object or prompt_only")

    def public_dict(self):
        return asdict(self)


class OpenAICompatibleClient:
    def __init__(self, config=ModelConfig(), *, api_key="ollama"):
        self.config = config
        self._api_key = api_key

    def complete(self, messages, tools, *, json_output=False):
        body = dict(model=self.config.model, messages=messages, temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens, stream=False)
        if tools:
            body.update(tools=tools, tool_choice="auto")
        if json_output and self.config.plan_format != "prompt_only":
            body["response_format"] = ({"type": "json_schema", "json_schema": {
                "name": "banking_plan", "schema": PLAN_SCHEMA, "strict": False}}
                if self.config.plan_format == "json_schema" else {"type": "json_object"})
        request = Request(self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body, allow_nan=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"})
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(2000).decode(errors="replace")
            raise ModelRequestError(f"HTTP {exc.code}: {detail}", retryable=exc.code == 429 or exc.code >= 500) from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            raise ModelRequestError(str(exc), retryable=True) from exc
