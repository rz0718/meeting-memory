"""Shared OpenRouter chat transport for durable-knowledge model calls."""

from __future__ import annotations

import json
import socket
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib import error, request

from .errors import (
    AuthenticationError,
    ConfigurationError,
    ExtractionError,
    TransientExtractionError,
)


class OpenRouterChatClient:
    """Small standard-library client for OpenRouter chat completions."""

    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        api_key: Optional[str],
        model: Optional[str],
        timeout: int = 120,
        opener: Optional[Callable[..., Any]] = None,
    ):
        if not api_key:
            raise ConfigurationError(
                "OPENROUTER_API_KEY or ANTHROPIC_AUTH_TOKEN is not configured"
            )
        if not model:
            raise ConfigurationError("an OpenRouter model is not configured")
        if timeout < 1:
            raise ValueError("timeout must be at least 1")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.opener = opener or request.urlopen

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        response_format: Optional[Mapping[str, str]] = None,
    ) -> Any:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": list(messages),
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        req = request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost/meeting-memory",
                "X-Title": "AI Meeting Memory Durable Knowledge",
            },
            method="POST",
        )
        try:
            with self.opener(req, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            detail = "OpenRouter HTTP %d: %s" % (exc.code, body[:500])
            if exc.code in (401, 402, 403):
                raise AuthenticationError(detail) from exc
            if exc.code in (408, 429, 500, 502, 503, 529):
                raise TransientExtractionError(detail) from exc
            raise ExtractionError(detail) from exc
        except (error.URLError, socket.timeout, TimeoutError) as exc:
            raise TransientExtractionError(
                "temporary provider connection failure: %s" % exc
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise ExtractionError(
                "provider returned an invalid response envelope"
            ) from exc
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExtractionError(
                "provider response did not contain message content"
            ) from exc
