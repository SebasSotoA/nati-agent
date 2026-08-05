"""Tests for the provider. NO NETWORK: a fake client is injected.

The provider is the only code that talks to the model, and it does the most
delicate parsing in the project: where the reasoning comes from (three possible
fields, depending on the backend) and how a tool call's arguments are read (they
do not always arrive well formed).

It is also where a silent bug already happened: Bedrock accepted
`enable_thinking` and `reasoning_budget` without complaint and returned ZERO
reasoning. The Chain of Thought is a deliverable of this exercise, so the correct
parameter is pinned by a test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nati.config import Profile
from nati.providers import openai_compat as prov

# A deliberately fake id. The provider must not care which model is configured,
# so no test pins a real one.
FAKE_MODEL = "vendor.some-model-v1:0"

# --------------------------------------------------------------------------
# Where the reasoning comes from
# --------------------------------------------------------------------------


def test_reasoning_from_reasoning_content() -> None:
    """vLLM and some backends send it like this."""
    content, reasoning, leaked = prov._extract_reasoning(
        {"content": "The cash is $100.", "reasoning_content": "I add 60 and 40"}
    )
    assert content == "The cash is $100."
    assert reasoning == "I add 60 and 40"
    assert not leaked


def test_reasoning_from_the_reasoning_field() -> None:
    """The field Bedrock actually USES. Verified live against bedrock-mantle."""
    content, reasoning, leaked = prov._extract_reasoning(
        {"content": "That is $290.", "reasoning": "29 members x $10"}
    )
    assert content == "That is $290."
    assert reasoning == "29 members x $10"
    assert not leaked


def test_an_embedded_think_block_is_taken_out_of_content() -> None:
    """If the server's parser does not split it, <think> leaks into content.

    It has to come out: showing the raw reasoning to the administrator is itself
    an attack surface.
    """
    content, reasoning, leaked = prov._extract_reasoning(
        {"content": "<think>this request is odd, better not</think>I cannot do that."}
    )
    assert content == "I cannot do that."
    assert "this request is odd" in reasoning
    assert leaked is True


def test_an_unclosed_think_block_is_also_taken_out() -> None:
    """Reasoning truncated by the token cap: there is no closing tag."""
    content, reasoning, leaked = prov._extract_reasoning(
        {"content": "Done.<think>I was thinking and got cut o"}
    )
    assert content == "Done."
    assert "I was thinking" in reasoning
    assert leaked is True


def test_a_think_block_combines_with_the_reasoning_field() -> None:
    content, reasoning, leaked = prov._extract_reasoning(
        {"content": "<think>part A</think>Answer.", "reasoning": "part B"}
    )
    assert content == "Answer."
    assert "part A" in reasoning and "part B" in reasoning
    assert leaked is True


def test_block_style_content_is_flattened() -> None:
    content, reasoning, _ = prov._extract_reasoning(
        {"content": [{"text": "Hello "}, {"text": "world"}, {"other": "ignored"}]}
    )
    assert content == "Hello world"
    assert reasoning == ""


def test_no_reasoning_means_nothing_is_invented() -> None:
    assert prov._extract_reasoning({"content": "Just text."}) == (
        "Just text.", "", False,
    )


def test_an_empty_message_does_not_blow_up() -> None:
    assert prov._extract_reasoning({}) == ("", "", False)
    assert prov._extract_reasoning({"content": None, "reasoning": None}) == (
        "", "", False,
    )


# --------------------------------------------------------------------------
# Tool call arguments
# --------------------------------------------------------------------------


def _tc(name: str, args: Any, call_id: str | None = "call_abc") -> dict:
    return {"tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args}}]}


def test_arguments_as_a_json_string() -> None:
    """The normal case."""
    (call,) = prov._parse_tool_calls(
        _tc("record_saving", '{"member_id": 3, "amount": 50}')
    )
    assert call.parse_ok
    assert call.arguments == {"member_id": 3, "amount": 50}
    assert call.name == "record_saving"
    assert call.id == "call_abc"


def test_arguments_as_an_object() -> None:
    """Some backends send the object already parsed."""
    (call,) = prov._parse_tool_calls(_tc("fund_status", {"date": "2026-08-04"}))
    assert call.parse_ok
    assert call.arguments == {"date": "2026-08-04"}
    # arguments_raw is kept for the trace, always as text.
    assert json.loads(call.arguments_raw) == {"date": "2026-08-04"}


def test_empty_arguments_mean_an_empty_object() -> None:
    """A tool with no parameters: valid, not an error."""
    (call,) = prov._parse_tool_calls(_tc("members", ""))
    assert call.parse_ok
    assert call.arguments == {}


def test_bad_json_does_not_kill_the_loop() -> None:
    """The error is flagged and the raw text kept; the loop decides what to do.

    If this raised, a confused model could end the conversation — and that would
    be a trivial denial of service.
    """
    (call,) = prov._parse_tool_calls(_tc("record_payment", '{"amount": 50'))
    assert not call.parse_ok
    assert call.arguments is None
    assert call.parse_error
    assert call.arguments_raw == '{"amount": 50'


def test_json_that_is_not_an_object_is_refused() -> None:
    """A list or a number will not do as a tool's arguments."""
    for raw in ("[1, 2, 3]", '"just text"', "42"):
        (call,) = prov._parse_tool_calls(_tc("x", raw))
        assert not call.parse_ok, f"{raw!r} should be refused"
        assert "JSON object" in (call.parse_error or "")


def test_a_missing_id_is_synthesized_stably() -> None:
    """The id is required to answer the model. If it is missing, one is made up."""
    calls = prov._parse_tool_calls(
        {
            "tool_calls": [
                {"function": {"name": "a", "arguments": "{}"}},
                {"function": {"name": "b", "arguments": "{}"}},
            ]
        }
    )
    assert [c.id for c in calls] == ["call_0", "call_1"]


def test_several_calls_in_one_turn() -> None:
    calls = prov._parse_tool_calls(
        {
            "tool_calls": [
                {"id": "1", "function": {"name": "fund_status", "arguments": "{}"}},
                {"id": "2", "function": {"name": "members", "arguments": "{}"}},
            ]
        }
    )
    assert [c.name for c in calls] == ["fund_status", "members"]


def test_no_tool_calls_returns_an_empty_list() -> None:
    assert prov._parse_tool_calls({"content": "hello"}) == []
    assert prov._parse_tool_calls({"tool_calls": None}) == []


# --------------------------------------------------------------------------
# The request body: the Chain of Thought regression
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self) -> dict:
        return self._payload


class _FakeClient:
    """Captures the body that was sent and returns a fixed response. No network."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.received: dict = {}
        self.chat = self  # type: ignore[assignment]
        self.completions = self  # type: ignore[assignment]

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.received = kwargs
        return _FakeResponse(self.payload)


def _profile(**overrides: Any) -> Profile:
    base = {
        "name": "test",
        "model": FAKE_MODEL,
        "base_url": "https://example.invalid/v1",
        "region": "us-west-2",
        "max_tokens": 4096,
        "reasoning_effort": "high",
        "temperature": 0.3,
        "description": "",
    }
    return Profile(**{**base, **overrides})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    payload = {
        "model": FAKE_MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "The fund has $15,732 in cash.",
                    "reasoning": "I check the fund and read the cash",
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }
    fake = _FakeClient(payload)
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    return fake


def test_reasoning_effort_is_sent(client: _FakeClient) -> None:
    """REGRESSION: without this parameter Bedrock returns NO Chain of Thought.

    Bedrock accepts enable_thinking / reasoning_budget without complaint and
    answers with no reasoning at all. The bug is silent, so it is pinned here.
    """
    prov.complete([{"role": "user", "content": "hi"}], None, _profile())
    assert client.received["reasoning_effort"] == "high"
    # And what Bedrock silently ignores is NOT sent.
    assert "extra_body" not in client.received
    assert "enable_thinking" not in client.received
    assert "reasoning_budget" not in client.received


def test_without_reasoning_effort_the_field_is_omitted(client: _FakeClient) -> None:
    prov.complete(
        [{"role": "user", "content": "hi"}], None, _profile(reasoning_effort="")
    )
    assert "reasoning_effort" not in client.received


def test_tools_are_sent_with_tool_choice(client: _FakeClient) -> None:
    tools = [
        {"type": "function",
         "function": {"name": "fund_status", "parameters": {"type": "object"}}}
    ]
    prov.complete([{"role": "user", "content": "hi"}], tools, _profile())
    assert client.received["tools"] == tools
    assert client.received["tool_choice"] == "auto"
    # The schema must travel as an OBJECT, not a string: that is Bedrock's most
    # common ValidationException.
    assert isinstance(client.received["tools"][0]["function"]["parameters"], dict)


def test_without_tools_no_tool_choice_is_sent(client: _FakeClient) -> None:
    prov.complete([{"role": "user", "content": "hi"}], None, _profile())
    assert "tools" not in client.received
    assert "tool_choice" not in client.received


def test_the_profiles_caps_are_respected(client: _FakeClient) -> None:
    prov.complete(
        [{"role": "user", "content": "hi"}], None,
        _profile(max_tokens=1234, temperature=0.7),
    )
    assert client.received["max_tokens"] == 1234
    assert client.received["temperature"] == 0.7


# --------------------------------------------------------------------------
# The returned turn
# --------------------------------------------------------------------------


def test_the_turn_keeps_the_raw_envelope(client: _FakeClient) -> None:
    """The verbatim envelope is the evidence: the trace stores it before
    interpreting anything."""
    turn = prov.complete([{"role": "user", "content": "hi"}], None, _profile())
    assert turn.raw_response == client.payload
    assert turn.content == "The fund has $15,732 in cash."
    assert turn.reasoning == "I check the fund and read the cash"
    assert turn.finish_reason == "stop"
    assert turn.usage == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
    }
    assert turn.latency_ms >= 0


def test_the_assistant_message_is_kept_verbatim(client: _FakeClient) -> None:
    """It is echoed back into the history unchanged.

    Losing or reordering blocks is what produces "worked once, 400 on the third
    turn".
    """
    turn = prov.complete([{"role": "user", "content": "hi"}], None, _profile())
    assert turn.assistant_message == client.payload["choices"][0]["message"]


def test_a_missing_usage_block_does_not_blow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient({"choices": [{"message": {"content": "hi"}}]})
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    turn = prov.complete([{"role": "user", "content": "x"}], None, _profile())
    assert turn.usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert turn.finish_reason is None


def test_a_response_with_no_choices_does_not_blow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient({})
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    turn = prov.complete([{"role": "user", "content": "x"}], None, _profile())
    assert turn.content == ""
    assert turn.tool_calls == []


def test_a_leaked_think_block_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {"choices": [{"message": {"content": "<think>hmm</think>Done."}}]}
    )
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    turn = prov.complete([{"role": "user", "content": "x"}], None, _profile())
    assert turn.content == "Done."
    assert turn.reasoning_leaked is True


# --------------------------------------------------------------------------
# HTTP error translation
# --------------------------------------------------------------------------


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status


@pytest.mark.parametrize(
    ("status", "must_say"),
    [
        (401, "rejected the key"),
        (403, "rejected the key"),
        (404, "cannot find the model"),
        (400, "JSON object"),
        (418, "HTTP 418"),
    ],
)
def test_http_errors_explain_what_to_do(status: int, must_say: str) -> None:
    message = prov._http_message(_HttpError(status), _profile())  # type: ignore[arg-type]
    assert must_say in message


def test_the_400_mentions_the_empty_tool_result_cause() -> None:
    """Some models reject empty tool results; the message should say so."""
    message = prov._http_message(_HttpError(400), _profile())  # type: ignore[arg-type]
    assert "empty content" in message


# --------------------------------------------------------------------------
# Retries: what is retried and what is NOT
# --------------------------------------------------------------------------


def _status_error(status: int) -> Exception:
    """A real APIStatusError, which is what the code catches."""
    import httpx
    from openai import APIStatusError

    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, text="failed")
    return APIStatusError(f"HTTP {status}", response=response, body=None)


class _FailingClient:
    """Fails the first N times and then answers correctly."""

    def __init__(self, error: Exception, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.attempts = 0
        self.chat = self  # type: ignore[assignment]
        self.completions = self  # type: ignore[assignment]

    def create(self, **_kwargs: Any) -> _FakeResponse:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error
        return _FakeResponse(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backoff is real; in tests we do not actually want to wait."""
    monkeypatch.setattr(prov.time, "sleep", lambda _s: None)


def test_throttling_is_retried_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    """429 is transient: the key is shared by the whole team."""
    fake = _FailingClient(_status_error(429), failures=2)
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    turn = prov.complete([{"role": "user", "content": "x"}], None, _profile())
    assert turn.content == "ok"
    assert fake.attempts == 3


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None, status: int
) -> None:
    fake = _FailingClient(_status_error(status), failures=1)
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    assert prov.complete(
        [{"role": "user", "content": "x"}], None, _profile()
    ).content == "ok"
    assert fake.attempts == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_our_own_errors_are_NOT_retried(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None, status: int
) -> None:
    """A 4xx is our bug, not something transient.

    Retrying it four times spends four calls on a shared key and buries the real
    cause behind a "could not connect" message.
    """
    fake = _FailingClient(_status_error(status), failures=99)
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    with pytest.raises(prov.ProviderError):
        prov.complete([{"role": "user", "content": "x"}], None, _profile())
    assert fake.attempts == 1, "a 4xx must not be retried"


def test_it_gives_up_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    fake = _FailingClient(_status_error(503), failures=99)
    monkeypatch.setattr(prov, "_client", lambda _p: fake)
    with pytest.raises(prov.ProviderError, match="attempts"):
        prov.complete([{"role": "user", "content": "x"}], None, _profile())
    assert fake.attempts == prov._MAX_ATTEMPTS
