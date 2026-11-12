"""LLM client abstraction. Providers receive ONLY system/prompt/temperature/
max_tokens -- `tag` is local bookkeeping and is never serialized."""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

import httpx

from app.config import settings
from app.exceptions import LLMError, LLMNotConfigured


@dataclass(frozen=True)
class LLMRequest:
    system: str
    prompt: str
    temperature: float = 0.3
    max_tokens: int = 1800
    tag: dict = field(default_factory=dict, compare=False)  # group/persona/round; never sent


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str = ""


class LLMClient(ABC):
    name: str = "llm"

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResponse: ...


class ScriptedLLM(LLMClient):
    """Test double. `responder(req) -> str`; may raise to simulate a dead agent."""

    name = "scripted"

    def __init__(self, responder: Callable[[LLMRequest], str]):
        self._responder = responder
        self.calls: list[LLMRequest] = []
        self._lock = threading.Lock()

    def complete(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            self.calls.append(req)
        return LLMResponse(text=self._responder(req), model="scripted")


_RETRY_STATUS = {429, 500, 502, 503, 504}


def _post_with_retry(url: str, *, headers: dict, json: dict, timeout: float, attempts: int = 3) -> dict:
    last = "unknown"
    for n in range(attempts):
        try:
            r = httpx.post(url, headers=headers, json=json, timeout=timeout)
        except httpx.TransportError as e:
            last = f"transport error {type(e).__name__}"
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise LLMError("provider returned non-JSON body") from None
            last = f"HTTP {r.status_code}"
            if r.status_code not in _RETRY_STATUS:
                raise LLMError(f"provider call failed: {last}")
        if n < attempts - 1:
            time.sleep(min(2**n, 8) * 0.5)
    raise LLMError(f"provider call failed after {attempts} attempts: {last}")


class AnthropicClient(LLMClient):
    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, timeout: float = 120.0):
        if not api_key:
            raise LLMNotConfigured("ANTHROPIC_API_KEY is not set")
        self._key, self.model, self._timeout = api_key, model, timeout

    def complete(self, req: LLMRequest) -> LLMResponse:
        body = _post_with_retry(
            self.URL,
            headers={"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": self.model,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
                "system": req.system,
                "messages": [{"role": "user", "content": req.prompt}],
            },
            timeout=self._timeout,
        )
        try:
            text = "".join(b.get("text", "") for b in body["content"] if b.get("type") == "text")
        except (KeyError, TypeError, AttributeError):
            raise LLMError("unexpected Anthropic response shape") from None
        return LLMResponse(text=text, model=self.model)


class OllamaClient(LLMClient):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float = 300.0):
        self._url = base_url.rstrip("/") + "/api/chat"
        self.model, self._timeout = model, timeout

    def complete(self, req: LLMRequest) -> LLMResponse:
        body = _post_with_retry(
            self._url,
            headers={"content-type": "application/json"},
            json={
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": req.system},
                    {"role": "user", "content": req.prompt},
                ],
                "options": {"temperature": req.temperature, "num_predict": req.max_tokens},
            },
            timeout=self._timeout,
        )
        try:
            return LLMResponse(text=body["message"]["content"], model=self.model)
        except (KeyError, TypeError):
            raise LLMError("unexpected Ollama response shape") from None


def get_llm_client() -> LLMClient:
    p = settings.llm_provider
    if p == "anthropic":
        return AnthropicClient(settings.anthropic_api_key.get_secret_value(), settings.llm_model, settings.llm_timeout_s)
    if p == "ollama":
        return OllamaClient(settings.ollama_base_url, settings.llm_model, settings.llm_timeout_s)
    raise LLMNotConfigured("LLM_PROVIDER is 'none'; set LLM_PROVIDER=anthropic or ollama to run a debate")
