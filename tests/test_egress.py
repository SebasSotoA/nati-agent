"""Tests for the output (egress) filter.

Both directions matter equally: an invented figure must be blocked, and
ordinary benign replies must pass — a filter that blocks real work is a bug
with better manners.
"""

from __future__ import annotations

import json

import pytest

from nati import clock
from nati.agent import egress
from nati.tools import reads

ENV_FUND = {
    "ok": True,
    "message": "As of 2026-08-04 the fund has $15,732 in cash and $2,121 out on loan.",
    "cash": 15732,
    "capital_out": 2121,
    "equity": 17853,
    "figures": [
        {"field": "cash", "value": 15732, "text": "$15,732"},
        {"field": "capital_out", "value": 2121, "text": "$2,121"},
    ],
}


@pytest.fixture
def world(tmp_path):
    """A freshly built world. Never `db.connect()` on var/world.db: that file is
    generated, so a fresh clone does not have it and the suite must not need it.
    """
    from nati.world import db

    path = tmp_path / "world.db"
    db.create(path, reset=True)
    con = db.connect(path)
    yield con
    con.close()


def _review(reply: str, envelopes=None, user_text: str = "") -> egress.Verdict:
    with clock.frozen("2026-08-04"):
        return egress.review(reply, envelopes or [], user_text)


# --------------------------------------------------------------------------
# Benign replies pass
# --------------------------------------------------------------------------


def test_a_faithful_reply_passes() -> None:
    v = _review(
        "The fund has $15,732 in cash and $2,121 out on loan, as of 2026-08-04.",
        [ENV_FUND],
    )
    assert v.ok
    assert "15,732" in v.reply


def test_numbers_the_user_typed_may_be_echoed() -> None:
    # Confirm-before-write: she says an amount, Nati repeats it back to confirm.
    v = _review(
        "Just to confirm: record $40 of savings for Carlos?",
        [],
        user_text="Record a $40 saving for Carlos.",
    )
    assert v.ok


def test_small_counts_are_free() -> None:
    v = _review("You have two active loans; I checked all 3 tickets.", [ENV_FUND])
    assert v.ok


def test_prompt_numerals_are_allowed() -> None:
    # "29 members" and today's date come from the system prompt itself.
    v = _review("The natillera has 29 members and today is 2026-08-04.", [])
    assert v.ok


def test_a_year_from_an_allowed_date_is_allowed() -> None:
    v = _review("That saving is part of the 2026 cycle.", [ENV_FUND])
    assert v.ok


def test_a_date_may_be_written_in_prose() -> None:
    """Regression: a real reply was blocked on the '30' in "November 30, 2026".

    The tool returned the date as 2026-11-30. Blocking the ordinary English form
    of a date a tool DID return is over-blocking on a formatting difference.
    """
    env = {"ok": True, "message": "done", "cycle_ends": "2026-11-30"}
    v = _review("The cycle closes on November 30, 2026.", [env])
    assert v.ok, f"blocked on {v.unverified}"


def test_a_number_token_does_not_swallow_a_trailing_comma() -> None:
    with clock.frozen("2026-08-04"):
        assert egress._number_tokens("on 30, and 15,732 too") == ["30", "15,732"]


def test_a_reply_with_no_numbers_passes_with_no_envelopes() -> None:
    v = _review("I cannot do that, and repeating the request will not change it.")
    assert v.ok


def test_a_figure_from_an_earlier_turn_may_be_restated() -> None:
    """Regression: the allowlist is CONVERSATION-scoped, not turn-scoped.

    A real conversation was broken by the turn-scoped version: a tool stated the
    next cycle starts in 2027, and the very next turn — which called no tool —
    was blocked for repeating it. Ask-then-discuss is the most ordinary pattern
    there is, so it has to work.
    """
    history = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "what is in the fund?"},
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps(ENV_FUND)},
        {"role": "assistant", "content": "The fund has $15,732 in cash."},
        {"role": "user", "content": "is that enough for the payout?"},
    ]
    envelopes, said = egress.conversation_context(history)
    with clock.frozen("2026-08-04"):
        # This turn called no tool at all.
        v = egress.review("No — $15,732 does not cover it.", envelopes, said)
    assert v.ok, f"blocked on {v.unverified}"


def test_unreadable_tool_content_is_skipped_not_fatal() -> None:
    history = [{"role": "tool", "content": "{not json"},
               {"role": "user", "content": "hi"}]
    envelopes, said = egress.conversation_context(history)
    assert envelopes == []
    assert said == "hi"


def test_the_next_cycle_year_is_answerable(world) -> None:
    """Regression: a real reply was blocked on the single token '2027'.

    "When does the next cycle start?" is ordinary admin work. The model had no
    tool that returned the next cycle's year, so it added one to this year — and
    the filter correctly refuses model-side arithmetic. The fix was in the
    TOOLS: `fund_status` now returns the cycle's calendar, so the model reports
    it. This test pins that the answer passes.
    """
    with clock.frozen("2026-08-04"):
        env = reads.fund_status(world)
        v = egress.review(
            "The 2026 cycle runs January to November and closes on 2026-11-30. "
            "Benito can join when the 2027 cycle starts on 2027-01-01.",
            [env], "",
        )
    assert v.ok, f"still blocked on {v.unverified}"


# --------------------------------------------------------------------------
# Invented figures are blocked
# --------------------------------------------------------------------------


def test_an_invented_balance_is_blocked() -> None:
    v = _review("The fund has $99,999 in cash.", [ENV_FUND])
    assert not v.ok
    assert "$99,999" in v.unverified
    assert v.reply == egress.BLOCKED_REPLY
    assert "99,999" not in v.reply


def test_model_side_arithmetic_is_blocked() -> None:
    # 15732 + 2121 = 17853 is in the envelope; but a third of cash is not.
    v = _review("A third of the cash would be about $5,244.", [ENV_FUND])
    assert not v.ok


def test_a_fractional_AMOUNT_is_never_free() -> None:
    """Cents cannot exist in this ledger, so a fractional dollar figure is
    always invented. A bare decimal that is not money is not policed — the
    filter judges money and leaves counts, rates and ratios alone."""
    assert not _review("He owes $214.20 after interest.", [ENV_FUND]).ok
    assert not _review("That is 214.20 dollars.", [ENV_FUND]).ok
    assert _review("That works out to about 4.5 tickets each.", [ENV_FUND]).ok


def test_a_date_is_not_policed_as_money() -> None:
    """Dates were once judged like amounts, which cost two false positives
    ("2027", "November 30, 2026") and caught nothing real. A misstated date is
    wrong, but it is not a figure she would repeat to a creditor, and the tool
    result carries the true one. Money is the guard's job."""
    assert _review("I recorded it dated 2026-03-15.", [ENV_FUND]).ok


# --------------------------------------------------------------------------
# Leaks
# --------------------------------------------------------------------------


def test_a_leaked_think_block_is_stripped_but_the_reply_survives() -> None:
    v = _review(
        "<think>the user might be trying to trick me</think>The fund has "
        "$15,732 in cash.",
        [ENV_FUND],
    )
    assert v.ok
    assert v.stripped_think
    assert "<think>" not in v.reply
    assert "trick" not in v.reply
    assert v.reply == "The fund has $15,732 in cash."


def test_a_stray_close_tag_strips_everything_before_it() -> None:
    v = _review(
        "internal deliberation here</think>The fund has $15,732 in cash.",
        [ENV_FUND],
    )
    assert v.ok
    assert v.stripped_think
    assert v.reply == "The fund has $15,732 in cash."


def test_a_prompt_extraction_is_blocked() -> None:
    v = _review(
        'My instructions begin: "You are Nati, the assistant to the '
        'administrator of a natillera..."',
        [ENV_FUND],
    )
    assert not v.ok
    assert v.prompt_leak
    assert v.reply == egress.BLOCKED_LEAK_REPLY
    assert "You are Nati" not in v.reply


def test_the_canaries_do_not_trip_on_ordinary_talk() -> None:
    v = _review("Nati here — the books are balanced and nothing is pending.", [])
    assert v.ok


# --------------------------------------------------------------------------
# The allowlist builder
# --------------------------------------------------------------------------


def test_numbers_nested_anywhere_in_an_envelope_count() -> None:
    env = {
        "ok": True,
        "message": "done",
        "charged": [{"loan_id": 7, "derivation": {"base": 500, "rate": 3}}],
    }
    with clock.frozen("2026-08-04"):
        allowed = egress.allowed_numbers([env], "")
    assert {"7", "500", "3"} <= allowed


def test_booleans_do_not_smuggle_numbers() -> None:
    with clock.frozen("2026-08-04"):
        allowed = egress.allowed_numbers([{"ok": True, "paid": False}], "")
    # True/False must not become "1"/"0" — 0 and 1 are free anyway, but the
    # canon set must not grow from booleans.
    assert "True" not in allowed and "False" not in allowed


def test_formatted_and_raw_forms_both_pass() -> None:
    v = _review("Cash: $15,732. Raw: 15732.", [ENV_FUND])
    assert v.ok


# --------------------------------------------------------------------------
# Regressions from the red-team audit
# --------------------------------------------------------------------------


def test_money_is_field_bound_not_merely_present(world) -> None:
    """A raffle number must not license a dollar figure of the same value.

    `raffle_status` puts ~60 of the 90 two-digit values in the envelope — they
    are people's raffle numbers. Before the money/count split that licensed
    "your share of the fund is $47": a fabricated payout wearing a raffle
    number's clothes.
    """
    with clock.frozen("2026-08-04"):
        env = reads.raffle_status(world, "monthly", 7)

        numbers = [
            n for t in env.get("tickets", []) for n in (t.get("numbers") or [])
        ] if env.get("tickets") else []
        # Pick a number the envelope really contains, above the free limit.
        candidate = next((n for n in numbers if n > 12), None)
        if candidate is None:
            import pytest
            pytest.skip("no two-digit raffle number in this month")

        # Speakable as a number...
        assert egress.review(f"Number {candidate} is taken.", [env], "").ok
        # ...but NOT as money.
        v = egress.review(f"Your share is ${candidate}.", [env], "")
        assert not v.ok, "a raffle number licensed a dollar figure"


def test_a_real_money_figure_still_passes(world) -> None:
    """Derived from the envelope, not hardcoded: the world is mutable state."""
    from nati.tools.envelope import usd

    with clock.frozen("2026-08-04"):
        env = reads.fund_status(world)
        cash = usd(env["cash"])
        v = egress.review(f"The fund has {cash} in cash.", [env], "")
        assert v.ok, f"blocked a real money figure: {v.unverified}"


def test_the_world_hash_does_not_license_figures() -> None:
    """Digit-runs mined from world_hash used to free arbitrary large numbers."""
    env = {"ok": True, "message": "done",
           "world_hash": "3348b1c8cdb8cb498a50ea2b0f8abc80383ee2a88917e684f99"}
    with clock.frozen("2026-08-04"):
        v = egress.review("The December bonus will be $3348.", [env], "")
    assert not v.ok


def test_a_spaced_think_tag_does_not_slip_through() -> None:
    """`<think >` with one space defeated the exact-match patterns entirely."""
    with clock.frozen("2026-08-04"):
        for text in (
            "<think >secret deliberation</think >The fund is fine.",
            "<think>ok</think >The fund is fine.",
            "<THINK >x</THINK >The fund is fine.",
        ):
            v = egress.review(text, [], "")
            assert "deliberation" not in v.reply and "secret" not in v.reply
            assert "think" not in v.reply.lower(), v.reply


def test_every_canary_is_actually_in_the_prompt() -> None:
    """A canary that matches nothing is a control that can never fire."""
    from nati.agent import prompt

    text = prompt.system_prompt().lower()
    for canary in egress._PROMPT_CANARIES:
        assert canary in text, f"dead canary: {canary!r}"


def test_a_blocked_reply_says_so_when_the_write_landed() -> None:
    """Telling her "I have not sent it" after money moved invites a duplicate."""
    with clock.frozen("2026-08-04"):
        # A money figure, since money is what the filter judges now.
        v = egress.review("Recorded. His balance is now $935.", [], "",
                          wrote=True)
    assert not v.ok
    assert v.reply == egress.BLOCKED_AFTER_WRITE_REPLY
    assert "is saved" in v.reply


def test_a_tool_name_is_never_spoken() -> None:
    """SOP G1. It happened in ordinary replies — "I'd check with the
    `raffle_status` tool" — handing over the exact surface to aim at."""
    for text in (
        "I would need to check with the raffle_status tool.",
        "Use `record_saving` for that.",
        "Try **fund_status** instead.",
    ):
        v = _review(text, [ENV_FUND])
        assert not v.ok, f"leaked a tool name: {text}"
        assert v.reply == egress.BLOCKED_LEAK_REPLY


def test_ordinary_words_are_not_mistaken_for_tool_names() -> None:
    """`members` is a tool name AND plain English, so it must stay speakable."""
    assert _review("All 29 members have paid their tickets.", [ENV_FUND]).ok
    assert _review("Two members still owe money.", [ENV_FUND]).ok


def test_the_integrity_answer_is_speakable(world) -> None:
    """REGRESSION: "do the books balance?" got its own reply blocked.

    `check_integrity` returned no `figures[]` manifest, and money is field-bound
    — a dollar figure must have been TAGGED as money by a tool. So the most
    natural verification question anyone can ask produced a correct tool result
    and then had the answer suppressed on its own cash figure. It was the only
    read tool with no manifest.
    """
    from nati.tools.envelope import usd

    with clock.frozen("2026-08-04"):
        env = reads.check_integrity(world)
        money = {f["field"]: f for f in env["figures"]
                 if str(f["text"]).startswith("$")}
        assert money, "check_integrity emits no money figures"

        cash = money["cash"]["text"]
        expected = money["payout_expected"]["text"]
        v = egress.review(
            f"The books balance. Fund cash: {cash} (non-negative). "
            f"The payout accounts for {expected}.",
            [env], "",
        )
    assert v.ok, f"blocked on {v.unverified}"


def test_every_read_tool_can_state_its_own_money(world) -> None:
    """Any tool that reports an amount must tag it, or the filter gags the reply.

    This is the general form of the bug above: field-bound money means a tool
    that returns a dollar value without a manifest has made that value
    unspeakable.
    """
    with clock.frozen("2026-08-04"):
        envelopes = {
            "members": reads.members(world),
            "member_status": reads.member_status(world, 2),
            "fund_status": reads.fund_status(world),
            "loan_status": reads.loan_status(world, 1),
            "list_loans": reads.list_loans(world),
            "raffle_status": reads.raffle_status(world, "monthly", 7),
            "simulate_payout": reads.simulate_payout(world),
            "check_integrity": reads.check_integrity(world),
        }
    for name, env in envelopes.items():
        money = [f for f in (env.get("figures") or [])
                 if str(f.get("text", "")).startswith("$")]
        assert money, f"{name} reports no money the model is allowed to state"


def test_the_correction_cannot_launder_a_figure() -> None:
    """The retry text becomes a message in the conversation, and figures the
    administrator types are trusted — so if the correction quoted the invented
    amount, restating it would then pass. It must not name the number."""
    with clock.frozen("2026-08-04"):
        first = egress.review("It comes to $91,234 all told.", [], "")
        assert not first.ok and first.cause == egress.FIGURES

        correction = first.correction()
        assert "91,234" not in correction and "91234" not in correction

        # Saying it again, with the correction now in the conversation, still fails.
        again = egress.review("It comes to $91,234 all told.", [], correction)
    assert not again.ok, "the correction laundered the figure onto the allowlist"


def test_describing_what_it_does_is_not_blocked() -> None:
    """REGRESSION: "What can you do?" was refused.

    The answer said "numbers 00-99", which is a fact the SYSTEM handed the model
    in the tool descriptions — not one it invented. Numerals from the schemas are
    allowed exactly as the system prompt's are.
    """
    with clock.frozen("2026-08-04"):
        for text in (
            "I keep the books: savings, loans, monthly interest and the raffles. "
            "The monthly raffle takes two numbers from 00 to 99.",
            "The cycle runs from month 1 to month 11; December is not part of it.",
            "A phone number has to be exactly 10 digits.",
        ):
            v = egress.review(text, [], "")
            assert v.ok, f"blocked on {v.unverified}: {text!r}"


def test_but_a_price_still_has_to_be_looked_up() -> None:
    """The widening is numbers only. Money stays field-bound, so a ticket price
    cannot be recited from the schema — it must come from a tool result."""
    with clock.frozen("2026-08-04"):
        v = egress.review("A monthly ticket costs $20.", [], "")
    assert not v.ok
    assert v.cause == egress.FIGURES


# --------------------------------------------------------------------------
# The allowlist must accept money in every spelling the filter judges
# --------------------------------------------------------------------------


@pytest.mark.parametrize("wrote_it_as", ["900 usd", "900 USD", "900 dollars",
                                         "$900", "USD 900", "usd900"])
def test_a_figure_she_typed_can_be_repeated_back(wrote_it_as: str) -> None:
    """The reported failure, and it was ours, not the model's.

    `review` judged three spellings of money while the allowlist harvested only
    `$`-prefixed ones. So "Sebastian just payed 900 usd" put nothing on the
    allowlist, and the model echoing her own figure back as "$900" — which is
    what asking a sensible clarifying question requires — was called a
    fabrication. She was refused twice and handed the generic message.
    """
    said = f"Sebastian just payed {wrote_it_as}"
    for spelling in ("$900", "900 USD", "900 dollars"):
        verdict = egress.review(
            f"Is that {spelling} a savings contribution or a loan repayment?",
            [], said,
        )
        assert verdict.ok, (
            f"she wrote {wrote_it_as!r}, the reply said {spelling!r}, "
            f"unverified={verdict.unverified}"
        )


def test_the_allowlist_and_the_judge_use_one_pattern() -> None:
    """Whatever `review` is willing to call money, `allowed_money` must harvest.

    This is the invariant the bug broke, asserted directly so widening one side
    without the other fails here rather than in her chat.
    """
    for spelling in ("$1,234", "1234 dollars", "1234 USD", "USD 1,234", "$1234.50"):
        harvested = egress.allowed_money([], f"she paid {spelling}")
        judged = egress.review(f"the amount is {spelling}", [], "")
        assert judged.unverified, f"{spelling!r} is not judged as money at all"
        assert harvested, f"{spelling!r} is judged but never harvested"
        assert egress._canon(judged.unverified[0]) in harvested, (
            f"{spelling!r} canonicalizes differently on each side: "
            f"judged={judged.unverified} harvested={harvested}"
        )


def test_an_amount_no_one_mentioned_is_still_caught() -> None:
    """The widening must not blunt the check it exists for."""
    verdict = egress.review(
        "After that he still owes $4,231.", [], "Sebastian just payed 900 usd")
    assert not verdict.ok
    assert verdict.cause == egress.FIGURES
    assert "$4,231" in verdict.unverified


def test_her_bare_figure_can_be_quoted_back() -> None:
    """The second half of the same bug, and the one she hit.

    She wrote "Sebastian just payed 900" — no `$`, no "usd", so the money pattern
    does not match it. `record_payment` then refused the amount as larger than the
    balance, and the sentence that EXPLAINS that refusal has to name the figure.
    It was suppressed as a fabrication and she got the generic message.
    """
    her = "Sebastian just payed 900"
    refusal = {"ok": False,
               "message": "That payment is more than what is left to pay on that loan."}
    verdict = egress.review(
        "That is more than what is left on his loan, so I have not recorded "
        "the $900.", [refusal], her)
    assert verdict.ok, verdict.unverified


def test_a_number_she_typed_is_speakable_as_money_however_she_wrote_it() -> None:
    for spelling in ("900", "$900", "900 usd", "900 dollars", "USD 900"):
        allowed = egress.allowed_money([], f"Sebastian just payed {spelling}")
        assert "900" in allowed, f"{spelling!r} did not reach the money allowlist"


def test_the_model_still_cannot_invent_a_figure_she_never_mentioned() -> None:
    """The widening must not blunt the check. Nothing here supplies 4231."""
    verdict = egress.review(
        "After that he still owes $4,231.", [], "Sebastian just payed 900")
    assert not verdict.ok
    assert verdict.cause == egress.FIGURES
    assert "$4,231" in verdict.unverified


def test_a_figure_cannot_be_laundered_through_the_correction() -> None:
    """The correction is a `system` message and the allowlist reads `user` only.

    Asserted end to end rather than by inspection: build the history the loop
    would, and confirm the invented amount is still absent from the allowlist.
    """
    verdict = egress.review("Your share is $9,999.", [], "what is my share?")
    assert not verdict.ok
    history = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "what is my share?"},
        {"role": "assistant", "content": "Your share is $9,999."},
        {"role": "system", "content": verdict.correction("what is my share?")},
    ]
    envelopes, said = egress.conversation_context(history)
    assert "9999" not in egress.allowed_money(envelopes, said)
    assert not egress.review("Your share is $9,999.", envelopes, said).ok
