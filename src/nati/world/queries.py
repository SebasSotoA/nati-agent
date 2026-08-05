"""Read queries that translate the world into the structures `calc` works on.

Separate from `calc` on purpose: `calc` is pure arithmetic, testable with no
database. The SQL lives here. Separate from `db` because `db` is writes and
connections.

The fund (lender_id = 0) versus personal (lender_id > 0) split is applied **in
the SQL**, not afterwards: it is the fiduciary boundary, and it cannot depend on
somebody remembering to filter.
"""

from __future__ import annotations

import sqlite3

from nati import clock
from nati.tools import calc

FUND_ID = 0


def cycle_year(con: sqlite3.Connection) -> int:
    """The active cycle year. Single source for reads and writes."""
    return setting_int(con, "cycle_year", clock.today_iso(), clock.today().year)


# --------------------------------------------------------------------------
# Dated settings
# --------------------------------------------------------------------------


class InvalidSetting(Exception):
    """A money setting cannot be read as a whole number."""


# Keys that represent money. An unreadable value in one of these must NOT fall
# back to a silent zero: if `monthly_ticket_price` were assumed to be 0, the
# whole year's raffle income would become zero and no unpaid ticket would count
# as a debt. An error that size has to be loud.
MONEY_KEYS = frozenset(
    {
        "monthly_ticket_price",
        "quarterly_ticket_price",
        "monthly_prize",
        "quarterly_prize",
    }
)


def setting(con: sqlite3.Connection, key: str, date: str) -> str | None:
    """The value of `key` that was in effect on `date`.

    This is why changing the ticket price in August does not rewrite January's
    raffle income: each month is valued with the entry that applied to it.
    """
    # `id DESC` breaks a same-date tie: several entries may share a day (a
    # correction made minutes after a mistake), and the one that applies is the
    # last one written. Without the tiebreak SQLite could return either row.
    row = con.execute(
        """SELECT value FROM settings
            WHERE key = ? AND effective_from <= ?
            ORDER BY effective_from DESC, id DESC LIMIT 1""",
        (key, date),
    ).fetchone()
    return row["value"] if row else None


def setting_int(con: sqlite3.Connection, key: str, date: str, default: int) -> int:
    """Read a setting as an integer.

    Money keys have NO silent default: if missing or unreadable, this raises
    `InvalidSetting`. For everything else a default is acceptable.
    """
    value = setting(con, key, date)
    is_money = key in MONEY_KEYS

    if value is None:
        if is_money:
            raise InvalidSetting(f"no value for '{key}' in effect on {date}")
        return default

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        if is_money:
            raise InvalidSetting(
                f"'{key}' has a value that is not a whole number: {value!r}"
            ) from exc
        return default

    if is_money and number < 0:
        raise InvalidSetting(f"'{key}' cannot be negative: {number}")
    return number


def ticket_price(con: sqlite3.Connection, kind: str, date: str) -> int:
    """The price of a ticket of that kind, as it was on that date."""
    key = "monthly_ticket_price" if kind == "monthly" else "quarterly_ticket_price"
    return setting_int(con, key, date, 0)


# --------------------------------------------------------------------------
# Loans
# --------------------------------------------------------------------------


def loan_movements(
    con: sqlite3.Connection, loan_id: int, upto: str
) -> list[calc.Movement]:
    """Interest charged and payments received up to a date, unordered.

    `loan_balance` does the ordering; this only collects.
    """
    movements: list[calc.Movement] = []
    for row in con.execute(
        """SELECT date, interest_amount AS amount FROM loan_interest
            WHERE loan_id = ? AND date <= ?""",
        (loan_id, upto),
    ):
        movements.append(calc.Movement(row["date"], "interest", int(row["amount"])))

    for row in con.execute(
        """SELECT date, amount FROM loan_payments
            WHERE loan_id = ? AND date <= ?""",
        (loan_id, upto),
    ):
        movements.append(calc.Movement(row["date"], "payment", int(row["amount"])))

    return movements


def loan_balance(
    con: sqlite3.Connection, loan_id: int, upto: str
) -> calc.LoanBalance:
    """A loan's balance at a date, with interest-first application."""
    row = con.execute(
        "SELECT principal FROM loans WHERE id = ?", (loan_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"there is no loan {loan_id}")
    return calc.loan_balance(
        int(row["principal"]), loan_movements(con, loan_id, upto)
    )


def debt_by_member(con: sqlite3.Connection, upto: str) -> dict[int, int]:
    """How much each member owes THE FUND (personal loans are not counted here).

    Live loans only: a voided loan is not a debt.
    """
    debts: dict[int, int] = {}
    for row in con.execute(
        """SELECT id, member_id FROM loans
            WHERE lender_id = ? AND status != 'voided'""",
        (FUND_ID,),
    ):
        balance = loan_balance(con, int(row["id"]), upto)
        if balance.total:
            member = int(row["member_id"])
            debts[member] = debts.get(member, 0) + balance.total
    return debts


# --------------------------------------------------------------------------
# Raffles
# --------------------------------------------------------------------------


def raffle_income(con: sqlite3.Connection, upto: str) -> int:
    """Income from PAID tickets, at the price frozen on each ticket.

    Settings are no longer consulted: the price was written into the row at the
    moment of sale, so no price change can retroactively revalue what was
    already sold.
    """
    row = con.execute(
        """SELECT COALESCE(SUM(price), 0) FROM raffle_tickets
            WHERE paid = 1 AND paid_on <= ?""",
        (upto,),
    ).fetchone()
    return int(row[0])


def raffle_debt_by_member(con: sqlite3.Connection, upto: str) -> dict[int, int]:
    """UNPAID tickets per member. The raffles are mandatory, so this is debt.

    Also at the frozen price, for the same reason: an old debt cannot be made
    cheaper — or free — by changing a setting.
    """
    return {
        int(r["member_id"]): int(r["debt"])
        for r in con.execute(
            """SELECT member_id, SUM(price) AS debt FROM raffle_tickets
                WHERE paid = 0
                  AND printf('%04d-%02d-01', year, month) <= ?
                GROUP BY member_id""",
            (upto,),
        )
    }


# --------------------------------------------------------------------------
# The fund
# --------------------------------------------------------------------------


def fund(con: sqlite3.Connection, upto: str) -> calc.Fund:
    """The state of the common fund at a date. Fund money only."""

    def one(sql: str, params: tuple = ()) -> int:
        row = con.execute(sql, params).fetchone()
        return int(row[0] or 0)

    savings = one(
        "SELECT COALESCE(SUM(amount),0) FROM savings WHERE date <= ?", (upto,)
    )

    capital_out = one(
        """SELECT COALESCE(SUM(principal),0) FROM loans
            WHERE lender_id = ? AND status != 'voided' AND started_on <= ?""",
        (FUND_ID, upto),
    )

    payments_in = one(
        """SELECT COALESCE(SUM(p.amount),0) FROM loan_payments p
             JOIN loans l ON l.id = p.loan_id
            WHERE l.lender_id = ? AND l.status != 'voided' AND p.date <= ?""",
        (FUND_ID, upto),
    )

    interest_charged = one(
        """SELECT COALESCE(SUM(i.interest_amount),0) FROM loan_interest i
             JOIN loans l ON l.id = i.loan_id
            WHERE l.lender_id = ? AND l.status != 'voided' AND i.date <= ?""",
        (FUND_ID, upto),
    )

    prizes = one(
        "SELECT COALESCE(SUM(prize),0) FROM draws WHERE date <= ?", (upto,)
    )

    # Interest actually collected and capital actually recovered: derived from
    # the interest-first application loan by loan, not from a raw sum.
    interest_collected = 0
    capital_recovered = 0
    for row in con.execute(
        """SELECT id FROM loans
            WHERE lender_id = ? AND status != 'voided' AND started_on <= ?""",
        (FUND_ID, upto),
    ):
        balance = loan_balance(con, int(row["id"]), upto)
        interest_collected += balance.interest_paid
        capital_recovered += balance.capital_paid

    return calc.Fund(
        savings=savings,
        capital_out=capital_out,
        capital_recovered=capital_recovered,
        payments_in=payments_in,
        raffle_income=raffle_income(con, upto),
        prizes_paid=prizes,
        interest_charged=interest_charged,
        interest_collected=interest_collected,
    )


def personal_interest(con: sqlite3.Connection, upto: str) -> dict[int, int]:
    """Interest COLLECTED by each personal lender. This is not the fund's.

    It exists so the figure can be reported separately — never so it can be
    added to the payout.
    """
    by_lender: dict[int, int] = {}
    for row in con.execute(
        """SELECT id, lender_id FROM loans
            WHERE lender_id != ? AND status != 'voided' AND started_on <= ?""",
        (FUND_ID, upto),
    ):
        balance = loan_balance(con, int(row["id"]), upto)
        lender = int(row["lender_id"])
        by_lender[lender] = by_lender.get(lender, 0) + balance.interest_paid
    return by_lender


# --------------------------------------------------------------------------
# Year-end payout
# --------------------------------------------------------------------------


def payout(con: sqlite3.Connection, upto: str) -> calc.Payout:
    """A simulation of the year-end payout. Moves nothing.

    Each member gets their savings back plus an EQUAL share of the profits,
    minus what they owe on loans and on unpaid raffle tickets.
    """
    f = fund(con, upto)
    loan_debts = debt_by_member(con, upto)
    ticket_debts = raffle_debt_by_member(con, upto)

    members: list[tuple[int, str, int, int, int]] = []
    for row in con.execute(
        """SELECT m.id, m.name,
                  COALESCE((SELECT SUM(s.amount) FROM savings s
                             WHERE s.member_id = m.id AND s.date <= ?), 0) AS saved
             FROM members m
            WHERE m.joined_on <= ?
            ORDER BY m.id""",
        (upto, upto),
    ):
        member_id = int(row["id"])
        members.append(
            (
                member_id,
                row["name"],
                int(row["saved"]),
                loan_debts.get(member_id, 0),
                ticket_debts.get(member_id, 0),
            )
        )

    # The cycle rotates who receives the leftover dollar from the division, so
    # it does not always land on the administrator (member 1).
    cycle = setting_int(con, "cycle_year", upto, 0)

    return calc.compute_payout(members, f.profits, f.cash, rotation_seed=cycle)


def payout_balance_check(con: sqlite3.Connection, upto: str) -> calc.BalanceCheck:
    """Re-adds the payout against independent sums from the database.

    Unlike `Payout.balances` (which only looks inside the object), this sums the
    savings and profits by its own route and compares. If it fails, money was
    invented or lost somewhere.
    """
    p = payout(con, upto)

    savings_independent = int(
        con.execute(
            """SELECT COALESCE(SUM(s.amount), 0) FROM savings s
                 JOIN members m ON m.id = s.member_id
                WHERE s.date <= ? AND m.joined_on <= ?""",
            (upto, upto),
        ).fetchone()[0]
    )

    # Interest collected is re-derived by its OWN route: cash actually received
    # on fund loans, minus the capital portion of it. Taking f.interest_collected
    # here would have made the check circular — it would be verifying a number
    # against itself, which is how a backdated interest charge could invent
    # $250 of distributable profit and still report "everything balances".
    interest_independent = 0
    for row in con.execute(
        """SELECT id FROM loans
            WHERE lender_id = ? AND status != 'voided' AND started_on <= ?""",
        (FUND_ID, upto),
    ):
        balance = loan_balance(con, int(row["id"]), upto)
        paid_in = int(
            con.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM loan_payments
                    WHERE loan_id = ? AND date <= ?""",
                (int(row["id"]), upto),
            ).fetchone()[0]
        )
        # Every dollar received is either interest or capital; nothing else.
        interest_independent += paid_in - balance.capital_paid

    f = fund(con, upto)
    profits_independent = interest_independent + (f.raffle_income - f.prizes_paid)

    return calc.check_balance(p, savings_independent, profits_independent)
