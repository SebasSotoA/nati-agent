"""Tests for the 7 rare write tools and the two-phase confirmation.

The confirmation mechanism is the headline: a prompt-level "confirm before you
act" is a politeness the model can drop, so these tools make it a token that is
unforgeable, single-use, and bound to the exact arguments confirmed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nati import clock
from nati.tools import confirm, registry
from nati.world import db

CYCLE_DAY = "2026-08-04"


@pytest.fixture(autouse=True)
def frozen_clock():
    with clock.frozen(CYCLE_DAY):
        yield


@pytest.fixture(autouse=True)
def fresh_confirmations():
    confirm.reset()
    yield
    confirm.reset()


@pytest.fixture
def con(tmp_path: Path):
    path = tmp_path / "world.db"
    db.create(path, reset=True)
    connection = db.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def ctx() -> db.Ctx:
    return db.Ctx(conv_id="test-rare", seq=1)


def call(con, ctx, tool_name: str, /, **args):
    return registry.dispatch(con, tool_name, args, ctx)


def confirmed(con, ctx, tool_name: str, /, **args):
    """Drive the two-phase flow the way the model would: ask, then confirm."""
    first = call(con, ctx, tool_name, **args)
    assert first.for_the_model.get("confirmation_required"), first.for_the_model
    token = first.for_the_model["token"]
    return call(con, ctx, tool_name, **args, confirm_token=token)


def member(con, member_id: int) -> str:
    return con.execute(
        "SELECT name FROM members WHERE id = ?", (member_id,)
    ).fetchone()["name"]


# --------------------------------------------------------------------------
# The confirmation mechanism itself
# --------------------------------------------------------------------------


def test_the_first_call_writes_nothing(con, ctx) -> None:
    before = db.world_hash(con)
    result = call(con, ctx, "set_setting", key="monthly_prize", value=70)
    env = result.for_the_model
    assert env["ok"] is False
    assert env["confirmation_required"] is True
    assert env["token"]
    assert "summary" in env
    assert db.world_hash(con) == before


def test_the_token_completes_the_write(con, ctx) -> None:
    result = confirmed(con, ctx, "set_setting", key="monthly_prize", value=70)
    assert result.for_the_model["ok"] is True
    row = con.execute(
        """SELECT value FROM settings WHERE key = 'monthly_prize'
            ORDER BY effective_from DESC LIMIT 1"""
    ).fetchone()
    assert row["value"] == "70"


def test_a_token_is_single_use(con, ctx) -> None:
    first = call(con, ctx, "set_setting", key="monthly_prize", value=70)
    token = first.for_the_model["token"]
    ok1 = call(con, ctx, "set_setting", key="monthly_prize", value=70,
               confirm_token=token)
    assert ok1.for_the_model["ok"] is True
    # Same token again: no write, a fresh confirmation request instead. (The
    # write itself would also fail on the same-day UNIQUE, but it must not even
    # get that far.)
    again = call(con, ctx, "set_setting", key="monthly_prize", value=70,
                 confirm_token=token)
    assert again.for_the_model.get("confirmation_required") is True


def test_a_token_is_bound_to_the_exact_arguments(con, ctx) -> None:
    """Confirm $70, then try to slip $700 through with the same token."""
    first = call(con, ctx, "set_setting", key="monthly_prize", value=70)
    token = first.for_the_model["token"]
    tampered = call(con, ctx, "set_setting", key="monthly_prize", value=700,
                    confirm_token=token)
    env = tampered.for_the_model
    assert env["ok"] is False
    assert env.get("confirmation_required") is True  # a fresh request, no write
    assert con.execute(
        "SELECT COUNT(*) FROM settings WHERE key='monthly_prize' AND value='700'"
    ).fetchone()[0] == 0


def test_a_token_is_bound_to_the_conversation(con, ctx) -> None:
    first = call(con, ctx, "set_setting", key="monthly_prize", value=70)
    token = first.for_the_model["token"]
    other = db.Ctx(conv_id="another-conversation", seq=1)
    replayed = call(con, other, "set_setting", key="monthly_prize", value=70,
                    confirm_token=token)
    assert replayed.for_the_model.get("confirmation_required") is True


def test_a_forged_token_does_not_write(con, ctx) -> None:
    forged = call(con, ctx, "set_setting", key="monthly_prize", value=70,
                  confirm_token="0123456789abcdef")
    assert forged.for_the_model.get("confirmation_required") is True


# --------------------------------------------------------------------------
# add_member
# --------------------------------------------------------------------------


def test_nobody_joins_mid_cycle(con, ctx) -> None:
    # The seeded world has months of activity, so this always refuses.
    result = call(con, ctx, "add_member", name="Nueva Persona",
                  phone="3129999999")
    assert result.for_the_model["ok"] is False
    assert result.reason == "cycle_started"


def test_add_member_works_on_a_quiet_world(con, ctx) -> None:
    # Empty the cycle's activity to simulate a world before the cycle starts.
    con.execute("PRAGMA foreign_keys = OFF")
    for table in ("draws", "raffle_tickets", "loan_interest", "loan_payments",
                  "loans", "savings"):
        con.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
    con.execute("DROP TRIGGER IF EXISTS payments_no_delete")
    con.execute("DROP TRIGGER IF EXISTS interest_no_delete")
    con.execute("DROP TRIGGER IF EXISTS tickets_no_delete")
    for table in ("draws", "raffle_tickets", "loan_interest", "loan_payments",
                  "loans", "savings"):
        con.execute(f"DELETE FROM {table}")  # noqa: S608
    result = confirmed(con, ctx, "add_member", name="Nueva Persona",
                       phone="3129999999")
    assert result.for_the_model["ok"] is True
    assert member(con, result.for_the_model["member_id"]) == "Nueva Persona"


# --------------------------------------------------------------------------
# record_loan
# --------------------------------------------------------------------------


def test_a_fund_loan_is_recorded(con, ctx) -> None:
    result = confirmed(con, ctx, "record_loan", member_id=2,
                       confirm_name=member(con, 2), amount=200,
                       monthly_rate=3, money_from="fund")
    env = result.for_the_model
    assert env["ok"] is True
    loan = con.execute(
        "SELECT * FROM loans WHERE id = ?", (env["loan_id"],)
    ).fetchone()
    assert loan["lender_id"] == 0
    assert loan["principal"] == 200
    assert loan["status"] == "active"


def test_a_personal_loan_carries_the_warning(con, ctx) -> None:
    # Personal money is always the administrator's — no lender needs naming.
    result = confirmed(con, ctx, "record_loan", member_id=2,
                       confirm_name=member(con, 2), amount=200,
                       monthly_rate=3, money_from="personal")
    env = result.for_the_model
    assert env["ok"] is True
    loan = con.execute(
        "SELECT lender_id FROM loans WHERE id = ?", (env["loan_id"],)
    ).fetchone()
    assert loan["lender_id"] != 0
    assert any("own money" in w for w in env.get("warnings", []))


def test_a_personal_loan_is_always_the_administrators(con, ctx) -> None:
    """Only the administrator lends privately, so 'personal' takes no lender
    argument and can never be attributed to the wrong pocket — it is always hers.

    This replaces the old ambiguity handling: there used to be several private
    lenders, so the tool refused ('ambiguous_lender') unless told whose money it
    was. With one private pocket, that whole question is gone.
    """
    done = confirmed(con, ctx, "record_loan", member_id=2,
                     confirm_name=member(con, 2), amount=200,
                     monthly_rate=3, money_from="personal")
    assert done.for_the_model["ok"] is True

    # The loan lands against the administrator's lender row.
    admin_member_id = int(con.execute(
        "SELECT value FROM settings WHERE key = 'admin_member_id'"
    ).fetchone()[0])
    row = con.execute(
        """SELECT le.member_id FROM loans l JOIN lenders le ON le.id = l.lender_id
            WHERE l.id = ?""", (done.for_the_model["loan_id"],)
    ).fetchone()
    assert int(row["member_id"]) == admin_member_id

    # And there is exactly one personal lender to attribute to.
    assert con.execute(
        "SELECT COUNT(*) FROM lenders WHERE kind = 'personal'"
    ).fetchone()[0] == 1


def test_the_fund_cannot_lend_more_cash_than_it_has(con, ctx) -> None:
    before = db.world_hash(con)
    result = confirmed(con, ctx, "record_loan", member_id=2,
                       confirm_name=member(con, 2), amount=999_000,
                       monthly_rate=3, money_from="fund")
    assert result.for_the_model["ok"] is False
    assert db.world_hash(con) == before


def test_a_voided_loan_cannot_be_resourced(con, ctx) -> None:
    """Void as fund, recreate as personal: the interest-diversion attack."""
    created = confirmed(con, ctx, "record_loan", member_id=2,
                        confirm_name=member(con, 2), amount=200,
                        monthly_rate=3, money_from="fund")
    loan_id = created.for_the_model["loan_id"]
    voided = confirmed(con, ctx, "void_loan", loan_id=loan_id)
    assert voided.for_the_model["ok"] is True

    recreated = call(con, ctx, "record_loan", member_id=2,
                     confirm_name=member(con, 2), amount=200,
                     monthly_rate=3, money_from="personal")
    assert recreated.for_the_model["ok"] is False
    assert recreated.reason == "resourced_loan"

    # Same source is a legitimate re-entry, and stays possible.
    again = confirmed(con, ctx, "record_loan", member_id=2,
                      confirm_name=member(con, 2), amount=200,
                      monthly_rate=3, money_from="fund")
    assert again.for_the_model["ok"] is True


# --------------------------------------------------------------------------
# void_loan
# --------------------------------------------------------------------------


def test_a_loan_with_history_cannot_be_voided(con, ctx) -> None:
    loan = con.execute(
        """SELECT l.id FROM loans l JOIN loan_payments p ON p.loan_id = l.id
            WHERE l.status = 'active' LIMIT 1"""
    ).fetchone()
    result = call(con, ctx, "void_loan", loan_id=int(loan["id"]))
    assert result.for_the_model["ok"] is False
    assert result.reason == "loan_has_history"


def test_an_old_loan_cannot_be_voided(con, ctx) -> None:
    loan = con.execute(
        """SELECT l.id FROM loans l
            WHERE l.status = 'active'
              AND substr(l.started_on, 1, 7) != '2026-08'
              AND NOT EXISTS (SELECT 1 FROM loan_payments p WHERE p.loan_id = l.id)
              AND NOT EXISTS (SELECT 1 FROM loan_interest i WHERE i.loan_id = l.id)
            LIMIT 1"""
    ).fetchone()
    if loan is None:
        pytest.skip("the seed has no clean old loan to try")
    result = call(con, ctx, "void_loan", loan_id=int(loan["id"]))
    assert result.for_the_model["ok"] is False
    assert result.reason == "correction_window_closed"


# --------------------------------------------------------------------------
# record_reversal
# --------------------------------------------------------------------------


def _record_saving(con, ctx, amount: int) -> int:
    result = call(con, ctx, "record_saving", member_id=2,
                  confirm_name=member(con, 2), amount=amount)
    assert result.for_the_model["ok"] is True
    return result.for_the_model["saving_id"]


def _total_saved(con, member_id: int) -> int:
    return int(con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM savings WHERE member_id = ?",
        (member_id,),
    ).fetchone()[0])


def test_a_reversal_nets_out_and_both_rows_stay(con, ctx) -> None:
    before = _total_saved(con, 2)
    saving_id = _record_saving(con, ctx, 500)
    assert _total_saved(con, 2) == before + 500

    result = confirmed(con, ctx, "record_reversal", saving_id=saving_id)
    assert result.for_the_model["ok"] is True
    assert _total_saved(con, 2) == before

    rows = con.execute(
        "SELECT * FROM savings WHERE id = ? OR reversal_of = ? ORDER BY id",
        (saving_id, saving_id),
    ).fetchall()
    assert len(rows) == 2  # nothing erased
    assert rows[0]["amount"] == 500 and rows[1]["amount"] == -500


def test_a_saving_reverses_only_once(con, ctx) -> None:
    saving_id = _record_saving(con, ctx, 500)
    first = confirmed(con, ctx, "record_reversal", saving_id=saving_id)
    assert first.for_the_model["ok"] is True
    second = call(con, ctx, "record_reversal", saving_id=saving_id)
    assert second.for_the_model["ok"] is False
    assert second.reason == "already_reversed"


def test_a_reversal_is_not_reversed(con, ctx) -> None:
    saving_id = _record_saving(con, ctx, 500)
    first = confirmed(con, ctx, "record_reversal", saving_id=saving_id)
    reversal_id = first.for_the_model["reversal_id"]
    result = call(con, ctx, "record_reversal", saving_id=reversal_id)
    assert result.for_the_model["ok"] is False
    assert result.reason == "reversal_of_reversal"


def test_the_schema_rejects_an_incoherent_reversal_row(con, ctx) -> None:
    """Belt and braces: even a buggy tool could not write a partial reversal."""
    saving_id = _record_saving(con, ctx, 500)
    import sqlite3 as s
    with pytest.raises(s.IntegrityError):
        con.execute(
            "INSERT INTO savings (member_id, date, amount, reversal_of) "
            "VALUES (2, '2026-08-04', -400, ?)",  # wrong magnitude
            (saving_id,),
        )
    with pytest.raises(s.IntegrityError):
        con.execute(
            "INSERT INTO savings (member_id, date, amount) VALUES (2, '2026-08-04', -50)"
        )  # negative with no reversal_of


# --------------------------------------------------------------------------
# record_draw
# --------------------------------------------------------------------------


def _july_ticket_number(con) -> int:
    row = con.execute(
        """SELECT number_1 FROM raffle_tickets
            WHERE kind = 'monthly' AND month = 7 LIMIT 1"""
    ).fetchone()
    assert row is not None, "the seed should have July tickets"
    return int(row["number_1"])


def _free_number(con, month: int) -> int:
    taken = set()
    for r in con.execute(
        """SELECT number_1, number_2 FROM raffle_tickets
            WHERE kind = 'monthly' AND month = ?""", (month,),
    ):
        taken.add(r["number_1"])
        if r["number_2"] is not None:
            taken.add(r["number_2"])
    return next(n for n in range(100) if n not in taken)


def test_a_draw_with_a_winner_derives_the_prize(con, ctx) -> None:
    number = _july_ticket_number(con)
    result = confirmed(con, ctx, "record_draw", kind="monthly", month=7,
                       winning_number=number)
    env = result.for_the_model
    assert env["ok"] is True
    assert env["winner"] is not None
    draw = con.execute(
        "SELECT * FROM draws WHERE kind='monthly' AND month=7"
    ).fetchone()
    assert draw["winning_number"] == number
    assert draw["prize"] > 0  # the configured prize, not model-supplied


def test_a_draw_nobody_wins_pays_nothing(con, ctx) -> None:
    number = _free_number(con, 7)
    result = confirmed(con, ctx, "record_draw", kind="monthly", month=7,
                       winning_number=number)
    env = result.for_the_model
    assert env["ok"] is True
    assert env["winner"] is None
    draw = con.execute(
        "SELECT prize FROM draws WHERE kind='monthly' AND month=7"
    ).fetchone()
    assert draw["prize"] == 0


def test_a_month_is_drawn_once(con, ctx) -> None:
    number = _free_number(con, 7)
    first = confirmed(con, ctx, "record_draw", kind="monthly", month=7,
                      winning_number=number)
    assert first.for_the_model["ok"] is True
    second = call(con, ctx, "record_draw", kind="monthly", month=7,
                  winning_number=number)
    assert second.for_the_model["ok"] is False
    assert second.reason == "draw_exists"


def test_a_future_month_cannot_be_drawn(con, ctx) -> None:
    result = call(con, ctx, "record_draw", kind="monthly", month=9,
                  winning_number=10)
    assert result.for_the_model["ok"] is False
    assert result.reason == "future_month"


# --------------------------------------------------------------------------
# set_setting
# --------------------------------------------------------------------------


def test_the_past_keeps_its_values(con, ctx) -> None:
    from nati.world import queries as q
    old_price = q.ticket_price(con, "monthly", "2026-03-01")
    result = confirmed(con, ctx, "set_setting", key="monthly_ticket_price",
                       value=old_price + 5)
    assert result.for_the_model["ok"] is True
    assert q.ticket_price(con, "monthly", "2026-03-01") == old_price
    assert q.ticket_price(con, "monthly", CYCLE_DAY) == old_price + 5


def test_an_implausible_setting_is_refused(con, ctx) -> None:
    result = call(con, ctx, "set_setting", key="monthly_prize", value=999_999)
    assert result.for_the_model["ok"] is False
    assert result.reason == "implausible_setting"


def test_only_raffle_settings_are_reachable(con, ctx) -> None:
    result = call(con, ctx, "set_setting", key="admin_member_id", value=2)
    assert result.for_the_model["ok"] is False
    assert result.reason == "invalid_choice"


# --------------------------------------------------------------------------
# mark_ticket_paid
# --------------------------------------------------------------------------


def _unpaid_ticket(con):
    return con.execute(
        """SELECT t.*, m.name FROM raffle_tickets t
             JOIN members m ON m.id = t.member_id
            WHERE t.paid = 0 LIMIT 1"""
    ).fetchone()


def test_an_unpaid_ticket_is_settled_at_its_frozen_price(con, ctx) -> None:
    ticket = _unpaid_ticket(con)
    result = call(con, ctx, "mark_ticket_paid",
                  member_id=int(ticket["member_id"]),
                  confirm_name=ticket["name"],
                  kind=ticket["kind"], month=int(ticket["month"]))
    env = result.for_the_model
    assert env["ok"] is True
    after = con.execute(
        "SELECT paid, paid_on, price FROM raffle_tickets WHERE id = ?",
        (int(ticket["id"]),),
    ).fetchone()
    assert after["paid"] == 1
    assert after["paid_on"] == CYCLE_DAY
    assert after["price"] == ticket["price"]  # untouched


def test_a_paid_ticket_is_not_paid_twice(con, ctx) -> None:
    ticket = _unpaid_ticket(con)
    args = dict(member_id=int(ticket["member_id"]), confirm_name=ticket["name"],
                kind=ticket["kind"], month=int(ticket["month"]))
    assert call(con, ctx, "mark_ticket_paid", **args).for_the_model["ok"] is True
    again = call(con, ctx, "mark_ticket_paid", **args)
    assert again.for_the_model["ok"] is False
    assert again.reason == "already_paid"


def test_the_wrong_name_settles_nothing(con, ctx) -> None:
    ticket = _unpaid_ticket(con)
    result = call(con, ctx, "mark_ticket_paid",
                  member_id=int(ticket["member_id"]),
                  confirm_name="Somebody Else",
                  kind=ticket["kind"], month=int(ticket["month"]))
    assert result.for_the_model["ok"] is False
    assert result.reason == "name_mismatch"


# --------------------------------------------------------------------------
# Regressions from the red-team audit
# --------------------------------------------------------------------------


def _loan_args(con) -> dict:
    return dict(member_id=2, confirm_name=member(con, 2), amount=777,
                monthly_rate=3, money_from="fund")


def _offer(con, args: dict, turn: int) -> str:
    """Raise the confirmation and mark it as having reached her, as the loop does."""
    ctx = db.Ctx(conv_id="gate", seq=1, turn=turn)
    token = registry.dispatch(con, "record_loan", args, ctx).for_the_model["token"]
    confirm.delivered(token, turn)
    return token


def _spend(con, args: dict, token: str, turn: int):
    return registry.dispatch(
        con, "record_loan", {**args, "confirm_token": token},
        db.Ctx(conv_id="gate", seq=1, turn=turn))


def test_a_token_cannot_be_minted_and_spent_in_one_turn(con, ctx) -> None:
    """The human gate. Without it the mechanism only proved the model repeated
    itself — the loop allows several model calls per user message, so it could
    mint a token and spend it before she ever saw the summary."""
    args = _loan_args(con)
    token = _offer(con, args, turn=5)
    assert _spend(con, args, token, turn=5).for_the_model["ok"] is False
    assert _spend(con, args, token, turn=6).for_the_model["ok"] is True


def test_an_offer_she_never_answered_cannot_be_spent_in_a_later_turn(con) -> None:
    """The gate used to key on the EARLIEST turn a confirmation was minted in and
    to accept any later turn. Together those let the model propose something in
    turn 2, get no confirmation at all, and then mint-and-spend inside turn 5:
    the check compared 5 > 2 instead of 5 > 5. Every rare write was affected."""
    args = _loan_args(con)
    _offer(con, args, turn=2)          # she is shown it and says nothing

    # Turn 5: ask again and spend it immediately, without her ever confirming.
    ctx5 = db.Ctx(conv_id="gate", seq=1, turn=5)
    reminted = registry.dispatch(
        con, "record_loan", args, ctx5).for_the_model["token"]
    assert _spend(con, args, reminted, turn=5).for_the_model["ok"] is False

    # Only once that fresh offer has actually reached her does the next turn work.
    confirm.delivered(reminted, 5)
    assert _spend(con, args, reminted, turn=6).for_the_model["ok"] is True


def test_a_token_never_shown_to_her_is_not_spendable(con) -> None:
    """Minting is not offering. A confirmation the loop never delivered — the turn
    failed, or the reply carrying the summary was suppressed — opens nothing."""
    args = _loan_args(con)
    ctx = db.Ctx(conv_id="gate", seq=1, turn=2)
    token = registry.dispatch(con, "record_loan", args, ctx).for_the_model["token"]
    for turn in (3, 4, 20):
        assert _spend(con, args, token, turn=turn).for_the_model["ok"] is False


def test_a_failed_attempt_in_the_same_turn_does_not_burn_her_yes(con) -> None:
    """She said yes and was asked again. If the model made any failed attempt in
    the turn she confirmed — calling without the token first, which it does — the
    re-mint used to move the gate to the current turn and her genuine
    confirmation was then refused as same-turn."""
    args = _loan_args(con)
    token = _offer(con, args, turn=2)

    # Turn 3 is her "yes". The model fumbles once before presenting the token.
    registry.dispatch(con, "record_loan", args,
                      db.Ctx(conv_id="gate", seq=1, turn=3))
    assert _spend(con, args, token, turn=3).for_the_model["ok"] is True


def test_resourcing_is_refused_even_with_a_different_amount_or_date(con, ctx) -> None:
    """The guard matched exact details, so $1 more or one day later walked
    through it — and that is the manoeuvre that diverts the group's interest
    into one person's pocket."""
    base = dict(member_id=2, confirm_name=member(con, 2), amount=1000,
                monthly_rate=5, money_from="fund", date="2026-08-02")
    created = confirmed(con, ctx, "record_loan", **base)
    confirmed(con, ctx, "void_loan",
              loan_id=created.for_the_model["loan_id"])

    for tweak in ({}, {"amount": 1001}, {"date": "2026-08-03"},
                  {"monthly_rate": 9}):
        attempt = call(con, ctx, "record_loan",
                       **{**base, "money_from": "personal", **tweak})
        assert attempt.for_the_model["ok"] is False, f"resourced via {tweak}"
        assert attempt.reason == "resourced_loan"

    # A re-entry from the SAME source is a legitimate correction and still works.
    again = confirmed(con, ctx, "record_loan", **base)
    assert again.for_the_model["ok"] is True


def test_every_refusal_leaves_evidence(con, ctx) -> None:
    """SOP F6. Refusals returned as envelopes wrote no audit row at all, so an
    attacker could probe the whole boundary and leave nothing in the database."""
    def rows() -> int:
        return con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    probes = [
        ("record_payment", dict(loan_id=999, amount=50)),
        ("add_member", dict(name="Nobody Here", phone="3001234567")),
        ("set_setting", dict(key="monthly_prize", value=999_999)),
        # A confirmation request is an attempt too.
        ("record_loan", dict(member_id=2, confirm_name=member(con, 2),
                             amount=100, monthly_rate=3, money_from="fund")),
    ]
    for name, args in probes:
        before = rows()
        result = call(con, ctx, name, **args)
        assert result.for_the_model["ok"] is False
        assert rows() > before, f"{name} left no evidence"


def test_paid_needs_a_clear_yes_or_no(con, ctx) -> None:
    """`paid="n"` / "null" / "off" / "" all used to mark a ticket PAID, which is
    irreversible: it erases a member's debt and inflates the fund's income."""
    for bad in ("null", "off", "", "maybe", 2, None):
        result = call(con, ctx, "sell_raffle_ticket", member_id=2,
                      confirm_name=member(con, 2), kind="quarterly",
                      month=9, number_1=7, paid=bad)
        assert result.for_the_model["ok"] is False, f"{bad!r} was accepted"
        assert result.reason == "invalid_boolean"

    # The words we DO recognise still work, in both directions.
    yes = call(con, ctx, "sell_raffle_ticket", member_id=2,
               confirm_name=member(con, 2), kind="quarterly",
               month=9, number_1=7, paid="yes")
    assert yes.for_the_model["ok"] is True
    assert yes.for_the_model["paid"] is True

    no = call(con, ctx, "sell_raffle_ticket", member_id=3,
              confirm_name=member(con, 3), kind="quarterly",
              month=9, number_1=8, paid="n")
    assert no.for_the_model["ok"] is True
    assert no.for_the_model["paid"] is False


def test_a_shared_given_name_does_not_confirm_identity(con, ctx) -> None:
    """"Amparo" matches both Amparo Henao and Consuelo Amparo Jaramillo, so it
    confirmed nothing — whichever id was typed simply won."""
    shared = con.execute(
        """SELECT id, name FROM members
            WHERE name LIKE '%Amparo%' ORDER BY id"""
    ).fetchall()
    if len(shared) < 2:
        pytest.skip("this world has no shared given name to test")

    result = call(con, ctx, "record_saving", member_id=int(shared[1]["id"]),
                  confirm_name="Amparo", amount=200)
    assert result.for_the_model["ok"] is False
    assert result.reason == "ambiguous_name"

    # The full name is unambiguous and still works.
    full = call(con, ctx, "record_saving", member_id=int(shared[1]["id"]),
                confirm_name=shared[1]["name"], amount=200)
    assert full.for_the_model["ok"] is True


def test_a_setting_can_be_corrected_the_same_day(con, ctx) -> None:
    """A price changed by mistake used to stand for a full 24 hours.

    `UNIQUE (key, effective_from)` meant one change per key per day, so putting
    a wrong price back failed on the constraint. A money parameter an attacker
    can change and the administrator cannot revert is worse than the ambiguity
    the constraint avoided.
    """
    from nati.world import queries as q

    def change(value: int, turn: int):
        first = db.Ctx(conv_id="same-day", seq=1, turn=turn)
        second = db.Ctx(conv_id="same-day", seq=1, turn=turn + 1)
        args = {"key": "monthly_ticket_price", "value": value}
        token = registry.dispatch(
            con, "set_setting", args, first).for_the_model["token"]
        confirm.delivered(token, turn)      # as the loop does when the turn ends
        return registry.dispatch(
            con, "set_setting", {**args, "confirm_token": token}, second)

    original = q.ticket_price(con, "monthly", CYCLE_DAY)

    assert change(50, 1).for_the_model["ok"] is True
    assert q.ticket_price(con, "monthly", CYCLE_DAY) == 50

    reverted = change(original, 3)
    assert reverted.for_the_model["ok"] is True, reverted.for_the_model["message"]
    assert q.ticket_price(con, "monthly", CYCLE_DAY) == original

    # Nothing was overwritten: every version is still on the record (F4).
    rows = con.execute(
        """SELECT value FROM settings WHERE key = 'monthly_ticket_price'
            ORDER BY id"""
    ).fetchall()
    assert [r["value"] for r in rows] == [str(original), "50", str(original)]

    # And an earlier month keeps the price that applied to it.
    assert q.ticket_price(con, "monthly", "2026-01-15") == original


def test_resourcing_is_refused_across_month_boundaries(con, ctx) -> None:
    """REGRESSION (critical): the guard keyed on the REPLACEMENT's month.

    Every detail of the replacement is chosen by whoever is asking, so no detail
    could be part of the test. Moving the new loan's date four days into the
    previous month re-sourced $1,000 of the group's money into one person's
    pocket — and `check_integrity` still reported "everything balances".
    """
    base = dict(member_id=29, confirm_name=member(con, 29), amount=1000,
                monthly_rate=5, money_from="fund", date="2026-08-01")
    made = confirmed(con, ctx, "record_loan", **base)
    confirmed(con, ctx, "void_loan", loan_id=made.for_the_model["loan_id"])

    for when in ("2026-08-01", "2026-07-28", "2026-06-15", "2026-01-20"):
        attempt = call(con, ctx, "record_loan",
                       **{**base, "money_from": "personal", "date": when})
        assert attempt.for_the_model["ok"] is False, f"re-sourced via {when}"
        assert attempt.reason == "resourced_loan"

    # Re-entering it from the SAME source is a real correction and still works.
    again = confirmed(con, ctx, "record_loan", **base)
    assert again.for_the_model["ok"] is True


def test_a_prize_cannot_exceed_what_its_month_collected(con, ctx) -> None:
    """REGRESSION (critical): this could switch off the books permanently.

    The prize was valued at today's setting and checked only against total cash,
    so raising it above the year's profits and recording one draw drove
    distributable profits negative. Negative profits make the payout arithmetic
    raise, which turned `check_integrity` AND `simulate_payout` into
    internal_error for the rest of the cycle — irreversibly, because a draw is
    never edited or deleted. The two tools that detect every other kind of
    tampering could be disabled by one legitimate-looking pair of calls.
    """
    from nati.world import queries as q

    profits_before = q.fund(con, CYCLE_DAY).profits
    collected = int(con.execute(
        """SELECT COALESCE(SUM(price),0) FROM raffle_tickets
            WHERE kind='monthly' AND month=7 AND paid=1"""
    ).fetchone()[0])

    assert confirmed(con, ctx, "set_setting", key="monthly_prize",
                     value=profits_before + 1).for_the_model["ok"] is True

    held = con.execute(
        "SELECT number_1 FROM raffle_tickets WHERE kind='monthly' AND month=7 LIMIT 1"
    ).fetchone()[0]
    attempt = call(con, ctx, "record_draw", kind="monthly", month=7,
                   winning_number=held)
    assert attempt.for_the_model["ok"] is False
    assert attempt.reason == "prize_exceeds_income"

    # The books still work, which is the whole point.
    assert q.fund(con, CYCLE_DAY).profits == profits_before
    assert call(con, ctx, "check_integrity").for_the_model["ok"] is True
    assert call(con, ctx, "simulate_payout").for_the_model["ok"] is True

    # A prize the month actually covers is still payable.
    assert confirmed(con, ctx, "set_setting", key="monthly_prize",
                     value=collected).for_the_model["ok"] is True
    fine = confirmed(con, ctx, "record_draw", kind="monthly", month=7,
                     winning_number=held)
    assert fine.for_the_model["ok"] is True


def test_a_reversal_cannot_overdraw_the_fund(con, ctx) -> None:
    """REGRESSION: SOP B2 says the fund's cash can NEVER go negative.

    The solvency hook was wired only to `record_loan`, so lending the fund down
    to zero and then reversing a saving left cash at -$200. `check_integrity`
    reported it afterwards; "never" means refusing it.
    """
    from nati.world import queries as q

    cash = q.fund(con, CYCLE_DAY).cash
    lent = confirmed(con, ctx, "record_loan", member_id=3,
                     confirm_name=member(con, 3), amount=cash,
                     monthly_rate=3, money_from="fund")
    assert lent.for_the_model["ok"] is True
    assert q.fund(con, CYCLE_DAY).cash == 0

    saving_id = con.execute(
        """SELECT id FROM savings WHERE reversal_of IS NULL
            ORDER BY amount DESC LIMIT 1"""
    ).fetchone()[0]
    attempt = confirmed(con, ctx, "record_reversal", saving_id=saving_id)
    assert attempt.for_the_model["ok"] is False
    assert attempt.reason == "invariant_broken"
    assert q.fund(con, CYCLE_DAY).cash == 0, "the fund went negative"


def test_a_premature_attempt_does_not_burn_her_confirmation(con, ctx) -> None:
    """REGRESSION: the token was consumed BEFORE the turn was checked.

    A same-turn spend attempt popped a perfectly good confirmation, and only an
    accidental re-mint put it back. Worse, every re-mint stamped the CURRENT
    turn, which quietly moved the human gate: if the model made any failed call
    in the same turn as her "yes", her genuine confirmation was then refused as
    "same turn" and she was asked all over again.
    """
    args = {"key": "monthly_prize", "value": 70}

    def attempt(turn: int, **extra):
        return registry.dispatch(
            con, "set_setting", {**args, **extra},
            db.Ctx(conv_id="burn", seq=1, turn=turn),
        ).for_the_model

    token = attempt(1)["token"]
    confirm.delivered(token, 1)   # the summary reached her in turn 1

    # The model tries to spend it immediately. Refused — and must NOT consume it.
    assert attempt(1, confirm_token=token)["ok"] is False

    # It also fumbles with the wrong amount in the turn she actually says yes.
    registry.dispatch(
        con, "set_setting", {"key": "monthly_prize", "value": 999,
                             "confirm_token": token},
        db.Ctx(conv_id="burn", seq=1, turn=2),
    )

    # Her real "yes" still works. It did not before.
    assert attempt(2, confirm_token=token)["ok"] is True

    # And it is still single use.
    assert attempt(3, confirm_token=token)["ok"] is False
