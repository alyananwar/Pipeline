"""One LLM client for every provider the tool supports.

Deliberately talks raw HTTP instead of pulling in the anthropic and openai SDKs.
Both APIs are a single POST, and skipping the SDKs keeps `pip install` small and
stops the two libraries from fighting over their shared httpx dependency.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

import httpx

from pipeline.settings import ConfigError, LLMConfig

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

MAX_RETRIES = 3
BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT = 120.0
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or will not return usable output."""


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def _extract_first_object(text: str) -> str:
    """Pull the outermost {...} out of a reply that wrapped it in prose."""
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text


class LLMClient:
    """Provider-agnostic chat client returning text or parsed JSON."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._client = httpx.Client(timeout=REQUEST_TIMEOUT)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- public ---------------------------------------------------------------

    def complete_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> str:
        """Return the model's reply as plain text."""
        return self._request(system, user, max_tokens, temperature)

    def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        """Return the model's reply parsed as a JSON object.

        Retries with a lower temperature when the model returns something that
        will not parse, which is the common failure on smaller local models.
        """
        last_error: Optional[str] = None
        for attempt in range(MAX_RETRIES):
            temp = temperature * (0.5 ** attempt)
            raw = self._request(system, user, max_tokens, temp)
            candidate = _extract_first_object(_strip_fences(raw))
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_error = str(exc)
                logger.debug("JSON parse failed (attempt %d): %s", attempt + 1, exc)
                continue
            if isinstance(parsed, dict):
                return parsed
            last_error = "model returned {0}, expected an object".format(type(parsed).__name__)

        raise LLMError(
            "Model did not return valid JSON after {0} attempts: {1}".format(
                MAX_RETRIES, last_error
            )
        )

    # -- transport ------------------------------------------------------------

    def _request(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        url, headers, payload = (
            self._anthropic_request(system, user, max_tokens, temperature)
            if self.config.is_anthropic
            else self._openai_request(system, user, max_tokens, temperature)
        )

        last_error: Optional[str] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                last_error = "network error: {0}".format(exc)
                logger.debug("LLM request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
                continue

            if response.status_code == 200:
                return self._read_reply(response.json())

            # 429 and 5xx are worth another try. Everything else is our fault.
            if response.status_code == 429 or response.status_code >= 500:
                last_error = "HTTP {0}: {1}".format(response.status_code, response.text[:200])
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
                continue

            raise LLMError(
                "{0} rejected the request (HTTP {1}): {2}".format(
                    self.config.provider, response.status_code, response.text[:400]
                )
            )

        raise LLMError("LLM unreachable after {0} attempts. {1}".format(MAX_RETRIES, last_error))

    def _anthropic_request(self, system: str, user: str, max_tokens: int, temperature: float):
        if not self.config.api_key:
            raise ConfigError("Anthropic requires LLM_API_KEY.")
        base = self.config.base_url or "https://api.anthropic.com"
        return (
            "{0}/v1/messages".format(base.rstrip("/")),
            {
                "x-api-key": self.config.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            {
                "model": self.config.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )

    def _openai_request(self, system: str, user: str, max_tokens: int, temperature: float):
        base = (self.config.base_url or "").rstrip("/")
        if not base:
            raise ConfigError("LLM_BASE_URL is required for provider '{0}'.".format(self.config.provider))
        headers = {"content-type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer {0}".format(self.config.api_key)
        return (
            "{0}/chat/completions".format(base),
            headers,
            {
                "model": self.config.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )

    def _read_reply(self, body: Dict[str, Any]) -> str:
        if self.config.is_anthropic:
            blocks = body.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        else:
            choices = body.get("choices") or []
            text = choices[0].get("message", {}).get("content", "") if choices else ""

        if not text.strip():
            raise LLMError("Model returned an empty reply.")
        return text
