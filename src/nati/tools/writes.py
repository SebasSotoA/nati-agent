"""Four of the five day-to-day write tools.

The fifth, `mark_ticket_paid`, sits in `writes_rare.py` — the wrong home, since
it is daily and single-phase like these. Left there for now rather than moved on
the eve of a run; noted so the next reader is not misled by the filenames.

What holds this layer up:

- **The model does no arithmetic.** Interest is computed by `calc`, never passed
  in as an argument. Splitting a payment between interest and capital is computed
  by `calc`.
- **`confirm_name` on anything carrying a `member_id`.** The world has real name
  traps, and a wrong id records money against the wrong person. With the name, a
  one-digit slip is no longer enough: it would take an internally consistent
  forgery.
- **Invariants AFTER writing.** A loan's balance depends on the full
  chronological order of its movements, so "does it fit" can only be answered
  against state already written. That is what `db`'s `verify` hook is for.
"""

from __future__ import annotations

import calendar
import sqlite3
from typing import Any

from nati import clock
from nati.tools import calc, gates
from nati.tools.envelope import Figures, ok, refuse, usd
from nati.world import db
from nati.world import queries as q

FUND_ID = q.FUND_ID


def _confirmed_member(
    con: sqlite3.Connection, member_id: object, confirm_name: object
) -> sqlite3.Row:
    """Resolves the member and requires the name to match.

    The id alone is not enough. "Amparo" is two people in this world and
    "Liliana" is two more; and the phone numbers 3101234567 and 3110123456 differ
    by a single transposition. Asking for the name turns a one-digit slip into a
    coherent forgery, which is far harder to do by accident.
    """
    mid = gates.integer(member_id, "member")
    row = con.execute(
        "SELECT id, name, joined_on FROM members WHERE id = ?", (mid,)
    ).fetchone()
    if row is None:
        raise gates.Refusal("unknown_member", "I have no record of that person.")

    expected = gates.normalize_name(row["name"])
    given = gates.normalize_name(gates.text(confirm_name, "name"))
    # A partial name is accepted ("Carlos Ramirez" for "Carlos Ramirez"), but it
    # has to belong to that person.
    words = given.split()
    if not words or not all(w in expected for w in words):
        raise gates.Refusal(
            "name_mismatch",
            "That name does not match the person you gave me. Confirm who it is.",
        )

    # The name must identify ONE person, not merely be consistent with the id.
    # "Amparo" satisfies both Amparo Henao and Consuelo Amparo Jaramillo, so
    # accepting it confirmed nothing: whichever id was typed simply won, which is
    # the mistake the confirmation exists to catch. Given names are shared in
    # this group — Amparo twice, José twice, Liliana twice.
    matches = [
        r["id"] for r in con.execute("SELECT id, name FROM members")
        if all(w in gates.normalize_name(r["name"]) for w in words)
    ]
    if len(matches) > 1:
        raise gates.Refusal(
            "ambiguous_name",
            f"More than one member's name matches “{given}”, so I cannot be "
            "sure who this is. Give me the full name.",
        )
    return row


def _movement_date(con: sqlite3.Connection, when: object) -> str:
    """The date of a money movement: today by default, inside the cycle."""
    if when in (None, ""):
        return clock.today_iso()
    first, last = q.cycle_months(con)
    return gates.date_in_cycle(
        when, q.cycle_year(con), first_month=first, last_month=last
    )


def _cycle_month(con: sqlite3.Connection, value: object) -> int:
    first, last = q.cycle_months(con)
    return gates.cycle_month(value, first_month=first, last_month=last)


def _not_before_last_movement(
    con: sqlite3.Connection, loan_id: int, when: str
) -> None:
    """A loan's book only grows forward.

    This closes an entire class of attack. Because the interest-first split is
    applied in chronological order at read time, inserting an interest charge
    with an earlier date converts capital ALREADY received into "interest paid" —
    and on a personally-funded loan that becomes the administrator's personal
    income, without touching any protected column.
    """
    latest = con.execute(
        """SELECT MAX(d) FROM (
             SELECT MAX(date) AS d FROM loan_payments WHERE loan_id = ?
             UNION ALL
             SELECT MAX(date) AS d FROM loan_interest WHERE loan_id = ?
           )""",
        (loan_id, loan_id),
    ).fetchone()[0]
    if latest and when < latest:
        raise gates.Refusal(
            "backdated_movement",
            "I cannot record a movement dated before the last one that loan "
            "already has. If something is wrong, it gets corrected with a new "
            "entry, not by changing the past.",
        )


# --------------------------------------------------------------------------
# record_saving
# --------------------------------------------------------------------------


def record_saving(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    member_id: object,
    confirm_name: object,
    amount: object,
    when: object = None,
) -> dict[str, Any]:
    """Records a savings contribution. No fixed quota: each saves what they can."""
    member = _confirmed_member(con, member_id, confirm_name)
    value = gates.amount(amount, "saving")
    date_used = _movement_date(con, when)

    if date_used < member["joined_on"]:
        return refuse(
            "before_joining",
            f"{member['name']} joined the natillera on {member['joined_on']}, so I "
            "cannot record a saving dated earlier than that.",
        )

    result = db.insert(
        con, "savings",
        {"member_id": int(member["id"]), "date": date_used, "amount": value},
        ctx,
    )

    total = int(
        con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM savings WHERE member_id = ?",
            (int(member["id"]),),
        ).fetchone()[0]
    )
    figures = Figures()
    return ok(
        f"Done: I recorded {usd(value)} of savings for {member['name']} dated "
        f"{date_used}. They now have {usd(total)} saved.",
        saving_id=result["id"],
        audit_id=result["audit_id"],
        member_id=int(member["id"]),
        member=member["name"],
        date=figures.date("date", date_used),
        amount=figures.money("amount", value),
        total_saved=figures.money("total_saved", total),
        world_hash=db.world_hash(con),
        figures=figures,
    )


# --------------------------------------------------------------------------
# record_payment
# --------------------------------------------------------------------------


def record_payment(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    loan_id: object,
    amount: object,
    when: object = None,
) -> dict[str, Any]:
    """Records a payment against a loan. Interest first, then capital.

    It does not take a `member_id`: that is derived from the loan. If it did, a
    payment from one person could reduce someone else's debt — and both of them
    would be wrong at year-end.
    """
    lid = gates.integer(loan_id, "loan")
    value = gates.amount(amount, "payment")

    loan = con.execute(
        """SELECT l.*, m.name, le.name AS lender, le.kind AS lender_kind
             FROM loans l
             JOIN members m ON m.id = l.member_id
             JOIN lenders le ON le.id = l.lender_id
            WHERE l.id = ?""",
        (lid,),
    ).fetchone()
    if loan is None:
        return refuse("unknown_loan", "I have no record of that loan.")
    if loan["status"] == "voided":
        return refuse(
            "loan_voided", "That loan has been voided, so I cannot pay anything on it."
        )

    date_used = _movement_date(con, when)
    if date_used < loan["started_on"]:
        return refuse(
            "payment_before_loan",
            f"That loan started on {loan['started_on']}, so I cannot record an "
            "earlier payment on it.",
        )
    _not_before_last_movement(con, lid, date_used)

    before = q.loan_balance(con, lid, date_used)
    if before.total == 0:
        return refuse(
            "nothing_owed",
            f"{loan['name']}'s loan is already settled: there is nothing left to pay.",
        )

    def no_overpayment(c: sqlite3.Connection, _pk: int) -> None:
        """POST-write invariant: the loan must not end up overpaid.

        Checked afterwards because the balance depends on the full chronological
        order; a `amount <= balance` pre-check is wrong as soon as dates can vary.
        """
        after = q.loan_balance(c, lid, clock.today_iso())
        if after.overpaid > 0:
            raise db.InvariantBroken(
                "That payment is more than what is left to pay on that loan."
            )

    result = db.insert(
        con, "loan_payments",
        {"loan_id": lid, "member_id": int(loan["member_id"]),
         "date": date_used, "amount": value},
        ctx, verify=no_overpayment,
    )

    after = q.loan_balance(con, lid, clock.today_iso())
    split = after.splits[-1] if after.splits else None

    # Closing is a CONSEQUENCE of reaching zero, not a decision anyone can ask
    # for. That is why there is no `close_loan` tool.
    closed_now = False
    if after.total == 0 and loan["status"] == "active":
        db.update(con, "loans", lid, {"status": "closed"}, ctx)
        closed_now = True

    figures = Figures()
    to_interest = split.to_interest if split else 0
    to_capital = split.to_capital if split else 0

    warnings = []
    if int(loan["lender_id"]) != FUND_ID:
        warnings.append(
            f"This payment is on a loan {loan['lender']} funded with their own "
            "money: it does not go into the fund's cash."
        )
    if closed_now:
        warnings.append("With this payment the loan is settled and has been closed.")

    return ok(
        f"I recorded {usd(value)} from {loan['name']}: {usd(to_interest)} covered "
        f"interest and {usd(to_capital)} reduced the capital. "
        + (
            "That settles it."
            if after.total == 0
            else f"They still owe {usd(after.total)}."
        ),
        payment_id=result["id"],
        audit_id=result["audit_id"],
        loan_id=lid,
        member=loan["name"],
        money_from="fund" if int(loan["lender_id"]) == FUND_ID else "personal",
        date=figures.date("date", date_used),
        paid=figures.money("paid", value),
        to_interest=figures.money("to_interest", to_interest),
        to_capital=figures.money("to_capital", to_capital),
        balance_before=figures.money("balance_before", before.total),
        balance_after=figures.money("balance_after", after.total),
        loan_closed=closed_now,
        derivation={
            "rule": "a payment covers overdue interest first, the rest reduces capital",
            "interest_overdue_before": before.interest_outstanding,
        },
        world_hash=db.world_hash(con),
        figures=figures,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# accrue_monthly_interest
# --------------------------------------------------------------------------


def accrue_monthly_interest(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    month: object,
    loan_id: object = None,
) -> dict[str, Any]:
    """Charges a month's interest. The amount is computed, never supplied.

    With no `loan_id` it covers ALL active FUND loans — but never the
    personally-funded ones. Those require an explicit call, so that every dollar
    of personal interest is individually justified and individually audited,
    rather than slipping in under a general "charge July's interest".
    """
    m = _cycle_month(con, month)
    year = q.cycle_year(con)
    if (year, m) > (clock.today().year, clock.today().month):
        return refuse(
            "future_month", f"I cannot charge interest for month {m} yet."
        )

    month_end = calc.to_date(
        f"{year:04d}-{m:02d}-{calendar.monthrange(year, m)[1]:02d}"
    )
    date_used = min(month_end, clock.today()).isoformat()

    if loan_id not in (None, ""):
        lid = gates.integer(loan_id, "loan")
        if con.execute("SELECT id FROM loans WHERE id = ?", (lid,)).fetchone() is None:
            return refuse("unknown_loan", "I have no record of that loan.")
        targets = [lid]
        fund_only = False
    else:
        targets = [
            int(r["id"]) for r in con.execute(
                """SELECT id FROM loans
                    WHERE status = 'active' AND lender_id = ?
                      AND started_on <= ? ORDER BY id""",
                (FUND_ID, date_used),
            )
        ]
        fund_only = True

    charged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    figures = Figures()
    total = 0

    # The whole batch in ONE transaction: if one fails, nothing is left half-done.
    try:
        with db.transaction(con):
            for lid in targets:
                loan = con.execute(
                    """SELECT l.*, m.name, le.kind AS lender_kind
                         FROM loans l
                         JOIN members m ON m.id = l.member_id
                         JOIN lenders le ON le.id = l.lender_id
                        WHERE l.id = ?""",
                    (lid,),
                ).fetchone()

                if loan["status"] != "active":
                    skipped.append({"loan_id": lid, "why": f"it is {loan['status']}"})
                    continue
                if date_used < loan["started_on"]:
                    skipped.append(
                        {"loan_id": lid, "why": "it started after that month"}
                    )
                    continue
                already = con.execute(
                    """SELECT 1 FROM loan_interest
                        WHERE loan_id = ? AND substr(date,1,7) = ?""",
                    (lid, f"{year:04d}-{m:02d}"),
                ).fetchone()
                if already:
                    skipped.append(
                        {"loan_id": lid, "why": "it already has that month's interest"}
                    )
                    continue

                # The loan's book only grows forward. Charging a month whose date
                # falls before a payment the borrower has already made would
                # retroactively turn repaid capital into "interest paid" — which
                # on a personal loan becomes the administrator's own income, and
                # on a fund loan invents profit that no cash backs. The database
                # refuses this too; this check is here so the answer is a clean
                # skip with a reason rather than an aborted batch.
                last_payment = con.execute(
                    "SELECT MAX(date) FROM loan_payments WHERE loan_id = ?", (lid,)
                ).fetchone()[0]
                if last_payment and date_used < last_payment:
                    skipped.append({
                        "loan_id": lid,
                        "why": "that month is earlier than a payment already "
                               "recorded on it, and the book only moves forward",
                    })
                    continue

                balance = q.loan_balance(con, lid, date_used)
                if balance.capital_outstanding == 0:
                    skipped.append({"loan_id": lid, "why": "no capital left owing"})
                    continue

                # HERE is the rule: on the ORIGINAL amount, not the balance.
                interest = calc.percent(
                    int(loan["principal"]), int(loan["monthly_rate"])
                )
                if interest == 0:
                    # A zero row would occupy that month's slot forever, so if the
                    # rate were later corrected the real interest could never be
                    # charged.
                    skipped.append({"loan_id": lid, "why": "the rate is zero"})
                    continue

                result = db.insert(
                    con, "loan_interest",
                    {"loan_id": lid, "date": date_used, "interest_amount": interest},
                    ctx,
                )
                total += interest
                charged.append({
                    "loan_id": lid,
                    "member": loan["name"],
                    "money_from": (
                        "fund" if int(loan["lender_id"]) == FUND_ID else "personal"
                    ),
                    "interest": figures.money(f"interest_{lid}", interest),
                    "audit_id": result["audit_id"],
                    "derivation": {
                        "base": int(loan["principal"]),
                        "rate": int(loan["monthly_rate"]),
                        "rule": "interest on the original amount",
                    },
                })
    except gates.Refusal:
        raise
    except db.WorldError as exc:
        return refuse("batch_failed", str(exc))

    if not charged:
        reasons = "; ".join(f"loan {s['loan_id']}: {s['why']}" for s in skipped)
        return ok(
            f"I charged no interest for month {m}."
            + (f" Reasons: {reasons}." if reasons else ""),
            month=m, year=year, charged=[], skipped=skipped, total=0,
        )

    warnings = []
    if fund_only:
        warnings.append(
            "I only charged interest on the fund's loans. Personally-funded ones "
            "are charged one at a time."
        )

    return ok(
        f"I charged month {m}'s interest on {len(charged)} loan(s), "
        f"{usd(total)} in total.",
        month=m, year=year, date=date_used,
        charged=charged, skipped=skipped,
        total=figures.money("total_interest", total),
        world_hash=db.world_hash(con),
        figures=figures, warnings=warnings,
    )


# --------------------------------------------------------------------------
# sell_raffle_ticket
# --------------------------------------------------------------------------


def sell_raffle_ticket(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    member_id: object,
    confirm_name: object,
    kind: object,
    month: object,
    number_1: object,
    number_2: object = None,
    paid: object = True,
) -> dict[str, Any]:
    """Sells a raffle ticket. Numbers are not repeated within a raffle.

    The price is FROZEN into the row, so no future settings change can revalue a
    ticket already sold or make an old debt cheaper.
    """
    member = _confirmed_member(con, member_id, confirm_name)
    which = gates.choice(kind, "raffle kind", ("monthly", "quarterly"))
    m = _cycle_month(con, month)
    year = q.cycle_year(con)
    n1 = gates.raffle_number(number_1, "first number")

    if which == "monthly":
        if number_2 in (None, ""):
            return refuse(
                "missing_number",
                "The monthly raffle takes two numbers. The second one is missing.",
            )
        n2: int | None = gates.raffle_number(number_2, "second number")
        if n1 == n2:
            return refuse(
                "duplicate_numbers",
                "The two numbers on a ticket have to be different.",
            )
    else:
        if number_2 not in (None, ""):
            return refuse(
                "extra_number", "The quarterly raffle takes a single number."
            )
        n2 = None

    is_paid = gates.boolean(paid, "whether it is paid")
    price = q.ticket_price(con, which, clock.today_iso())
    if price <= 0:
        return refuse(
            "no_price",
            "I do not have a price configured for that raffle, so I cannot sell it "
            "yet.",
        )

    values: dict[str, Any] = {
        "member_id": int(member["id"]), "kind": which, "month": m, "year": year,
        "paid": 1 if is_paid else 0,
        "paid_on": clock.today_iso() if is_paid else None,
        "price": price, "number_1": n1, "number_2": n2,
    }
    result = db.insert(con, "raffle_tickets", values, ctx)

    numbers = ", ".join(f"{n:02d}" for n in (n1, n2) if n is not None)
    label = "numbers" if n2 is not None else "number"
    figures = Figures()
    return ok(
        f"I sold {member['name']} the {which} ticket for month {m} with "
        f"{label} {numbers}, for {usd(price)}"
        + ("." if is_paid else ", and they still owe it."),
        ticket_id=result["id"], audit_id=result["audit_id"],
        member_id=int(member["id"]), member=member["name"],
        kind=which, month=m, year=year,
        numbers=[n for n in (n1, n2) if n is not None],
        paid=is_paid,
        price=figures.money("price", price),
        world_hash=db.world_hash(con),
        figures=figures,
        warnings=(
            [] if is_paid else
            ["Since they did not pay it, that amount is a debt against their payout."]
        ),
    )
