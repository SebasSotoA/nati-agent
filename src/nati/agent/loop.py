"""The agent's tool loop — the heart of a conversation turn.

One user message can take several model calls: the model asks for a tool, we run
it, feed the result back, and let the model decide what to do next, until it
produces a plain reply. This function drives that, and it is written as a
**generator of typed events** so the exact same code powers the web stream, a
terminal REPL, and the tests — none of them branch on the caller.

Design notes that matter:

- **The tool-call ids we send back always match.** We rebuild the assistant
  message from the parsed tool calls rather than echoing the raw one, so the
  `tool_call_id` on each tool result is guaranteed to exist on the assistant
  message. A mismatch there is the classic "worked once, 400 next turn".

- **A tool that refuses is not an error.** Its refusal envelope is fed back to
  the model as the tool result, exactly like a success, so the model can explain
  it. The model never sees the internal refusal `reason` — only the message.

- **Everything is traced before it is interpreted.** The raw request and
  response envelopes are written verbatim; the reasoning (Chain of Thought) is
  recorded in full even though it is never shown to the user.

- **A hard step cap.** A model that loops on tools cannot run forever.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from nati.agent import egress
from nati.agent.prompt import system_prompt
from nati.agent.tracer import Tracer
from nati.config import Profile
from nati.providers import ProviderError, complete
from nati.tools import confirm, registry
from nati.world import db

MAX_STEPS = 8  # model calls per user message; a loop backstop, not a normal limit


# --------------------------------------------------------------------------
# Events the loop yields
# --------------------------------------------------------------------------


@dataclass
class Thinking:
    """The model is about to be called. UI shows a typing indicator."""


@dataclass
class ToolStep:
    """A tool was called. `status` is start | ok | refused | error.

    `reason` is the INTERNAL refusal code (`before_joining`, `nothing_owed`, …).
    It is never sent to the model — that would let it enumerate the whole
    taxonomy of controls — but the administrator debugging her own agent is not
    the model, so it travels to the UI, where it is the fastest way to see which
    guard fired.
    """

    name: str
    status: str
    message: str = ""
    reason: str = ""


@dataclass
class Retrying:
    """The filter suppressed a reply; the model is answering again.

    Surfaced so the administrator sees the control working instead of a longer
    silence — and so a teammate can tell "the filter caught something" apart from
    "the agent is broken".

    The remaining fields exist because "held back an amount no tool had returned"
    is unactionable on its own: it does not say WHICH amount, nor what the
    allowlist held, so a false positive is indistinguishable from a real catch
    without opening the trace. They are for the UI only and never reach the model.
    """

    cause: str
    unverified: list[str] = field(default_factory=list)
    disclosed: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    withheld: str = ""


@dataclass
class Reply:
    """The final assistant text for this turn."""

    content: str


@dataclass
class Failure:
    """The turn could not complete (model unreachable, step cap, etc.)."""

    message: str


Event = Thinking | ToolStep | Retrying | Reply | Failure


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def start_history() -> list[dict[str, Any]]:
    """A fresh message history, seeded with the system prompt."""
    return [{"role": "system", "content": system_prompt()}]


def _assistant_message(content: str, tool_calls: list) -> dict[str, Any]:
    """Rebuild the assistant message so the ids we reference always exist."""
    msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments_raw},
            }
            for tc in tool_calls
        ]
    return msg


def run_turn(
    con,
    history: list[dict[str, Any]],
    user_text: str,
    profile: Profile,
    tracer: Tracer,
    *,
    max_steps: int = MAX_STEPS,
) -> Iterator[Event]:
    """Run one user turn to completion, yielding events as it goes.

    Mutates `history` in place (so the caller keeps the conversation), and yields
    exactly one terminal event: a `Reply` or a `Failure`.
    """
    history.append({"role": "user", "content": user_text})
    tracer.user_message(user_text)

    # Which of her messages this is. Confirmation tokens are only spendable in a
    # LATER turn than the one that minted them, so this is what makes her "yes"
    # mechanically necessary rather than merely expected.
    user_turn = sum(1 for m in history if m.get("role") == "user")
    audit_seq = 0
    wrote_this_turn = False
    retried = False        # at most one correction per turn
    # Confirmation requests raised during this turn. They only become spendable
    # once she has actually been shown one, which is the moment this turn ends
    # with a reply she can read — see the `Reply` branch below.
    offered_tokens: list[str] = []
    for call_index in range(1, max_steps + 1):
        yield Thinking()

        try:
            turn = complete(history, registry.schemas(), profile)
        except ProviderError:
            tracer.conv_end("provider_error", {"model_calls": call_index})
            yield Failure(
                "I could not reach the model just now. Please try again in a moment."
            )
            return

        tracer.llm_request(turn.request_body)
        tracer.llm_response(
            content=turn.content,
            reasoning=turn.reasoning,
            reasoning_leaked=turn.reasoning_leaked,
            tool_calls=[
                {"id": tc.id, "name": tc.name, "arguments_raw": tc.arguments_raw,
                 "parse_ok": tc.parse_ok}
                for tc in turn.tool_calls
            ],
            usage=turn.usage,
            latency_ms=turn.latency_ms,
            raw_response=turn.raw_response,
        )

        history.append(_assistant_message(turn.content, turn.tool_calls))

        if not turn.tool_calls:
            # The LAST thing before the user: the output filter. It strips a
            # leaked Chain of Thought, blocks a prompt extraction, and blocks
            # any figure no tool returned in this conversation. The allowlist
            # comes from `history`, which already holds every tool result and
            # every user message — so a figure established in an earlier turn
            # can still be discussed in this one.
            envelopes, said = egress.conversation_context(history)
            verdict = egress.review(turn.content or "", envelopes, said,
                                    wrote=wrote_this_turn)
            tracer.egress(
                ok=verdict.ok, stripped_think=verdict.stripped_think,
                prompt_leak=verdict.prompt_leak, unverified=verdict.unverified,
                original=turn.content or "",
            )

            # A block used to END the turn, which was the wrong consequence.
            # Asking "what can I do if someone will not pay me?" is ordinary
            # bookkeeping; the model answered it by naming a tool, printing an
            # example call and inventing $4.20 of interest — so the reply was
            # rightly suppressed, and the administrator got a fixed sentence
            # about instructions and no answer to her question.
            #
            # So: tell the model exactly what was wrong and let it say the same
            # thing properly. NOTHING is loosened — the identical replies are
            # still refused, they simply are not the end of the conversation.
            # Capped at ONE correction, so the model gets a single chance to
            # restate rather than a hill to climb toward wording that slips
            # through. Every attempt stays in the trace.
            if not verdict.ok and not retried:
                retried = True
                # A `system` message, not a `user` one. As a user turn it read
                # like a fresh instruction from the administrator and displaced
                # her actual question — asked "what are your available tools?"
                # the model came back with "I do not have any amounts to share".
                # It is also no longer trusted text for the figure allowlist,
                # which removes a laundering path for free.
                history.append({
                    "role": "system",
                    "content": verdict.correction(user_text),
                })
                tracer.note(
                    f"reply suppressed ({verdict.cause}); asked the model to "
                    "answer again",
                    author="egress",
                )
                yield Retrying(
                    verdict.cause or "blocked",
                    unverified=list(verdict.unverified),
                    disclosed=list(verdict.disclosed),
                    allowed=sorted(egress.allowed_money(envelopes, said)),
                    withheld=turn.content or "",
                )
                continue

            # The turn ends with something she can read. If it is the model's own
            # words (not a suppressed reply replaced by a fixed sentence) then any
            # confirmation summary raised this turn genuinely reached her, and its
            # token becomes spendable in her NEXT message. A suppressed reply
            # carries no summary, so it must not open the gate.
            if verdict.ok:
                for token in offered_tokens:
                    confirm.delivered(token, user_turn)

            tracer.conv_end("reply", {"model_calls": call_index,
                                      "egress_ok": verdict.ok,
                                      "retried": retried})
            # The model occasionally returns nothing at all — no content and no
            # tool call — which reached the browser as a blank bubble that reads
            # as a crash. The same "never empty" contract the tools follow
            # applies to the last thing she actually sees.
            yield Reply(verdict.reply or
                        "I did not manage to put an answer together. Ask me "
                        "again and I will look it up properly.")
            return

        # Answer every tool call the model made, in order.
        for tc in turn.tool_calls:
            yield ToolStep(tc.name, "start")

            if not tc.parse_ok:
                # The model produced un-parseable arguments. Feed a structured
                # error back so it can correct itself, rather than crashing.
                result = {
                    "ok": False,
                    "message": "I could not read the details for that action. "
                               "Please state them again clearly.",
                }
                tracer.tool_result(tc.name, ok=False, reason="unparsable_arguments",
                                   result=result, internal_error=tc.parse_error)
                yield ToolStep(tc.name, "error", result["message"],
                               reason="unparsable_arguments")
            else:
                audit_seq += 1
                tracer.tool_call(tc.name, tc.arguments or {})
                outcome = registry.dispatch(
                    con, tc.name, tc.arguments or {},
                    db.Ctx(conv_id=tracer.conv_id, seq=audit_seq,
                           actor="admin", turn=user_turn),
                )
                result = outcome.for_the_model
                wrote_this_turn = wrote_this_turn or outcome.wrote
                tracer.tool_result(
                    tc.name, ok=bool(result.get("ok")), reason=outcome.reason,
                    result=result, internal_error=outcome.internal_error,
                )
                if isinstance(result.get("token"), str):
                    offered_tokens.append(result["token"])
                status = "ok" if result.get("ok") else "refused"
                yield ToolStep(tc.name, status, str(result.get("message", "")),
                               reason=outcome.reason or "")

            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # Ran out of steps — a runaway tool loop.
    tracer.conv_end("step_cap", {"model_calls": max_steps})
    yield Failure(
        "That took more steps than I allow in one go. Let's break it into smaller "
        "requests."
    )
