"""Tests for the conversation loop and the tracer. NO NETWORK: complete() is faked.

The loop is where a user turn becomes model calls + tool calls, so the things
worth pinning are: the tool-call ids we send back match, a refusing tool is fed
back (not crashed) so the model can explain it, the Chain of Thought is traced
but never returned to the user, and a runaway loop is capped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nati import clock
from nati.agent import egress, loop, prompt
from nati.agent.tracer import Tracer, new_conv_id
from nati.config import Profile
from nati.providers import ProviderError
from nati.providers.openai_compat import RawTurn, ToolCall
from nati.world import db


@pytest.fixture
def con(tmp_path: Path):
    ru = tmp_path / "world.db"
    db.create(ru, reset=True)
    c = db.connect(ru)
    yield c
    c.close()


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(conv_id=new_conv_id(), operator="tester", root=tmp_path / "traces")


def _profile() -> Profile:
    return Profile(
        name="test", model="vendor.model-v1:0",
        base_url="https://x.invalid/v1", region="us-west-2",
        max_tokens=4096, reasoning_effort="high", temperature=0.3, description="",
    )


def _turn(*, content="", reasoning="", tool_calls=None) -> RawTurn:
    return RawTurn(
        content=content, reasoning=reasoning, tool_calls=tool_calls or [],
        finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15},
        latency_ms=1, model="vendor.model-v1:0", request_body={"messages": []},
        raw_response={"choices": [{"message": {"content": content}}]},
    )


def _fake_complete(script):
    """Returns a complete() that yields the scripted turns in order.

    When the script runs out it repeats the last turn, rather than raising. The
    output filter can ask the model to answer again after suppressing a reply, so
    a turn may need one more model call than the test scripted — and a test
    double should not turn that into a StopIteration.
    """
    remaining = list(script)

    def fake(messages, tools, profile):
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    return fake


def _run(con, tracer, text, script, monkeypatch, **kw):
    monkeypatch.setattr(loop, "complete", _fake_complete(script))
    history = loop.start_history()
    events = list(loop.run_turn(con, history, text, _profile(), tracer, **kw))
    return history, events


# --------------------------------------------------------------------------
# The happy path: a tool call, then a reply
# --------------------------------------------------------------------------


def test_a_tool_call_then_a_reply(con, tracer, monkeypatch) -> None:
    script = [
        _turn(
            reasoning="I should check the fund.",
            tool_calls=[ToolCall(id="c1", name="fund_status", arguments_raw="{}",
                                 arguments={}, parse_ok=True)],
        ),
        _turn(content="The fund has $15,732 in cash."),
    ]
    history, events = _run(con, tracer, "how much is in the fund?", script, monkeypatch)

    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Thinking", "ToolStep", "ToolStep", "Thinking", "Reply"]
    assert events[1].status == "start"
    assert events[2].status == "ok"
    assert events[-1].content == "The fund has $15,732 in cash."


def test_the_history_is_a_valid_tool_exchange(con, tracer, monkeypatch) -> None:
    """The assistant message must carry the tool_calls, and each tool message
    must reference an id that exists on it — or the next call is a 400."""
    script = [
        _turn(tool_calls=[ToolCall(id="c1", name="fund_status", arguments_raw="{}",
                                   arguments={}, parse_ok=True)]),
        _turn(content="done"),
    ]
    history, _ = _run(con, tracer, "x", script, monkeypatch)

    assistant = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
    tool_msg = next(m for m in history if m["role"] == "tool")
    ids_on_assistant = {tc["id"] for tc in assistant["tool_calls"]}
    assert tool_msg["tool_call_id"] in ids_on_assistant
    # the tool message content is the envelope, never empty
    assert json.loads(tool_msg["content"])["ok"] in (True, False)


# --------------------------------------------------------------------------
# A refusing tool is fed back, not crashed
# --------------------------------------------------------------------------


def test_a_refusing_tool_is_reported_and_the_turn_continues(con, tracer, monkeypatch) -> None:
    script = [
        _turn(tool_calls=[ToolCall(
            id="c1", name="record_saving",
            arguments_raw='{"member_id":2,"confirm_name":"Wrong Name","amount":50}',
            arguments={"member_id": 2, "confirm_name": "Wrong Name", "amount": 50},
            parse_ok=True)]),
        _turn(content="I couldn't record that — the name didn't match."),
    ]
    _, events = _run(con, tracer, "save 50 for member 2", script, monkeypatch)
    tool_steps = [e for e in events if isinstance(e, loop.ToolStep)]
    assert any(s.status == "refused" for s in tool_steps)
    assert isinstance(events[-1], loop.Reply)


def test_unparsable_tool_arguments_do_not_crash(con, tracer, monkeypatch) -> None:
    script = [
        _turn(tool_calls=[ToolCall(id="c1", name="fund_status",
                                   arguments_raw="{bad json", arguments=None,
                                   parse_ok=False, parse_error="boom")]),
        _turn(content="Let me try that again differently."),
    ]
    _, events = _run(con, tracer, "x", script, monkeypatch)
    assert any(isinstance(e, loop.ToolStep) and e.status == "error" for e in events)
    assert isinstance(events[-1], loop.Reply)


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


def test_a_provider_error_becomes_a_failure_not_an_exception(con, tracer, monkeypatch) -> None:
    def boom(messages, tools, profile):
        raise ProviderError("bedrock is down")

    monkeypatch.setattr(loop, "complete", boom)
    history = loop.start_history()
    events = list(loop.run_turn(con, history, "hi", _profile(), tracer))
    assert len(events) == 2  # Thinking, then Failure
    assert isinstance(events[-1], loop.Failure)


def test_a_runaway_tool_loop_is_capped(con, tracer, monkeypatch) -> None:
    # A model that asks for a tool forever.
    forever = _turn(tool_calls=[ToolCall(id="c", name="fund_status",
                                         arguments_raw="{}", arguments={},
                                         parse_ok=True)])

    def always_tool(messages, tools, profile):
        return forever

    monkeypatch.setattr(loop, "complete", always_tool)
    history = loop.start_history()
    events = list(loop.run_turn(con, history, "x", _profile(), tracer, max_steps=3))
    assert isinstance(events[-1], loop.Failure)
    assert sum(1 for e in events if isinstance(e, loop.Thinking)) == 3


# --------------------------------------------------------------------------
# The tracer
# --------------------------------------------------------------------------


def test_the_conversation_is_traced(con, tracer, monkeypatch) -> None:
    script = [
        _turn(reasoning="secret chain of thought",
              tool_calls=[ToolCall(id="c1", name="fund_status", arguments_raw="{}",
                                   arguments={}, parse_ok=True)]),
        _turn(content="The fund has $15,732."),
    ]
    # The server opens the conversation (writing meta.json) before the loop runs.
    tracer.conv_start({"model": "vendor.model-v1:0", "world_db_sha256": "abc"})
    _run(con, tracer, "how much?", script, monkeypatch)

    d = tracer.dir
    assert (d / "meta.json").exists()
    assert (d / "turns.jsonl").exists()
    # raw request AND response envelopes persisted verbatim, per model call
    assert (d / "raw" / "0001.request.json").exists()
    assert (d / "raw" / "0001.response.json").exists()

    lines = [json.loads(x) for x in (d / "turns.jsonl").read_text().splitlines()]
    types = [x["type"] for x in lines]
    assert "conv_start" in types
    assert "user_message" in types
    assert "llm_response" in types
    assert "tool_result" in types
    assert "conv_end" in types

    # The Chain of Thought is recorded in full in the trace...
    llm = next(x for x in lines if x["type"] == "llm_response")
    assert llm["data"]["reasoning"] == "secret chain of thought"


def test_the_reasoning_never_reaches_the_user(con, tracer, monkeypatch) -> None:
    """...but it is never in what the loop yields as the reply."""
    script = [
        _turn(reasoning="internal deliberation the user must not see",
              content="The fund has $15,732."),
    ]
    _, events = _run(con, tracer, "how much?", script, monkeypatch)
    reply = next(e for e in events if isinstance(e, loop.Reply))
    assert "internal deliberation" not in reply.content


def test_meta_pins_the_setup(con, tracer, monkeypatch) -> None:
    tracer.conv_start({"model": "vendor.model-v1:0", "world_db_sha256": "abc"})
    meta = json.loads((tracer.dir / "meta.json").read_text())
    assert meta["conv_id"] == tracer.conv_id
    assert meta["operator"] == "tester"
    assert meta["model"] == "vendor.model-v1:0"


# --------------------------------------------------------------------------
# The system prompt does not hand over the map
# --------------------------------------------------------------------------


def test_the_prompt_does_not_leak_internals() -> None:
    text = prompt.system_prompt().lower()
    for forbidden in ("member_id", "lender_id", "sql", "trigger", "sop",
                      "fund_status", "record_saving", "hard", "soft"):
        assert forbidden not in text, f"prompt leaks {forbidden!r}"


def test_the_prompt_keeps_nati_in_scope() -> None:
    """SOP G13. A red-team run got Nati to answer a Python question in full.

    Dissolving the persona is the on-ramp to everything else: once Nati is
    behaving as a general assistant, the natillera's rules read as suggestions.
    Only the prompt can hold this line, so the line has to stay in the prompt.
    """
    text = prompt.system_prompt().lower()
    assert "not this natillera's" in text or "not write code" in text
    assert "come back to the books" in text


def test_the_prompt_states_todays_date() -> None:
    from nati import clock
    with clock.frozen("2026-08-04"):
        assert "2026-08-04" in prompt.system_prompt()


def test_raw_envelopes_survive_across_turns(con, tracer, monkeypatch) -> None:
    """REGRESSION: turn 2 used to overwrite turn 1's raw/0001.*.

    The file number came from the loop's per-turn call index, which resets to 1
    on every new question, so a three-question conversation kept the raw
    envelopes of only the last one. The Chain of Thought survived in
    turns.jsonl, but the byte-exact envelopes — the thing that makes a trace
    usable as evidence — did not. One file per model call, for the whole
    conversation.
    """
    tracer.conv_start({"model": "vendor.model-v1:0", "world_db_sha256": "abc"})
    history = loop.start_history()

    # Turn 1: one model call. Turn 2: one. Turn 3: a tool, so two.
    scripts = [
        [_turn(content="I do not write code.")],
        [_turn(content="I cannot help with that.")],
        [
            _turn(tool_calls=[ToolCall(id="c1", name="fund_status",
                                       arguments_raw="{}", arguments={},
                                       parse_ok=True)]),
            _turn(content="The fund has $15,732 in cash."),
        ],
    ]
    for script in scripts:
        monkeypatch.setattr(loop, "complete", _fake_complete(script))
        list(loop.run_turn(con, history, "a question", _profile(), tracer))

    raw = sorted(p.name for p in (tracer.dir / "raw").iterdir())
    assert raw == [
        "0001.request.json", "0001.response.json",
        "0002.request.json", "0002.response.json",
        "0003.request.json", "0003.response.json",
        "0004.request.json", "0004.response.json",
    ], f"envelopes were overwritten: {raw}"

    # Every model call in turns.jsonl has a raw file, and the numbering matches.
    lines = [json.loads(x) for x in (tracer.dir / "turns.jsonl").read_text().splitlines()]
    calls = [x["data"]["call"] for x in lines if x["type"] == "llm_request"]
    assert calls == [1, 2, 3, 4]

    # Each response file pairs with its own request (same number), so a
    # reviewer can line up what was sent with what came back.
    for n in (1, 2, 3, 4):
        assert (tracer.dir / "raw" / f"{n:04d}.request.json").exists()
        assert (tracer.dir / "raw" / f"{n:04d}.response.json").exists()

    # NOTE: that turn 1's request body is turn 1's (and not the accumulated
    # history) cannot be asserted here — the fake `complete()` returns an empty
    # message list. The committed example traces demonstrate it against the
    # real model instead.


# --------------------------------------------------------------------------
# A suppressed reply is not the end of the turn
# --------------------------------------------------------------------------


def test_a_blocked_reply_gets_one_retry_and_a_real_answer(con, tracer, monkeypatch) -> None:
    """Asking "what can I do if someone will not pay?" is ordinary bookkeeping.

    The model answered it by naming a tool, printing an example call and
    inventing $4.20 of interest — so the reply was rightly suppressed, and the
    administrator got a sentence about instructions and NO answer. Telling the
    model what was wrong lets it say the same thing properly.
    """
    script = [
        # First attempt: invents a figure no tool returned.
        _turn(content="He owes $210, and with interest that is $214.20."),
        # After the correction: only figures it was actually given.
        _turn(content="He owes $210, and it comes out of his year-end share."),
    ]
    # She names the $210 herself, as people do — so that figure is legitimately
    # speakable. The invented $214.20 is not.
    history, events = _run(con, tracer,
                           "what if Henry will not pay the $210 he owes?",
                           script, monkeypatch)

    kinds = [type(e).__name__ for e in events]
    assert "Retrying" in kinds, "the turn ended instead of asking again"

    reply = next(e for e in events if isinstance(e, loop.Reply))
    assert "214.20" not in reply.content, "the invented figure reached the user"
    assert "$210" in reply.content, "the real answer was not delivered"

    # The correction goes in as a SYSTEM instruction, not a user turn. As a user
    # turn it read like a fresh question from the administrator and displaced the
    # real one — asked "what are your available tools?", the model answered "I do
    # not have any amounts to share". It is also not trusted text for the money
    # allowlist, which closes a laundering path.
    corrections = [m for m in history
                   if m["role"] == "system" and "NOT sent" in str(m["content"])]
    assert len(corrections) == 1
    # It must NOT quote the invented figure: the correction is a message in the
    # conversation, and figures the administrator types are trusted, so repeating
    # it back would launder it onto the allowlist.
    assert "214.20" not in corrections[0]["content"]
    assert "no tool returned" in corrections[0]["content"]
    # And it repeats her question, so the model cannot lose it.
    assert "Henry" in corrections[0]["content"]


def test_the_retry_happens_at_most_once(con, tracer, monkeypatch) -> None:
    """One correction, not a hill to climb toward wording that slips through."""
    bad = _turn(content="It is worth about $91,234 all told.")
    monkeypatch.setattr(loop, "complete", _fake_complete([bad]))
    history = loop.start_history()
    events = list(loop.run_turn(con, history, "how much?", _profile(), tracer))

    assert sum(1 for e in events if isinstance(e, loop.Retrying)) == 1
    reply = next(e for e in events if isinstance(e, loop.Reply))
    assert "91,234" not in reply.content
    # Having failed twice, she is told the truth about WHY.
    assert reply.content == egress.BLOCKED_REPLY


def test_the_message_matches_what_actually_went_wrong(con, tracer, monkeypatch) -> None:
    """Every cause used to collapse into whichever rule sat first in review()."""
    cases = [
        ("He owes $214.20 in total.", egress.BLOCKED_REPLY),
        ("I would check that with the fund_status tool.", egress.BLOCKED_LEAK_REPLY),
        ("Here you go:\n```python\nprint(1)\n```", egress.BLOCKED_OFF_DOMAIN_REPLY),
    ]
    for text, expected in cases:
        bad = _turn(content=text)
        monkeypatch.setattr(loop, "complete", _fake_complete([bad]))
        history = loop.start_history()
        events = list(loop.run_turn(con, history, "a question", _profile(), tracer))
        reply = next(e for e in events if isinstance(e, loop.Reply))
        assert reply.content == expected, f"wrong message for {text!r}"


def test_money_is_reported_before_a_named_tool(con, tracer, monkeypatch) -> None:
    """A reply can break several rules at once. The worst one decides the message.

    The reply that started all this named three tools, printed a code block AND
    invented cents. Whichever check sat first in the function decided what the
    administrator was told — so she heard about instructions when the real
    problem was a figure she might have repeated to somebody.
    """
    with clock.frozen("2026-08-04"):
        v = egress.review(
            "Use the fund_status tool; he owes $214.20.\n```json\n{}\n```",
            [], "",
        )
    assert v.cause == egress.FIGURES
    assert v.reply == egress.BLOCKED_REPLY
