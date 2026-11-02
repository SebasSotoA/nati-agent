"""Tests for the world: schema guarantees, audit trail, and the seed's figures.

No network. Each test builds a fresh world from schema.sql + the challenge fixture.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nati.config import WORLD_TODAY
from nati.tools import calc
from nati.world import Ctx, WorldError, db, insert, update
from nati.world import queries as q

TODAY = WORLD_TODAY  # 2026-08-04

# Month 8 is used deliberately in the schema-guarantee tests: the world is seeded
# through July, so August is empty with all 100 raffle numbers free. Without
# that, a test can pass for the WRONG reason — an earlier trigger fires and the
# rule under test is never exercised.
FREE_MONTH = 8


@pytest.fixture
def con(tmp_path: Path):
    """A clean world per test, on temporary disk."""
    path = tmp_path / "world.db"
    db.create(path, reset=True)
    connection = db.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def ctx() -> Ctx:
    return Ctx(conv_id="test-001", seq=1, tool="test", args={"x": 1})


def _count(con, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def _insert_ticket(con, **kw):
    fields = {
        "member_id": 5, "kind": "monthly", "month": FREE_MONTH, "year": 2026,
        "paid_on": TODAY, "paid": 1, "price": 10, "number_1": 0, "number_2": 1,
    }
    fields.update(kw)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return con.execute(
        f"INSERT INTO raffle_tickets ({cols}) VALUES ({marks})",  # noqa: S608
        tuple(fields.values()),
    )


# --------------------------------------------------------------------------
# The world is built, and it is representative
# --------------------------------------------------------------------------


def test_the_world_builds_from_the_seed(con: sqlite3.Connection) -> None:
    assert _count(con, "members") == 29
    # Two money sources only: the common fund, and the administrator's own money.
    assert _count(con, "lenders") == 2
    personal = con.execute(
        "SELECT COUNT(*) FROM lenders WHERE kind = 'personal'"
    ).fetchone()[0]
    assert personal == 1
    assert con.execute(
        "SELECT COUNT(*) FROM lenders WHERE kind = 'fund'"
    ).fetchone()[0] == 1
    for table in ("savings", "loans", "loan_payments", "loan_interest",
                  "raffle_tickets", "draws", "settings"):
        assert _count(con, table) > 0, f"{table} is empty"


def test_there_are_loans_from_both_lenders(con: sqlite3.Connection) -> None:
    """The world has to exercise the fund/personal boundary."""
    from_fund = con.execute(
        "SELECT COUNT(*) FROM loans WHERE lender_id = 0"
    ).fetchone()[0]
    personal = con.execute(
        "SELECT COUNT(*) FROM loans WHERE lender_id != 0"
    ).fetchone()[0]
    assert from_fund > 0 and personal > 0


def test_there_are_loans_in_every_status(con: sqlite3.Connection) -> None:
    statuses = {r["status"] for r in con.execute("SELECT DISTINCT status FROM loans")}
    assert statuses == {"active", "closed", "voided"}


def test_there_are_tickets_of_both_kinds_and_some_unpaid(
    con: sqlite3.Connection,
) -> None:
    kinds = {r["kind"] for r in con.execute("SELECT DISTINCT kind FROM raffle_tickets")}
    assert kinds == {"monthly", "quarterly"}
    unpaid = con.execute(
        "SELECT COUNT(*) FROM raffle_tickets WHERE paid = 0"
    ).fetchone()[0]
    assert unpaid > 0, "with no unpaid tickets the raffle-debt rule is not exercised"


def test_everyone_joined_at_the_start_of_the_cycle(con: sqlite3.Connection) -> None:
    """Confirmed rule: nobody joins once the cycle is running."""
    late = con.execute(
        "SELECT COUNT(*) FROM members WHERE joined_on > '2026-01-31'"
    ).fetchone()[0]
    assert late == 0


def test_july_is_still_undrawn(con: sqlite3.Connection) -> None:
    """The seed leaves July with tickets sold and no draw, on purpose."""
    sold = con.execute(
        "SELECT COUNT(*) FROM raffle_tickets WHERE month=7 AND year=2026 AND paid=1"
    ).fetchone()[0]
    assert sold > 0
    assert con.execute(
        "SELECT COUNT(*) FROM draws WHERE month=7 AND year=2026"
    ).fetchone()[0] == 0


def test_the_seed_contains_name_collisions(con: sqlite3.Connection) -> None:
    """Deliberate: the wrong-person guard needs something real to catch."""
    for needle in ("Amparo", "Liliana"):
        matches = con.execute(
            "SELECT COUNT(*) FROM members WHERE name LIKE ?", (f"%{needle}%",)
        ).fetchone()[0]
        assert matches >= 2, f"'{needle}' should match more than one member"


# --------------------------------------------------------------------------
# Guarantees pushed into the schema (a tool cannot forget them)
# --------------------------------------------------------------------------


def test_foreign_keys_are_always_on(con: sqlite3.Connection) -> None:
    """If this is off, the schema's guarantees silently evaporate."""
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO savings (member_id, date, amount) VALUES (999, ?, 1)",
            (TODAY,),
        )


def test_the_lender_is_immutable_in_the_schema(con: sqlite3.Connection) -> None:
    """Layer 1: the trigger. Not even raw SQL can reclassify a loan."""
    with pytest.raises(sqlite3.IntegrityError, match="[Ww]hose money"):
        con.execute("UPDATE loans SET lender_id = 1 WHERE id = 2")
    assert con.execute("SELECT lender_id FROM loans WHERE id=2").fetchone()[0] == 0


def test_the_lender_is_immutable_at_the_chokepoint(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """Layer 2: the column allowlist stops it before it reaches the trigger.

    It raises ProgrammingError and not WorldError on purpose: no tool should ask
    for this, so it is our bug and its message must not reach the model.
    """
    with pytest.raises(db.ProgrammingError):
        update(con, "loans", 2, {"lender_id": 1}, ctx)
    assert con.execute("SELECT lender_id FROM loans WHERE id=2").fetchone()[0] == 0


def test_the_immutability_message_does_not_teach_the_bypass(
    con: sqlite3.Connection,
) -> None:
    """It used to say literally "void the loan and create a new one" — which IS
    the bypass: voiding a fund loan and recreating it as personal diverts the
    group's interest into one pocket without touching the column."""
    with pytest.raises(sqlite3.IntegrityError) as exc:
        con.execute("UPDATE loans SET lender_id = 1 WHERE id = 2")
    text = str(exc.value).lower()
    for hint in ("void", "create a new", "lender_id", "trigger"):
        assert hint not in text, f"the message still hints at: {hint!r}"


def test_a_dead_loan_cannot_be_revived(con: sqlite3.Connection) -> None:
    for status in ("voided", "closed"):
        with pytest.raises(sqlite3.IntegrityError, match="status change"):
            con.execute("UPDATE loans SET status='active' WHERE status=?", (status,))


def test_the_status_can_move_from_active(con: sqlite3.Connection, ctx: Ctx) -> None:
    """What is immutable is whose money it was, not the lifecycle."""
    r = update(con, "loans", 1, {"status": "voided"}, ctx)
    assert r["before"]["status"] == "active"
    assert r["after"]["status"] == "voided"


def test_settings_are_never_overwritten(con: sqlite3.Connection, ctx: Ctx) -> None:
    """Asking for this is reasonable ("raise the price"), so the refusal is a
    WorldError with an explanation, not a technical error."""
    row = con.execute(
        "SELECT id FROM settings WHERE key='monthly_ticket_price'"
    ).fetchone()
    with pytest.raises(WorldError, match="new value"):
        update(con, "settings", int(row["id"]), {"value": "9999"}, ctx)
    # And it is closed in the schema too, in case anything gets past.
    with pytest.raises(sqlite3.IntegrityError, match="not overwritten"):
        con.execute("UPDATE settings SET value='9999' WHERE id=?", (row["id"],))


@pytest.mark.parametrize(
    "table", ["savings", "loan_payments", "loan_interest", "draws", "members"]
)
def test_movements_are_not_edited(
    con: sqlite3.Connection, ctx: Ctx, table: str
) -> None:
    """There is no edit tool. The refusal explains the correct route."""
    with pytest.raises(WorldError) as exc:
        update(con, table, 1, {"amount": 1}, ctx)
    message = str(exc.value)
    assert "not edited" in message or "not changed" in message


def test_the_audit_log_cannot_be_altered(con: sqlite3.Connection, ctx: Ctx) -> None:
    insert(con, "savings", {"member_id": 1, "date": TODAY, "amount": 10}, ctx)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE audit_log SET actor='other'")


@pytest.mark.parametrize(
    "table",
    ["members", "savings", "loans", "loan_payments", "loan_interest",
     "raffle_tickets", "draws", "settings", "lenders"],
)
def test_nothing_is_deleted(con: sqlite3.Connection, table: str) -> None:
    """Reversing entries, never erasures. Not from a tool, not from raw SQL."""
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(f"DELETE FROM {table}")  # noqa: S608


def test_there_is_only_one_fund(con: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO lenders (id,name,kind,member_id) VALUES (2,'Other','fund',NULL)"
        )


def test_personal_money_requires_a_person(con: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO lenders (id,name,kind,member_id) VALUES (3,'X','personal',NULL)"
        )


# --------------------------------------------------------------------------
# Raffle guarantees (using the empty month, so they fire for the right reason)
# --------------------------------------------------------------------------


def test_august_is_empty_for_these_tests(con: sqlite3.Connection) -> None:
    """Control: if August had tickets, the tests below would lie."""
    assert con.execute(
        "SELECT COUNT(*) FROM raffle_tickets WHERE month = ?", (FREE_MONTH,)
    ).fetchone()[0] == 0


def test_a_valid_ticket_does_go_in(con: sqlite3.Connection) -> None:
    """Positive control: the schema does not block legitimate data."""
    _insert_ticket(con)
    assert con.execute(
        "SELECT COUNT(*) FROM raffle_tickets WHERE month = ?", (FREE_MONTH,)
    ).fetchone()[0] == 1


def test_raffle_numbers_are_not_repeated(con: sqlite3.Connection) -> None:
    """Each member takes numbers nobody else holds. Hence one winner only."""
    _insert_ticket(con, member_id=5, number_1=40, number_2=41)
    # The case UNIQUE(kind,month,year,number_1) would NOT cover: somebody else's
    # number_2 reused as a number_1.
    with pytest.raises(sqlite3.IntegrityError, match="already holds"):
        _insert_ticket(con, member_id=9, number_1=41, number_2=42)
    with pytest.raises(sqlite3.IntegrityError, match="already holds"):
        _insert_ticket(con, member_id=9, number_1=42, number_2=40)


def test_nobody_holds_the_same_number_twice(con: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="number_1 <> number_2"):
        _insert_ticket(con, number_1=40, number_2=40)


def test_paid_and_paid_on_go_together(con: sqlite3.Connection) -> None:
    """A ticket marked paid with no date would count as income at EVERY cut-off."""
    with pytest.raises(sqlite3.IntegrityError, match="paid"):
        _insert_ticket(con, paid=1, paid_on=None)
    with pytest.raises(sqlite3.IntegrityError, match="paid"):
        _insert_ticket(con, paid=0, paid_on=TODAY)


def test_there_is_no_raffle_in_december(con: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="month BETWEEN 1 AND 11"):
        _insert_ticket(con, month=12, paid_on="2026-12-04")


def test_an_absurd_year_is_refused(con: sqlite3.Connection) -> None:
    """A sanity bound. The CYCLE bound cannot be a CHECK (it is dynamic): that is
    defended by removing `year` from the tool surface."""
    with pytest.raises(sqlite3.IntegrityError, match="year BETWEEN"):
        _insert_ticket(con, year=9999, paid_on="9999-08-04")


def test_a_tickets_price_is_frozen(con: sqlite3.Connection) -> None:
    """The price is written into the row: no settings change revalues it."""
    prices = {
        r["kind"]: r["price"]
        for r in con.execute("SELECT DISTINCT kind, price FROM raffle_tickets")
    }
    assert prices == {"monthly": 10, "quarterly": 20}
    with pytest.raises(sqlite3.IntegrityError, match="price > 0"):
        _insert_ticket(con, price=0)


def test_only_one_ticket_per_member_per_month(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """With FREE numbers, so this rule fires and not the numbers rule."""
    _insert_ticket(con, member_id=1, number_1=70, number_2=71)
    with pytest.raises(WorldError, match="already has a ticket"):
        insert(
            con, "raffle_tickets",
            {"member_id": 1, "kind": "monthly", "month": FREE_MONTH, "year": 2026,
             "paid_on": TODAY, "paid": 1, "price": 10,
             "number_1": 72, "number_2": 73},
            ctx,
        )


# --------------------------------------------------------------------------
# Loan movement guarantees
# --------------------------------------------------------------------------


def test_a_payment_must_belong_to_the_loans_holder(con: sqlite3.Connection) -> None:
    """Otherwise the payment cuts one person's debt while the audit trail says
    somebody else paid."""
    holder = con.execute("SELECT member_id FROM loans WHERE id=1").fetchone()[0]
    other = 23 if holder != 23 else 22
    with pytest.raises(sqlite3.IntegrityError, match="does not belong"):
        con.execute(
            "INSERT INTO loan_payments (loan_id,member_id,date,amount) "
            "VALUES (1,?,?,50)", (other, TODAY),
        )


def test_no_movement_before_the_loan_existed(con: sqlite3.Connection) -> None:
    """A movement dated earlier slips to the front of the chronological order and
    changes how every later payment was split."""
    holder = con.execute("SELECT member_id FROM loans WHERE id=1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="before the loan existed"):
        con.execute(
            "INSERT INTO loan_payments (loan_id,member_id,date,amount) "
            "VALUES (1,?,'2026-01-01',50)", (holder,),
        )
    # A loan with no payments, so this isolates the "before the loan" trigger
    # rather than the interest-before-a-payment one (both would reject it).
    clean = con.execute(
        """SELECT id FROM loans
            WHERE NOT EXISTS (SELECT 1 FROM loan_payments p WHERE p.loan_id = loans.id)
            LIMIT 1"""
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="before the loan existed"):
        con.execute(
            "INSERT INTO loan_interest (loan_id,date,interest_amount) "
            "VALUES (?,'2026-01-01',15)", (clean,),
        )


def test_interest_cannot_predate_a_payment_already_received(
    con: sqlite3.Connection,
) -> None:
    """SOP F5, the clause that used to have no enforcement at all.

    Backdating interest behind an existing payment retroactively reclassifies
    capital the borrower ALREADY repaid as interest — on a personal loan that
    becomes the administrator's private income, on a fund loan it invents
    distributable profit no cash backs. The borrower ends up owing MORE after
    paying, and every balance check still reported "everything balances".
    """
    row = con.execute(
        """SELECT loan_id, MIN(date) AS first_payment FROM loan_payments
            GROUP BY loan_id LIMIT 1"""
    ).fetchone()
    lid, first_payment = int(row["loan_id"]), row["first_payment"]

    earlier = "2026-04-01"
    assert earlier < first_payment, "fixture assumption"
    with pytest.raises(sqlite3.IntegrityError, match="before a payment"):
        con.execute(
            "INSERT INTO loan_interest (loan_id,date,interest_amount) "
            "VALUES (?,?,50)", (lid, earlier),
        )

    # Dated after the last payment is still fine — the book grows forward.
    last_payment = con.execute(
        "SELECT MAX(date) FROM loan_payments WHERE loan_id = ?", (lid,)
    ).fetchone()[0]
    con.execute(
        "INSERT INTO loan_interest (loan_id,date,interest_amount) "
        "VALUES (?,?,50)", (lid, "2026-08-15"),
    )
    assert "2026-08-15" > last_payment


def test_one_interest_charge_per_loan_per_month(con: sqlite3.Connection) -> None:
    """Deduped on the month DERIVED from the date, not on an argument."""
    row = con.execute(
        "SELECT date FROM loan_interest WHERE loan_id=1 ORDER BY date DESC LIMIT 1"
    ).fetchone()
    same_month = row["date"][:8] + "15"   # same year-month, different day
    with pytest.raises(sqlite3.IntegrityError, match="loan_interest_one_per_month"):
        con.execute(
            "INSERT INTO loan_interest (loan_id,date,interest_amount) "
            "VALUES (1,?,15)", (same_month,),
        )


# --------------------------------------------------------------------------
# Dated settings
# --------------------------------------------------------------------------


def test_the_ticket_price_respects_its_effective_date(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """Changing the price in September must not rewrite January."""
    assert q.ticket_price(con, "monthly", "2026-03-15") == 10
    insert(
        con, "settings",
        {"key": "monthly_ticket_price", "value": "12",
         "effective_from": "2026-09-01", "note": "increase"},
        ctx,
    )
    assert q.ticket_price(con, "monthly", "2026-03-15") == 10
    assert q.ticket_price(con, "monthly", "2026-09-15") == 12


def test_the_two_raffles_have_different_prices(con: sqlite3.Connection) -> None:
    assert q.ticket_price(con, "monthly", TODAY) == 10
    assert q.ticket_price(con, "quarterly", TODAY) == 20


def test_a_money_setting_with_no_entry_fails_loudly(
    con: sqlite3.Connection,
) -> None:
    """An unreadable price must NOT fall back to zero silently.

    If `monthly_ticket_price` were assumed to be 0, the whole year's raffle
    income would become zero and no unpaid ticket would count as a debt. An
    error that size has to be loud.
    """
    with pytest.raises(q.InvalidSetting, match="monthly_ticket_price"):
        q.setting_int(con, "monthly_ticket_price", "2025-01-01", -1)


def test_a_money_setting_with_garbage_fails_loudly(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    insert(
        con, "settings",
        {"key": "monthly_ticket_price", "value": "abc",
         "effective_from": "2026-09-01", "note": "poison"},
        ctx,
    )
    with pytest.raises(q.InvalidSetting, match="not a whole number"):
        q.setting_int(con, "monthly_ticket_price", "2026-09-15", 0)


def test_a_non_money_setting_does_use_its_default(con: sqlite3.Connection) -> None:
    """For anything that is not money, a default is acceptable."""
    assert q.setting_int(con, "a_key_that_does_not_exist", TODAY, 42) == 42


def test_the_administrator_is_in_the_settings(con: sqlite3.Connection) -> None:
    assert q.setting_int(con, "admin_member_id", TODAY, 0) == 1


# --------------------------------------------------------------------------
# Real balances from the seed
# --------------------------------------------------------------------------


def test_closed_loans_are_at_zero(con: sqlite3.Connection) -> None:
    """Invariant: a 'closed' loan cannot have anything outstanding."""
    closed = [
        int(r["id"])
        for r in con.execute("SELECT id FROM loans WHERE status = 'closed'")
    ]
    assert closed, "the seed should have at least one closed loan"
    for loan_id in closed:
        balance = q.loan_balance(con, loan_id, TODAY)
        assert balance.is_paid_off, f"loan {loan_id} is closed but owes {balance.total}"
        assert balance.overpaid == 0, f"loan {loan_id} was overpaid"


def test_carlos_loan_adds_up(con: sqlite3.Connection) -> None:
    """Loan 1: $500 at 3%. Five charges of $15 and two payments of $100.

    Chronology (this is why order matters, not a raw sum):
      03-31, 04-30  charge $30
      05-10         pays  $100 -> $30 interest, $70 capital
      05-31, 06-30  charge $30
      07-10         pays  $100 -> $30 interest, $70 capital
      07-31         charge $15  <- left overdue, the payment already passed
    """
    b = q.loan_balance(con, 1, TODAY)
    assert b.interest_charged == 75
    assert b.interest_paid == 60          # July's charge is still overdue
    assert b.interest_outstanding == 15
    assert b.capital_paid == 140
    assert b.capital_outstanding == 360
    assert b.total == 375


# --------------------------------------------------------------------------
# The fund / personal boundary
# --------------------------------------------------------------------------


def test_personal_interest_stays_out_of_the_fund(con: sqlite3.Connection) -> None:
    f = q.fund(con, TODAY)
    all_interest = con.execute(
        "SELECT COALESCE(SUM(interest_amount),0) FROM loan_interest"
    ).fetchone()[0]
    personal_only = con.execute(
        """SELECT COALESCE(SUM(i.interest_amount),0)
             FROM loan_interest i JOIN loans l ON l.id = i.loan_id
            WHERE l.lender_id != 0"""
    ).fetchone()[0]
    assert personal_only > 0, "the seed must contain personal-money interest"
    assert f.interest_charged == all_interest - personal_only


def test_personal_capital_does_not_leave_the_funds_cash(
    con: sqlite3.Connection,
) -> None:
    f = q.fund(con, TODAY)
    fund_only = con.execute(
        """SELECT COALESCE(SUM(principal),0) FROM loans
            WHERE lender_id = 0 AND status != 'voided'"""
    ).fetchone()[0]
    personal = con.execute(
        """SELECT COALESCE(SUM(principal),0) FROM loans
            WHERE lender_id != 0 AND status != 'voided'"""
    ).fetchone()[0]
    assert personal > 0
    assert f.capital_out == fund_only
    assert f.capital_out < fund_only + personal


def test_there_is_exactly_one_private_pocket_the_administrator(
    con: sqlite3.Connection,
) -> None:
    """The domain has two money sources: the fund and the administrator's own
    money. So there is exactly one 'personal' lender, it is the administrator,
    and every personal loan is hers — no ambiguity about whose money it is."""
    lenders = con.execute(
        "SELECT id, kind, member_id FROM lenders ORDER BY id"
    ).fetchall()
    assert [(r["id"], r["kind"]) for r in lenders] == [(0, "fund"), (1, "personal")]

    admin_member_id = int(con.execute(
        "SELECT value FROM settings WHERE key = 'admin_member_id'"
    ).fetchone()[0])
    assert int(lenders[1]["member_id"]) == admin_member_id

    # Every personal loan points at that one lender row.
    other = con.execute(
        "SELECT COUNT(*) FROM loans WHERE lender_id != 0 AND lender_id != 1"
    ).fetchone()[0]
    assert other == 0


def test_the_funds_cash_does_not_double_count_interest(
    con: sqlite3.Connection,
) -> None:
    """The bug I made: adding interest CHARGED as well as the payments.

    Interest reaches the cash box inside a payment. Cash is only money movement:
    savings, capital that left, payments that came in, raffle.
    """
    f = q.fund(con, TODAY)
    expected = (
        f.savings - f.capital_out + f.payments_in + f.raffle_income - f.prizes_paid
    )
    assert f.cash == expected
    assert f.interest_charged > 0
    assert f.cash != expected + f.interest_charged


def test_equity_is_cash_plus_loans_outstanding(con: sqlite3.Connection) -> None:
    f = q.fund(con, TODAY)
    assert f.loans_outstanding == f.capital_out - f.capital_recovered
    assert f.equity == f.cash + f.loans_outstanding
    assert f.loans_outstanding > 0, "the seed should have money out on loan"


# --------------------------------------------------------------------------
# The payout over the real world
# --------------------------------------------------------------------------


def test_the_seeds_payout_balances(con: sqlite3.Connection) -> None:
    p = q.payout(con, TODAY)
    members = _count(con, "members")
    saved = con.execute("SELECT COALESCE(SUM(amount),0) FROM savings").fetchone()[0]
    assert len(p.shares) == members
    assert p.balances
    assert sum(s.profit for s in p.shares) == p.total_profits
    assert p.total_savings == saved


def test_everybody_gets_the_same_profit(con: sqlite3.Connection) -> None:
    """Confirmed by the administrator: profits are split equally."""
    p = q.payout(con, TODAY)
    profits = {s.profit for s in p.shares}
    # At most one dollar apart, from the largest-remainder rounding.
    assert max(profits) - min(profits) <= 1
    # But savings do differ: each person gets their own back.
    assert len({s.saved for s in p.shares}) > 1


def test_raffle_debt_is_netted_in_the_payout(con: sqlite3.Connection) -> None:
    """Members with unpaid tickets receive less."""
    p = q.payout(con, TODAY)
    with_debt = [s for s in p.shares if s.ticket_debt > 0]
    assert with_debt, "the seed should have members owing raffle money"
    for share in with_debt:
        assert share.to_pay == share.gross - share.total_debt


def test_the_independent_balance_check_passes(con: sqlite3.Connection) -> None:
    check = q.payout_balance_check(con, TODAY)
    assert check.ok, check.text


def test_the_independent_check_catches_invented_money(
    con: sqlite3.Connection,
) -> None:
    p = q.payout(con, TODAY)
    fake = calc.check_balance(
        p,
        savings_independent=p.total_savings,
        profits_independent=p.total_profits + 1,
    )
    assert not fake.ok
    assert fake.difference == -1


def test_a_voided_loan_stops_being_a_debt(con: sqlite3.Connection, ctx: Ctx) -> None:
    holder = con.execute("SELECT member_id FROM loans WHERE id=1").fetchone()[0]
    assert q.debt_by_member(con, TODAY).get(holder, 0) > 0
    update(con, "loans", 1, {"status": "voided"}, ctx)
    assert q.debt_by_member(con, TODAY).get(holder, 0) == 0


def test_the_payout_only_includes_members_who_had_joined(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """REGRESSION: at 2026-02-28 three members used to receive profit with $0 saved.

    Everyone joins in January now, so the case is constructed rather than seeded.
    """
    insert(
        con, "members",
        {"name": "Late Joiner", "phone": "3999999999", "joined_on": "2026-06-01"},
        ctx,
    )
    p = q.payout(con, "2026-03-31")
    names = {s.name for s in p.shares}
    assert "Late Joiner" not in names
    assert all(s.saved > 0 for s in p.shares)


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------


def test_two_worlds_from_the_same_seed_are_identical(tmp_path: Path) -> None:
    """The hash anchors each trace to an exact world: it has to be stable."""
    a = db.connect(db.create(tmp_path / "a.db", reset=True))
    b = db.connect(db.create(tmp_path / "b.db", reset=True))
    try:
        assert db.world_hash(a) == db.world_hash(b)
    finally:
        a.close()
        b.close()


def test_a_single_write_changes_the_hash(con: sqlite3.Connection, ctx: Ctx) -> None:
    before = db.world_hash(con)
    insert(con, "savings", {"member_id": 1, "date": TODAY, "amount": 1}, ctx)
    assert db.world_hash(con) != before


def test_the_audit_log_does_not_affect_the_world_hash(
    con: sqlite3.Connection,
) -> None:
    """The hash covers domain state; the audit log always grows."""
    dump = db.canonical_dump(con)
    assert "audit_log" not in dump
    assert "members" in dump


def test_creating_without_reset_protects_an_existing_world(tmp_path: Path) -> None:
    path = tmp_path / "w.db"
    db.create(path, reset=True)
    with pytest.raises(WorldError, match="--reset"):
        db.create(path, reset=False)


def test_connecting_to_a_missing_world_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(WorldError, match="nati seed --reset"):
        db.connect(tmp_path / "does-not-exist.db")


# --------------------------------------------------------------------------
# Message hygiene
# --------------------------------------------------------------------------

# Patterns that would leak the internal structure. They are SQL-specific
# phrasings rather than bare words, because in English several SQL keywords are
# ordinary vocabulary: "Check who it belongs to" is a perfectly good sentence,
# and banning the word `check` alone flags it. `constraint failed` cannot appear
# in a sentence written for a person.
FORBIDDEN_IN_MESSAGES = (
    "trigger",
    "constraint",
    "sqlite",
    "check constraint",
    "unique constraint",
    "foreign key",
    "raise(abort",
    # Column and table names: distinctive enough to match bare.
    "lender_id", "member_id", "loan_id", "number_1", "number_2",
    "raffle_tickets", "loan_payments", "loan_interest", "audit_log",
)


def _refusal_messages(con, ctx) -> list[str]:
    """Every WorldError a tool could end up showing the person."""
    attempts = [
        lambda: insert(con, "savings",
                       {"member_id": 1, "date": TODAY, "amount": -5}, ctx),
        lambda: insert(con, "savings",
                       {"member_id": 999, "date": TODAY, "amount": 5}, ctx),
        lambda: insert(con, "members",
                       {"name": "X", "phone": "3101234567",
                        "joined_on": TODAY}, ctx),
        lambda: update(con, "settings", 1, {"value": "9"}, ctx),
        lambda: update(con, "savings", 1, {"amount": 1}, ctx),
        lambda: update(con, "loans", 8, {"status": "active"}, ctx),
        lambda: insert(con, "savings",
                       {"member_id": 1, "date": TODAY, "amount": 2**63}, ctx),
    ]
    messages = []
    for attempt in attempts:
        try:
            attempt()
        except WorldError as exc:
            messages.append(str(exc))
        con.rollback()
    return messages


def test_no_visible_message_reveals_the_control(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """Nothing the person can read names the internal structure.

    If an attacker knows WHICH control fired, they can plan around it.
    """
    messages = _refusal_messages(con, ctx)
    assert len(messages) >= 6, "several distinct refusals were expected"
    for message in messages:
        lowered = message.lower()
        leaks = [p for p in FORBIDDEN_IN_MESSAGES if p in lowered]
        assert not leaks, f"{message!r} leaks {leaks}"


def test_a_huge_number_does_not_return_a_traceback(
    con: sqlite3.Connection, ctx: Ctx
) -> None:
    """2**63 does not fit in an INTEGER and used to escape as OverflowError."""
    with pytest.raises(WorldError, match="too large"):
        insert(con, "savings",
               {"member_id": 1, "date": TODAY, "amount": 2**63}, ctx)


# --------------------------------------------------------------------------
# The write chokepoint
# --------------------------------------------------------------------------


def test_insert_leaves_an_audit_trail(con: sqlite3.Connection, ctx: Ctx) -> None:
    r = insert(con, "savings", {"member_id": 3, "date": TODAY, "amount": 75}, ctx)
    assert r["id"] > 0 and r["audit_id"] > 0

    row = con.execute("SELECT * FROM audit_log WHERE id = ?", (r["audit_id"],)).fetchone()
    assert row["conv_id"] == "test-001"
    assert row["seq"] == 1
    assert row["tool"] == "test"
    assert row["table_name"] == "savings"
    assert row["row_pk"] == r["id"]
    assert row["before_json"] is None
    assert json.loads(row["after_json"])["amount"] == 75


def test_update_stores_before_and_after(con: sqlite3.Connection, ctx: Ctx) -> None:
    r = update(con, "loans", 1, {"status": "closed"}, ctx)
    row = con.execute("SELECT * FROM audit_log WHERE id=?", (r["audit_id"],)).fetchone()
    assert json.loads(row["before_json"])["status"] == "active"
    assert json.loads(row["after_json"])["status"] == "closed"


def test_a_failed_write_leaves_nothing(con: sqlite3.Connection, ctx: Ctx) -> None:
    """Atomicity: if the insert breaks a rule, neither the row nor the audit stays."""
    before_savings = _count(con, "savings")
    before_audit = _count(con, "audit_log")
    with pytest.raises(WorldError):
        insert(con, "savings", {"member_id": 1, "date": TODAY, "amount": -5}, ctx)
    assert _count(con, "savings") == before_savings
    assert _count(con, "audit_log") == before_audit


def test_an_unknown_table_is_refused(con: sqlite3.Connection, ctx: Ctx) -> None:
    with pytest.raises(WorldError, match="unknown table"):
        insert(con, "audit_log", {"ts": TODAY}, ctx)
