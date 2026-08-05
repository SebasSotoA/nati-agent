"""Tests for the write chokepoint: atomicity, invariants, the clock.

What is tested here is not "the gate refuses X" (that is test_world) but the
properties of the MACHINERY: that a batch is all-or-nothing, that a post-write
invariant undoes its own write and its audit row together, and that there is one
clock.

These are the ones that fail silently in a refactor and leave the world half-done.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nati import clock
from nati.config import WORLD_TODAY
from nati.world import Ctx, WorldError, db, insert, update
from nati.world import queries as q

TODAY = WORLD_TODAY


@pytest.fixture
def con(tmp_path: Path):
    path = tmp_path / "world.db"
    db.create(path, reset=True)
    connection = db.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def ctx() -> Ctx:
    return Ctx(conv_id="test-writes", seq=1, tool="test")


def _count(con, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def _holder(con, loan_id: int) -> int:
    return int(
        con.execute(
            "SELECT member_id FROM loans WHERE id = ?", (loan_id,)
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------
# Batch atomicity
# --------------------------------------------------------------------------


def test_a_batch_is_all_or_nothing(con: sqlite3.Connection, ctx: Ctx) -> None:
    """The bug this closes: sqlite3's `with con:` COMMITs on exit.

    With that, charging a month's interest across nine loans was NINE separate
    transactions. If the sixth hit a gate, the first five were already written
    and nothing rolled them back: the world was left half-done, in a state no
    gate produced.
    """
    active = [
        int(r["id"])
        for r in con.execute(
            "SELECT id FROM loans WHERE lender_id=0 AND status='active' ORDER BY id"
        )
    ]
    assert len(active) >= 4, "the seed should have several active loans"

    rows_before = _count(con, "loan_interest")
    hash_before = db.world_hash(con)

    with pytest.raises(WorldError):
        with db.transaction(con):
            for i, loan_id in enumerate(active):
                # The third one carries a date before the loan existed, so a
                # trigger stops it midway through the batch.
                when = "2020-01-01" if i == 2 else TODAY
                insert(
                    con, "loan_interest",
                    {"loan_id": loan_id, "date": when, "interest_amount": 1},
                    ctx,
                )

    assert _count(con, "loan_interest") == rows_before
    assert db.world_hash(con) == hash_before


def test_a_clean_batch_is_written_in_full(con: sqlite3.Connection, ctx: Ctx) -> None:
    """Control: with no failures, the whole batch lands."""
    active = [
        int(r["id"])
        for r in con.execute(
            "SELECT id FROM loans WHERE lender_id=0 AND status='active'"
        )
    ]
    before = _count(con, "loan_interest")
    with db.transaction(con):
        for loan_id in active:
            insert(
                con, "loan_interest",
                {"loan_id": loan_id, "date": TODAY, "interest_amount": 1},
                ctx,
            )
    assert _count(con, "loan_interest") == before + len(active)


def test_transactions_nest_without_committing(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """An inner block must NOT commit the outer one."""
    before = _count(con, "savings")
    with pytest.raises(RuntimeError):
        with db.transaction(con):
            insert(con, "savings",
                   {"member_id": 1, "date": TODAY, "amount": 10}, ctx)
            with db.transaction(con):  # joins, does not open another
                insert(con, "savings",
                       {"member_id": 2, "date": TODAY, "amount": 20}, ctx)
            raise RuntimeError("something went wrong afterwards")
    assert _count(con, "savings") == before, "the inner block committed"


# --------------------------------------------------------------------------
# The `verify` hook: POST-write invariants
# --------------------------------------------------------------------------


def _no_overpayment(loan_id: int):
    """Invariant: after writing, the loan must not be overpaid.

    It has to be checked afterwards because a loan's balance depends on the full
    chronological order of its movements, not just on the amount arriving.
    """

    def check(c: sqlite3.Connection, _pk: int) -> None:
        balance = q.loan_balance(c, loan_id, TODAY)
        if balance.overpaid > 0:
            raise db.InvariantBroken(
                "That payment is more than what is left to pay on that loan."
            )

    return check


def test_a_broken_invariant_undoes_the_write(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    loan_id = 1
    balance = q.loan_balance(con, loan_id, TODAY)
    assert balance.total > 0

    payments_before = _count(con, "loan_payments")
    audit_before = _count(con, "audit_log")
    hash_before = db.world_hash(con)

    with pytest.raises(db.InvariantBroken, match="more than what is left"):
        insert(
            con, "loan_payments",
            {"loan_id": loan_id, "member_id": _holder(con, loan_id),
             "date": TODAY, "amount": balance.total + 500},
            ctx,
            verify=_no_overpayment(loan_id),
        )

    assert _count(con, "loan_payments") == payments_before
    assert db.world_hash(con) == hash_before
    # And the audit row is undone WITH the write: no orphan row claiming
    # something happened that did not.
    assert _count(con, "audit_log") == audit_before


def test_a_satisfied_invariant_lets_it_through(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    loan_id = 1
    r = insert(
        con, "loan_payments",
        {"loan_id": loan_id, "member_id": _holder(con, loan_id),
         "date": TODAY, "amount": 50},
        ctx,
        verify=_no_overpayment(loan_id),
    )
    assert r["id"] > 0 and r["audit_id"] > 0


def test_the_invariant_sees_the_state_already_written(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """The hook runs after the insert: it has to see the new row."""
    loan_id = 1
    seen: list[int] = []

    def check(c: sqlite3.Connection, _pk: int) -> None:
        seen.append(
            c.execute(
                "SELECT COUNT(*) FROM loan_payments WHERE loan_id = ?", (loan_id,)
            ).fetchone()[0]
        )

    before = con.execute(
        "SELECT COUNT(*) FROM loan_payments WHERE loan_id = ?", (loan_id,)
    ).fetchone()[0]
    insert(
        con, "loan_payments",
        {"loan_id": loan_id, "member_id": _holder(con, loan_id),
         "date": TODAY, "amount": 1},
        ctx, verify=check,
    )
    assert seen == [before + 1]


# --------------------------------------------------------------------------
# Auditing refused attempts
# --------------------------------------------------------------------------


def test_a_refusal_leaves_evidence(con: sqlite3.Connection) -> None:
    """In this exercise the refusals ARE the product.

    A session that hits fifteen gates must leave fifteen rows. If only successes
    were audited, a complete attack would leave no trace at all.
    """
    before = _count(con, "audit_log")
    audit_id = db.audit_refusal(
        con,
        Ctx(conv_id="c1", seq=7, tool="record_payment",
            args={"amount": 9_999}, raw_args={"amount": "9,999"}),
        reason="payment_exceeds_balance",
        table="loan_payments",
    )
    assert _count(con, "audit_log") == before + 1

    row = con.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()
    assert row["tool"] == "record_payment"
    assert row["conv_id"] == "c1" and row["seq"] == 7
    assert row["row_pk"] is None          # nothing was written
    assert row["after_json"] is None
    assert "refused" in row["args_json"]
    assert "payment_exceeds_balance" in row["args_json"]


def test_the_audit_keeps_the_raw_arguments(con: sqlite3.Connection) -> None:
    """If only the normalized form were stored, normalization would hide the attack."""
    audit_id = db.audit_refusal(
        con,
        Ctx(tool="t", args={"amount": 50_000}, raw_args={"amount": "50,000.00"}),
        reason="x",
    )
    args = con.execute(
        "SELECT args_json FROM audit_log WHERE id = ?", (audit_id,)
    ).fetchone()[0]
    assert "50,000.00" in args     # what the model sent
    assert "50000" in args         # what was going to be written


# --------------------------------------------------------------------------
# The column allowlist
# --------------------------------------------------------------------------


def test_marking_a_ticket_paid_cannot_touch_its_numbers(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """The allowlist lives at the chokepoint, not in 19 disciplined tools."""
    unpaid = con.execute(
        "SELECT id FROM raffle_tickets WHERE paid = 0 LIMIT 1"
    ).fetchone()
    assert unpaid is not None
    with pytest.raises(db.ProgrammingError, match="number_1"):
        update(
            con, "raffle_tickets", int(unpaid["id"]),
            {"paid": 1, "paid_on": TODAY, "number_1": 7}, ctx,
        )
    # The legitimate change goes through.
    update(con, "raffle_tickets", int(unpaid["id"]), {"paid": 1, "paid_on": TODAY}, ctx)


def test_ids_are_not_assigned_by_hand(con: sqlite3.Connection, ctx: Ctx) -> None:
    """SQLite accepts explicit and negative rowids, which breaks the dump order."""
    with pytest.raises(db.ProgrammingError, match="ids are not assigned"):
        insert(con, "savings",
               {"id": -5, "member_id": 1, "date": TODAY, "amount": 100}, ctx)


def test_lenders_are_never_written_by_a_tool(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    with pytest.raises(db.ProgrammingError):
        insert(con, "lenders",
               {"name": "Other", "kind": "personal", "member_id": 2}, ctx)


# --------------------------------------------------------------------------
# One clock, in Bogota
# --------------------------------------------------------------------------


def test_the_clock_is_in_bogota() -> None:
    """UTC-5. With a UTC clock, between 19:00 and midnight Bogota time the UTC
    date is already tomorrow, and a "no future dates" gate would accept it."""
    assert str(clock.ZONE) == "America/Bogota"


def test_the_clock_can_be_frozen() -> None:
    with clock.frozen("2026-08-04"):
        assert clock.today_iso() == "2026-08-04"
        assert clock.now_iso().startswith("2026-08-04")


def test_the_timestamp_carries_its_zone() -> None:
    """A timestamp with no zone is a trap for later: nobody knows what it meant."""
    with clock.frozen("2026-08-04"):
        assert clock.now_iso().endswith("-05:00")


def test_the_audit_uses_the_frozen_clock(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """So audit dumps are reproducible in tests."""
    with clock.frozen("2026-08-04"):
        r = insert(con, "savings",
                   {"member_id": 1, "date": TODAY, "amount": 10}, ctx)
    ts = con.execute(
        "SELECT ts FROM audit_log WHERE id = ?", (r["audit_id"],)
    ).fetchone()[0]
    assert ts.startswith("2026-08-04")


def test_interest_cannot_be_backdated_behind_a_payment(con, ctx) -> None:
    """The worst bug the red-team audit found, as a regression test.

    `_not_before_last_movement` existed and its docstring named this exact
    attack, but it was only called from `record_payment` — never from
    `accrue_monthly_interest`, the ONLY path that inserts interest. Backdating
    interest behind a payment retroactively reclassifies capital the borrower
    already repaid as interest: on a personal loan that becomes the
    administrator's private income, on a fund loan it invents distributable
    profit no cash backs. The borrower ends up owing MORE after paying, and
    every balance check still said "everything balances".
    """
    from nati.tools import confirm, registry
    from nati.world import queries as q

    confirm.reset()
    turn1 = db.Ctx(conv_id="regress", seq=1, turn=1)
    turn2 = db.Ctx(conv_id="regress", seq=1, turn=2)
    args = dict(member_id=2, confirm_name="Carlos Ramirez", amount=1000,
                monthly_rate=5, money_from="personal", lender_member_id=1,
                date="2026-02-01")
    token = registry.dispatch(con, "record_loan", args, turn1).for_the_model["token"]
    made = registry.dispatch(
        con, "record_loan", {**args, "confirm_token": token}, turn2)
    loan_id = made.for_the_model["loan_id"]

    paid = registry.dispatch(
        con, "record_payment",
        {"loan_id": loan_id, "amount": 500, "date": "2026-07-20"}, turn2)
    assert paid.for_the_model["ok"] is True, paid.for_the_model
    before = q.loan_balance(con, loan_id, "2026-08-04")
    assert before.total == 500 and before.interest_paid == 0

    for month in (2, 3, 4, 5, 6):
        result = registry.dispatch(
            con, "accrue_monthly_interest",
            {"month": month, "loan_id": loan_id}, turn2)
        assert not result.for_the_model.get("charged"), \
            f"month {month} was backdated behind the payment"

    after = q.loan_balance(con, loan_id, "2026-08-04")
    assert after.total == 500, "the borrower's debt was inflated"
    assert after.interest_paid == 0, "repaid capital was reclassified as interest"
    assert q.personal_interest(con, "2026-08-04").get(1, 0) == 0

    # Charging a month AFTER the payment is still normal work.
    ahead = registry.dispatch(
        con, "accrue_monthly_interest", {"month": 8, "loan_id": loan_id}, turn2)
    assert ahead.for_the_model["charged"], "legitimate accrual was blocked"


def test_the_balance_check_is_not_circular(con, ctx) -> None:
    """It used to verify interest_collected against itself, so an invented
    profit still reported ok=True."""
    import inspect

    from nati.world import queries as q

    code = "\n".join(
        line for line in inspect.getsource(q.payout_balance_check).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "f.interest_collected" not in code, \
        "the check is deriving interest from the number it is meant to verify"
    assert q.payout_balance_check(con, "2026-08-04").ok
