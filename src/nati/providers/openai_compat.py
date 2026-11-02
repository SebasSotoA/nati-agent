"""The only network code in the project.

Talks to Ollama Cloud through its OpenAI Chat Completions-compatible endpoint,
authenticated with a Bearer API key. The `openai` client is pointed at
`base_url`; there is no extra SDK.

The contract with the rest of the program: `complete()` returns a RawTurn
carrying the response envelope VERBATIM. The loop persists that envelope before
interpreting anything, so extracting the Chain of Thought is a pure, re-runnable
function over stored files.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

from nati.config import Profile, api_key

# Some models leak their reasoning as <think>...</think> inside `content` when
# the server-side parser does not split it out. Both routes have to be read.
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5


class ProviderError(Exception):
    """A failure talking to the model, with an actionable message."""


@dataclass
class ToolCall:
    """A tool call requested by the model."""

    id: str
    name: str
    arguments_raw: str
    arguments: dict[str, Any] | None = None
    parse_ok: bool = False
    parse_error: str | None = None


@dataclass
class RawTurn:
    """One model turn, normalized, with the raw envelope intact."""

    content: str
    reasoning: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: dict[str, int]
    latency_ms: int
    model: str
    request_body: dict[str, Any]
    raw_response: dict[str, Any] = field(default_factory=dict)
    # The assistant message exactly as it arrived. Echoed back VERBATIM into the
    # history: losing or reordering blocks is what produces "worked once, 400 on
    # the third turn".
    assistant_message: dict[str, Any] = field(default_factory=dict)

    @property
    def reasoning_leaked(self) -> bool:
        """True if <think> had to be stripped out of content (a red-team surface)."""
        return bool(self._think_leak)

    _think_leak: bool = False


def _client(profile: Profile) -> OpenAI:
    return OpenAI(
        api_key=api_key(),
        base_url=profile.base_url,
        timeout=120.0,
        max_retries=0,  # we retry ourselves, so every attempt can be traced
    )


def _extract_reasoning(msg: dict[str, Any]) -> tuple[str, str, bool]:
    """Returns (clean_content, reasoning, leaked).

    Reasoning can arrive by three different routes depending on the backend:
      - `reasoning_content` (Bedrock mantle, vLLM)
      - `reasoning`         (some gateways)
      - <think>...</think> embedded in `content` (a parser leak)
    """
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "") or ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        # Some backends return blocks; keep the text.
        content = "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )

    leaked = False
    if content and "<think>" in content.lower():
        found = _THINK_RE.findall(content)
        if found:
            leaked = True
            reasoning = ("\n".join(found) + "\n" + reasoning).strip()
            content = _THINK_RE.sub("", content)
        # An unclosed <think>: everything after it is truncated reasoning.
        parts = re.split(r"<think>", content, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            leaked = True
            reasoning = (reasoning + "\n" + parts[1]).strip()
            content = parts[0]

    return content.strip(), reasoning.strip(), leaked


def _parse_tool_calls(msg: dict[str, Any]) -> list[ToolCall]:
    out: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        # Arguments should be a JSON string, but they are not always.
        if isinstance(raw, dict):
            out.append(
                ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=fn.get("name") or "",
                    arguments_raw=json.dumps(raw, ensure_ascii=False),
                    arguments=raw,
                    parse_ok=True,
                )
            )
            continue

        raw = raw or ""
        call = ToolCall(
            id=tc.get("id") or f"call_{i}",
            name=fn.get("name") or "",
            arguments_raw=raw,
        )
        try:
            parsed = json.loads(raw) if raw.strip() else {}
            if not isinstance(parsed, dict):
                raise ValueError("arguments are not a JSON object")
            call.arguments = parsed
            call.parse_ok = True
        except (json.JSONDecodeError, ValueError) as exc:
            call.parse_error = str(exc)
        out.append(call)
    return out


def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    profile: Profile,
) -> RawTurn:
    """One model turn. Retries throttling and network errors; never retries a 4xx."""
    body: dict[str, Any] = {
        "model": profile.model,
        "messages": messages,
        "max_tokens": profile.max_tokens,
        "temperature": profile.temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    # `reasoning_effort` is the standard Chat Completions way to ask for a Chain
    # of Thought. Models that have no separate reasoning channel ignore it
    # harmlessly, so it is safe to send whenever a profile asks for it.
    #
    # Worth knowing, because it cost an afternoon: some Bedrock models accept
    # `enable_thinking`, `reasoning_budget` and `chat_template_kwargs` WITHOUT
    # complaining and then return no reasoning at all. The failure is silent, so
    # `reasoning_effort` is the only field this code sends.
    if profile.reasoning_effort:
        body["reasoning_effort"] = profile.reasoning_effort

    client = _client(profile)
    last_error: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(**body)
        except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
            last_error = exc
            if attempt == _MAX_ATTEMPTS:
                break
            time.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))
            continue
        except APIStatusError as exc:
            if exc.status_code in (429, 500, 502, 503, 504):
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    break
                time.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))
                continue
            raise ProviderError(_http_message(exc, profile)) from exc

        latency_ms = int((time.monotonic() - t0) * 1000)
        raw = resp.model_dump()
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        content, reasoning, leaked = _extract_reasoning(msg)
        usage = raw.get("usage") or {}

        return RawTurn(
            content=content,
            reasoning=reasoning,
            tool_calls=_parse_tool_calls(msg),
            finish_reason=choice.get("finish_reason"),
            usage={
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
            },
            latency_ms=latency_ms,
            model=raw.get("model") or profile.model,
            request_body=body,
            raw_response=raw,
            assistant_message=msg,
            _think_leak=leaked,
        )

    raise ProviderError(
        f"Could not reach {profile.model} after {_MAX_ATTEMPTS} attempts.\n"
        f"  Last error: {last_error}"
    )


def _http_message(exc: APIStatusError, profile: Profile) -> str:
    """Translates the common 4xx codes into something actionable."""
    code = exc.status_code
    detail = str(exc)
    if code in (401, 403):
        return (
            f"Ollama rejected the key (HTTP {code}).\n"
            "  Check NATI_API_KEY: it may be expired, or lack permission\n"
            f"  for {profile.model}.\n"
            f"  Detail: {detail}"
        )
    if code == 404:
        return (
            f"Ollama cannot find the model '{profile.model}' (HTTP 404).\n"
            "  Check the id in profiles.toml (or NATI_MODEL).\n"
            f"  Detail: {detail}"
        )
    if code == 400:
        return (
            f"Ollama rejected the request (HTTP 400).\n"
            "  Common causes: a tool's parameters are not a JSON object, or a\n"
            "  tool returned empty content (some models reject that).\n"
            f"  Detail: {detail}"
        )
    return f"Ollama responded HTTP {code}.\n  Detail: {detail}"
