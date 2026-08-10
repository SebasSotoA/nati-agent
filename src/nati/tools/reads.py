"""The 8 read tools.

The governing principle here: **every question the administrator asks is answered
by ONE result**. The moment the model has to combine two results to answer, it
starts doing arithmetic — and that collapses the whole premise that the maths
lives in `calc.py`.

That is why `member_status` carries a person's complete position, and why
`simulate_payout` includes the fund block even though `fund_status` exists.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from nati import clock
from nati.tools import gates
from nati.tools.envelope import Figures, ok, refuse, usd
from nati.world import queries as q

FUND_ID = q.FUND_ID


def _cutoff(con: sqlite3.Connection, when: object) -> str:
    """A query's cut-off date. Today by default; never in the future."""
    if when in (None, ""):
        return clock.today_iso()
    return gates.date_not_future(when, "cut-off date")


def _member_row(con: sqlite3.Connection, member_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, name, phone, joined_on FROM members WHERE id = ?", (member_id,)
    ).fetchone()


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------


def members(con: sqlite3.Connection, search: object = None) -> dict[str, Any]:
    """Lists members, or searches by name. NEVER guesses when it is ambiguous.

    The world contains real name traps: "Amparo" is two people (Amparo Henao and
    Consuelo Amparo Jaramillo), and "Liliana" is two more. If this returned a
    single id for an ambiguous name, money would be recorded against the wrong
    person. It returns `ambiguous: true` and lets the administrator decide.
    """
    rows = con.execute(
        """SELECT m.id, m.name, m.joined_on,
                  COALESCE((SELECT SUM(s.amount) FROM savings s
                             WHERE s.member_id = m.id), 0) AS saved
             FROM members m ORDER BY m.id"""
    ).fetchall()

    if search not in (None, ""):
        needle = gates.normalize_name(gates.text(search, "name to search for"))
        rows = [r for r in rows if needle in gates.normalize_name(r["name"])]

    figures = Figures()
    listed = [
        {
            "member_id": r["id"],
            "name": r["name"],
            "saved": figures.money(f"saved_{r['id']}", int(r["saved"])),
        }
        for r in rows
    ]

    if not listed:
        return ok(
            "I could not find any member with that name.",
            members=[], count=0, ambiguous=False,
        )
    if search not in (None, "") and len(listed) > 1:
        names = ", ".join(m["name"] for m in listed)
        return ok(
            f"{len(listed)} people match that: {names}. I need you to tell me "
            "which one.",
            members=listed, count=len(listed), ambiguous=True, figures=figures,
        )

    message = (
        f"{listed[0]['name']} has {usd(listed[0]['saved'])} saved."
        if len(listed) == 1
        else f"The natillera has {len(listed)} members."
    )
    return ok(message, members=listed, count=len(listed), ambiguous=False,
              figures=figures)


# --------------------------------------------------------------------------
# member_status
# --------------------------------------------------------------------------


def member_status(
    con: sqlite3.Connection, member_id: object, when: object = None
) -> dict[str, Any]:
    """One person's COMPLETE position, in a single result.

    It includes debt to the fund and, separately and explicitly labelled, debt on
    the administrator's own money. They are kept apart because `debt_by_member`
    deliberately counts only the fund's: if the personal figure were not stated
    on its own, Nati would tell Julian he owes nothing when he owes $696.
    """
    mid = gates.integer(member_id, "member")
    upto = _cutoff(con, when)
    person = _member_row(con, mid)
    if person is None:
        return refuse("unknown_member", "I have no record of that person.")

    figures = Figures()
    saved = int(
        con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM savings WHERE member_id=? AND date<=?",
            (mid, upto),
        ).fetchone()[0]
    )

    loans: list[dict[str, Any]] = []
    owes_fund = owes_personal = 0
    for row in con.execute(
        """SELECT l.id, l.principal, l.monthly_rate, l.started_on, l.status,
                  l.lender_id, le.name AS lender
             FROM loans l JOIN lenders le ON le.id = l.lender_id
            WHERE l.member_id = ? AND l.started_on <= ? AND l.status != 'voided'
            ORDER BY l.id""",
        (mid, upto),
    ):
        lid = int(row["id"])
        balance = q.loan_balance(con, lid, upto)
        from_fund = int(row["lender_id"]) == FUND_ID
        if from_fund:
            owes_fund += balance.total
        else:
            owes_personal += balance.total
        loans.append({
            "loan_id": lid,
            "money_from": "fund" if from_fund else "personal",
            "lender": row["lender"],
            "principal": figures.money(f"loan_{lid}_principal", int(row["principal"])),
            "monthly_rate": figures.number(f"loan_{lid}_rate", int(row["monthly_rate"])),
            "balance": figures.money(f"loan_{lid}_balance", balance.total),
            "capital_outstanding": figures.money(
                f"loan_{lid}_capital", balance.capital_outstanding
            ),
            "interest_outstanding": figures.money(
                f"loan_{lid}_interest", balance.interest_outstanding
            ),
            "status": row["status"],
        })

    unpaid = con.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(price),0) AS debt
             FROM raffle_tickets WHERE member_id = ? AND paid = 0
               AND printf('%04d-%02d-01', year, month) <= ?""",
        (mid, upto),
    ).fetchone()

    warnings: list[str] = []
    if owes_personal:
        # Personal money is always the administrator's — she is the only private
        # lender — so this is unambiguous. The point of the warning is the one
        # that always mattered: this debt is NOT owed to the common fund.
        warnings.append(
            f"Of that debt, {usd(owes_personal)} is owed to the administrator "
            "personally, on her own money — not to the common fund."
        )

    return ok(
        f"{person['name']} has {usd(saved)} saved"
        + (
            f" and owes the fund {usd(owes_fund)}."
            if owes_fund
            else " and owes the fund nothing."
        ),
        member_id=mid,
        name=person["name"],
        joined_on=person["joined_on"],
        cutoff_date=upto,
        saved=figures.money("saved", saved),
        fund_debt=figures.money("fund_debt", owes_fund),
        personal_debt_not_part_of_the_fund=figures.money(
            "personal_debt", owes_personal
        ),
        loans=loans,
        unpaid_tickets=figures.number("unpaid_tickets", int(unpaid["n"])),
        raffle_debt=figures.money("raffle_debt", int(unpaid["debt"])),
        figures=figures,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# fund_status
# --------------------------------------------------------------------------


def fund_status(con: sqlite3.Connection, when: object = None) -> dict[str, Any]:
    """The common fund. Fund money only; personal money is reported separately."""
    upto = _cutoff(con, when)
    f = q.fund(con, upto)
    personal = q.personal_interest(con, upto)
    figures = Figures()

    outside: list[dict[str, Any]] = []
    for lender_id, collected in sorted(personal.items()):
        row = con.execute(
            "SELECT name FROM lenders WHERE id = ?", (lender_id,)
        ).fetchone()
        outside.append({
            "lender": row["name"] if row else f"#{lender_id}",
            "interest_collected": figures.money(
                f"personal_interest_{lender_id}", collected
            ),
        })

    # The cycle's calendar, so the model REPORTS it instead of working it out.
    # Without this it had to infer "the next cycle starts in 2027" by adding one
    # to the year — model-side arithmetic, which the output filter refuses (and
    # it also invented a wrong reason for December being excluded). A fact the
    # model needs and no tool returned is a gap in the tools, not in the filter.
    year = q.cycle_year(con)
    figures.number("cycle_year", year)
    figures.number("next_cycle_year", year + 1)
    figures.date("next_cycle_starts", f"{year + 1:04d}-01-01")
    figures.date("cycle_ends", f"{year:04d}-11-30")

    # Which months each raffle has actually run. It lives here, on the tool that
    # needs no arguments, because `raffle_status` requires a month — so a model
    # asked "when do the quarterly raffles happen?" could not look it up without
    # already knowing the answer, and filled the gap by inventing "3, 6, 9, 11".
    raffle_months: dict[str, list[int]] = {}
    for kind in ("monthly", "quarterly"):
        raffle_months[kind] = [
            int(r["month"]) for r in con.execute(
                """SELECT DISTINCT month FROM raffle_tickets
                    WHERE kind = ? AND year = ? ORDER BY month""",
                (kind, year),
            )
        ]
        for month_number in raffle_months[kind]:
            figures.number(f"{kind}_month_{month_number}", month_number)

    return ok(
        f"As of {upto} the fund has {usd(f.cash)} in cash and "
        f"{usd(f.loans_outstanding)} out on loan.",
        cutoff_date=upto,
        raffles={
            "months_run": raffle_months,
            "note": "These are the months each raffle has tickets for in this "
                    "cycle. Do not assume any other month has one.",
        },
        cycle={
            "year": year,
            "runs": f"January to November {year}",
            "ends": f"{year:04d}-11-30",
            "next_cycle_year": year + 1,
            "next_cycle_starts": f"{year + 1:04d}-01-01",
            "why_december_is_excluded":
                "the savings exist to be spent in December, so the cycle "
                "closes at the end of November and pays out before it",
        },
        cash=figures.money("cash", f.cash),
        loans_outstanding=figures.money("loans_outstanding", f.loans_outstanding),
        equity=figures.money("equity", f.equity),
        savings_received=figures.money("savings_received", f.savings),
        capital_lent=figures.money("capital_lent", f.capital_out),
        payments_received=figures.money("payments_received", f.payments_in),
        raffle_income=figures.money("raffle_income", f.raffle_income),
        prizes_paid=figures.money("prizes_paid", f.prizes_paid),
        interest_charged=figures.money("interest_charged", f.interest_charged),
        interest_collected=figures.money("interest_collected", f.interest_collected),
        raffle_profit=figures.money("raffle_profit", f.raffle_profit),
        distributable_profits=figures.money("profits", f.profits),
        monthly_ticket_price=figures.money(
            "monthly_ticket_price", q.setting_int(con, "monthly_ticket_price", upto, 0)
        ),
        monthly_prize=figures.money(
            "monthly_prize", q.setting_int(con, "monthly_prize", upto, 0)
        ),
        money_outside_the_fund=outside,
        figures=figures,
        warnings=[
            "Interest charged is not cash yet: it arrives when the person pays.",
        ],
    )


# --------------------------------------------------------------------------
# loan_status / list_loans
# --------------------------------------------------------------------------


def loan_status(
    con: sqlite3.Connection, loan_id: object, when: object = None
) -> dict[str, Any]:
    """One loan, with the detail of how each payment was applied."""
    lid = gates.integer(loan_id, "loan")
    upto = _cutoff(con, when)
    row = con.execute(
        """SELECT l.*, m.name, le.name AS lender, le.kind AS lender_kind
             FROM loans l
             JOIN members m ON m.id = l.member_id
             JOIN lenders le ON le.id = l.lender_id
            WHERE l.id = ?""",
        (lid,),
    ).fetchone()
    if row is None:
        return refuse("unknown_loan", "I have no record of that loan.")

    balance = q.loan_balance(con, lid, upto)
    figures = Figures()
    from_fund = int(row["lender_id"]) == FUND_ID

    applications = [
        {
            "date": s.date,
            "paid": figures.money(f"payment_{i}", s.amount),
            "to_interest": figures.money(f"payment_{i}_interest", s.to_interest),
            "to_capital": figures.money(f"payment_{i}_capital", s.to_capital),
        }
        for i, s in enumerate(balance.splits, 1)
    ]

    warnings = []
    if not from_fund:
        warnings.append(
            f"{row['lender']} funded this loan with their own money: its interest "
            "does not go into the fund or the payout."
        )

    return ok(
        f"{row['name']}'s loan owes {usd(balance.total)} — "
        f"{usd(balance.capital_outstanding)} of capital and "
        f"{usd(balance.interest_outstanding)} of overdue interest.",
        loan_id=lid,
        member=row["name"],
        member_id=int(row["member_id"]),
        money_from="fund" if from_fund else "personal",
        lender=row["lender"],
        status=row["status"],
        started_on=row["started_on"],
        cutoff_date=upto,
        principal=figures.money("principal", int(row["principal"])),
        monthly_rate=figures.number("monthly_rate", int(row["monthly_rate"])),
        balance=figures.money("balance", balance.total),
        capital_outstanding=figures.money(
            "capital_outstanding", balance.capital_outstanding
        ),
        interest_outstanding=figures.money(
            "interest_outstanding", balance.interest_outstanding
        ),
        interest_charged=figures.money("interest_charged", balance.interest_charged),
        interest_paid=figures.money("interest_paid", balance.interest_paid),
        capital_paid=figures.money("capital_paid", balance.capital_paid),
        # So the checker can reconstruct the arithmetic without trusting the
        # model's prose.
        derivation={
            "base": int(row["principal"]),
            "rate": int(row["monthly_rate"]),
            "interest_per_month": (
                int(row["principal"]) * int(row["monthly_rate"]) + 50
            ) // 100,
            "rule": "interest on the original amount; a payment covers interest first",
        },
        payment_applications=applications,
        figures=figures,
        warnings=warnings,
    )


def list_loans(
    con: sqlite3.Connection, status: object = None, money_from: object = None
) -> dict[str, Any]:
    """The loan book: who owes money. Filterable by status and by source."""
    where, params = ["1=1"], []
    if status not in (None, ""):
        where.append("l.status = ?")
        params.append(gates.choice(status, "status", ("active", "closed", "voided")))
    if money_from not in (None, ""):
        chosen = gates.choice(money_from, "source", ("fund", "personal"))
        where.append("l.lender_id = 0" if chosen == "fund" else "l.lender_id != 0")

    upto = clock.today_iso()
    figures = Figures()
    listed, total = [], 0
    for row in con.execute(
        f"""SELECT l.id, l.member_id, l.lender_id, l.principal,
                   l.monthly_rate, l.status, m.name, le.name AS lender
              FROM loans l
              JOIN members m ON m.id = l.member_id
              JOIN lenders le ON le.id = l.lender_id
             WHERE {' AND '.join(where)}
             ORDER BY l.id""",  # noqa: S608
        tuple(params),
    ):
        lid = int(row["id"])
        balance = q.loan_balance(con, lid, upto)
        from_fund = int(row["lender_id"]) == FUND_ID
        if from_fund and row["status"] == "active":
            total += balance.total
        listed.append({
            "loan_id": lid,
            "member": row["name"],
            "member_id": int(row["member_id"]),
            "money_from": "fund" if from_fund else "personal",
            # WHOSE money — "personal" always means the administrator's own
            # money. Leaving the name out once cost a real answer: asked to split
            # the loan book by source, the model reported that Julian Marin's loan
            # came from "his own money". Julian is the BORROWER, not the lender.
            # The tool had said `personal` and nothing else, so the model filled
            # the gap with the only name in the row. Nothing downstream catches
            # that — the output filter judges figures, and this is a name — so the
            # funder is stated here rather than inferred.
            "lender": row["lender"],
            "status": row["status"],
            "principal": figures.money(f"l{lid}_principal", int(row["principal"])),
            "monthly_rate": figures.number(f"l{lid}_rate", int(row["monthly_rate"])),
            "balance": figures.money(f"l{lid}_balance", balance.total),
            # What that balance is MADE OF. Missing these cost a real reply: the
            # loan book gave interest charged ($120) and paid ($96) but not the
            # $24 still outstanding, so asked what Sebastian owed the model
            # subtracted to get the 24 — right — and then added it to the $470,
            # which already contained it, reaching $494. It would have had her
            # collect $24 too much. The filter caught it, because neither figure
            # came from a tool.
            #
            # This is the SECOND time this exact shape appeared here (see the
            # interest fields below, added for the same reason). The lesson is not
            # "the model does arithmetic"; it is that "the model never does
            # arithmetic" places an obligation on the TOOL: every figure a natural
            # answer needs must be handed over, or it will be derived.
            "capital_outstanding": figures.money(
                f"l{lid}_capital_outstanding", balance.capital_outstanding
            ),
            "interest_outstanding": figures.money(
                f"l{lid}_interest_outstanding", balance.interest_outstanding
            ),
            # Interest, split into what has been charged and what has actually
            # been collected. Without these the loan book could not answer "how
            # much interest has each person paid?" in one result — the model had
            # to either call a second tool per loan or work it out itself, and
            # working it out is exactly what it is not allowed to do. It ended up
            # correctly saying it did not have the figures, which is the right
            # answer to the wrong problem: the gap was in the tool.
            "interest_charged": figures.money(
                f"l{lid}_interest_charged", balance.interest_charged
            ),
            "interest_paid": figures.money(
                f"l{lid}_interest_paid", balance.interest_paid
            ),
            "capital_repaid": figures.money(
                f"l{lid}_capital_repaid", balance.capital_paid
            ),
        })

    if not listed:
        return ok("There are no loans matching that.", loans=[], count=0)

    # Totals ACROSS WHAT WAS RETURNED, not just across the fund. Filter the book
    # to personal loans and the only totals here used to be the fund's — which
    # are $0 by construction — so "what am I owed in total?" had no answer in the
    # result and the model added the four balances up itself. Its sum was right
    # that time. It cannot be relied on to be: the same model computed
    # $470 + $24 = $494 an hour earlier, which was wrong by exactly the $24 it
    # had already counted. The filter cannot tell those apart, and should not have
    # to — a total the answer obviously needs is the tool's job to provide.
    active = [l for l in listed if l["status"] == "active"]
    owed_total = sum(l["balance"] for l in active)
    interest_paid_total = sum(l["interest_paid"] for l in listed)
    interest_charged_total = sum(l["interest_charged"] for l in listed)
    interest_outstanding_total = sum(l["interest_outstanding"] for l in active)
    return ok(
        f"There are {len(listed)} loans, {len(active)} of them active and owing "
        f"{usd(owed_total)} in total. The fund is still owed {usd(total)}, and "
        f"{usd(interest_paid_total)} of interest has been paid across them.",
        loans=listed,
        count=len(listed),
        active_count=len(active),
        owed_total=figures.money("owed_total", owed_total),
        owed_to_the_fund=figures.money("owed_to_the_fund", total),
        interest_charged_total=figures.money(
            "interest_charged_total", interest_charged_total
        ),
        interest_outstanding_total=figures.money(
            "interest_outstanding_total", interest_outstanding_total
        ),
        interest_paid_total=figures.money(
            "interest_paid_total", interest_paid_total
        ),
        figures=figures,
    )


# --------------------------------------------------------------------------
# raffle_status
# --------------------------------------------------------------------------


def raffle_status(
    con: sqlite3.Connection, kind: object, month: object
) -> dict[str, Any]:
    """A raffle's tickets for a month, and its draw if it has happened.

    It shows everyone's numbers on purpose: the administrator needs to know who
    holds which number, both to sell tickets and to verify a winner.
    """
    which = gates.choice(kind, "raffle kind", ("monthly", "quarterly"))
    m = gates.cycle_month(month)
    year = q.cycle_year(con)
    figures = Figures()

    tickets = [
        {
            "ticket_id": int(r["id"]),
            "member_id": int(r["member_id"]),
            "member": r["name"],
            "numbers": [n for n in (r["number_1"], r["number_2"]) if n is not None],
            "paid": bool(r["paid"]),
            "price": figures.money(f"ticket_{r['id']}_price", int(r["price"])),
        }
        for r in con.execute(
            """SELECT t.*, m.name FROM raffle_tickets t
                 JOIN members m ON m.id = t.member_id
                WHERE t.kind = ? AND t.month = ? AND t.year = ?
                ORDER BY t.member_id""",
            (which, m, year),
        )
    ]

    draw = con.execute(
        "SELECT * FROM draws WHERE kind=? AND month=? AND year=?", (which, m, year)
    ).fetchone()

    sold = sum(1 for t in tickets if t["paid"])
    collected = sum(t["price"] for t in tickets if t["paid"])
    owing = [t["member"] for t in tickets if not t["paid"]]

    # The months this raffle actually runs in, read from the world rather than
    # left to the model. Asked when the quarterly raffles fall, Nati answered
    # "months 3, 6, 9, 11" from its own head — the world has 3 and 6. Small
    # numbers like that sail past the output filter (they are under the free
    # count limit), so a fabricated schedule is invisible there. A fact the model
    # needs and no tool returns is a gap in the tools.
    schedule = [
        int(r["month"]) for r in con.execute(
            """SELECT DISTINCT month FROM raffle_tickets
                WHERE kind = ? AND year = ? ORDER BY month""",
            (which, year),
        )
    ]
    for month_number in schedule:
        figures.number(f"{which}_month_{month_number}", month_number)

    data: dict[str, Any] = {
        "kind": which, "month": m, "year": year,
        "tickets": tickets,
        "tickets_paid": figures.number("tickets_paid", sold),
        "tickets_unpaid": figures.number("tickets_unpaid", len(owing)),
        "collected": figures.money("collected", collected),
        "who_owes": owing,
        "drawn": draw is not None,
        "months_this_raffle_has_run": schedule,
        "schedule_note": (
            f"The {which} raffle has tickets recorded for these months of the "
            f"{year} cycle. Do not assume any other month has one."
        ),
    }

    if draw is not None:
        from nati.tools import calc
        winners = [
            t["member"] for t in tickets
            if calc.ticket_wins(
                t["numbers"][0],
                t["numbers"][1] if len(t["numbers"]) > 1 else None,
                int(draw["winning_number"]),
            )
        ]
        data.update(
            winning_number=figures.number(
                "winning_number", int(draw["winning_number"])
            ),
            prize=figures.money("prize", int(draw["prize"])),
            draw_date=draw["date"],
            winners=winners,
        )
        outcome = (
            f"{draw['winning_number']:02d} came up and {', '.join(winners)} won"
            if winners
            else f"{draw['winning_number']:02d} came up, nobody won, so the prize "
                 "stayed in the natillera"
        )
        message = f"{which.capitalize()} raffle for month {m}: {outcome}."
    else:
        message = (
            f"{which.capitalize()} raffle for month {m}: {sold} tickets are paid "
            f"({usd(collected)}) and the draw has not happened yet."
        )

    return ok(message, figures=figures, **data)


# --------------------------------------------------------------------------
# simulate_payout
# --------------------------------------------------------------------------


def simulate_payout(con: sqlite3.Connection, when: object = None) -> dict[str, Any]:
    """A simulation of the year-end payout. It does NOT move a dollar.

    It also carries the fund block, so the model never has to join two results
    and start adding.
    """
    upto = _cutoff(con, when)
    p = q.payout(con, upto)
    f = q.fund(con, upto)
    balance = q.payout_balance_check(con, upto)
    figures = Figures()

    shares = [
        {
            "member_id": s.member_id,
            "member": s.name,
            "saved": figures.money(f"p{s.member_id}_saved", s.saved),
            "profit": figures.money(f"p{s.member_id}_profit", s.profit),
            "loan_debt": figures.money(f"p{s.member_id}_loan_debt", s.loan_debt),
            "ticket_debt": figures.money(f"p{s.member_id}_ticket_debt", s.ticket_debt),
            "receives": figures.money(f"p{s.member_id}_receives", s.to_pay),
            "still_owes": figures.money(f"p{s.member_id}_still_owes", s.still_owes),
        }
        for s in p.shares
    ]

    warnings = [
        "This is only a simulation: it does not move any money.",
        "Profits are split equally. Savings belong to each person.",
    ]
    if not p.cash_is_enough:
        warnings.append(
            f"There is NOT enough cash: {usd(p.total_to_pay - p.cash_available)} "
            "short. The loans have to be collected before paying out."
        )
    if p.total_still_owed:
        warnings.append(
            f"Nobody is paid a negative amount: {usd(p.total_still_owed)} is still "
            "owed by those who owe more than their share."
        )
    if not balance.ok:
        warnings.append(
            "Careful: the books do not balance. Check with check_integrity."
        )

    return ok(
        f"As of {upto} the payout would be {usd(p.total_to_pay)} across "
        f"{len(shares)} members. Each one receives "
        f"{usd(p.shares[0].profit) if shares else '$0'} of profit.",
        is_simulation=True,
        cutoff_date=upto,
        shares=shares,
        member_count=figures.number("member_count", len(shares)),
        total_savings=figures.money("total_savings", p.total_savings),
        total_profits=figures.money("total_profits", p.total_profits),
        total_to_pay=figures.money("total_to_pay", p.total_to_pay),
        total_still_owed=figures.money("total_still_owed", p.total_still_owed),
        cash_available=figures.money("cash_available", f.cash),
        cash_is_enough=p.cash_is_enough,
        balances=balance.ok,
        figures=figures,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# check_integrity
# --------------------------------------------------------------------------


def check_integrity(con: sqlite3.Connection) -> dict[str, Any]:
    """Runs the world's invariants and reports each one.

    It exists for two reasons: it gives Nati a legitimate way to say "the books
    do not balance" instead of improvising, and it gives the exercise's checker a
    single place to hook into.
    """
    upto = clock.today_iso()
    f = q.fund(con, upto)
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    record("the fund's cash is not negative", f.cash >= 0, usd(f.cash))
    record("distributable profits are not negative", f.profits >= 0, usd(f.profits))

    # The payout arithmetic REFUSES to run on negative profits, and this tool is
    # how anyone finds out the books are wrong — so it must survive the very
    # state it exists to report. It did not: a prize larger than the year's
    # profits made `compute_payout` raise, which turned both this tool and
    # `simulate_payout` into `internal_error` for the rest of the cycle, with no
    # way back because a draw is never edited or deleted. Reporting beats
    # crashing; the balance check is simply skipped when it cannot be computed.
    balance = None
    if f.profits >= 0:
        try:
            balance = q.payout_balance_check(con, upto)
        except (ValueError, ArithmeticError) as exc:      # pragma: no cover
            record("the payout balances to the dollar", False, str(exc))
    if balance is not None:
        record("the payout balances to the dollar", balance.ok, balance.text)
    elif f.profits < 0:
        record(
            "the payout balances to the dollar", False,
            "cannot be computed while profits are negative",
        )
    # An INDEPENDENT statement of the boundary: the fund's interest plus every
    # private lender's interest must account for all interest ever charged, with
    # nothing double-counted. Comparing `f.interest_charged` against the same
    # `lender_id = 0` SQL that produced it — as this once did — could not fail.
    all_interest = int(
        con.execute(
            """SELECT COALESCE(SUM(i.interest_amount), 0)
                 FROM loan_interest i JOIN loans l ON l.id = i.loan_id
                WHERE l.status != 'voided'"""
        ).fetchone()[0]
    )
    personal_interest_charged = int(
        con.execute(
            """SELECT COALESCE(SUM(i.interest_amount), 0)
                 FROM loan_interest i JOIN loans l ON l.id = i.loan_id
                WHERE l.lender_id != 0 AND l.status != 'voided'"""
        ).fetchone()[0]
    )
    record(
        "interest on personal money stayed out of the fund",
        f.interest_charged + personal_interest_charged == all_interest
        and personal_interest_charged >= 0,
        f"fund {usd(f.interest_charged)} + private {usd(personal_interest_charged)}"
        f" = {usd(all_interest)}",
    )
    overpaid = [
        int(r["id"]) for r in con.execute(
            "SELECT id FROM loans WHERE status != 'voided'"
        )
        if q.loan_balance(con, int(r["id"]), upto).overpaid > 0
    ]
    record("no loan has been overpaid", not overpaid, str(overpaid or "none"))
    closed_with_balance = [
        int(r["id"]) for r in con.execute(
            "SELECT id FROM loans WHERE status = 'closed'"
        )
        if q.loan_balance(con, int(r["id"]), upto).total != 0
    ]
    record(
        "closed loans are at zero",
        not closed_with_balance,
        str(closed_with_balance or "all at zero"),
    )
    unbacked_prizes = [
        f"{r['kind']} month {r['month']}" for r in con.execute("SELECT * FROM draws")
        if int(r["prize"]) > int(
            con.execute(
                """SELECT COALESCE(SUM(price),0) FROM raffle_tickets
                    WHERE kind=? AND month=? AND year=? AND paid=1""",
                (r["kind"], r["month"], r["year"]),
            ).fetchone()[0]
        )
    ]
    record(
        "no prize exceeded what its month collected",
        not unbacked_prizes,
        str(unbacked_prizes or "none"),
    )

    failing = [c["check"] for c in checks if not c["passed"]]

    # This tool used to return no `figures[]` manifest at all, which made it the
    # one read tool whose amounts Nati could not state as money: the output
    # filter requires a dollar figure to have been TAGGED as money by a tool,
    # and an untagged integer in the body does not qualify. So the most natural
    # question a person can ask — "do the books balance?" — got a correct answer
    # from the tool and then had the reply blocked on its own cash figure.
    figures = Figures()
    figures.money("cash", f.cash)
    figures.money("distributable_profits", f.profits)
    figures.money("equity", f.equity)
    if balance is not None:
        figures.money("payout_expected", balance.expected)
        figures.money("payout_computed", balance.computed)
    figures.date("checked_on", upto)

    return ok(
        "Everything balances." if not failing
        else f"{len(failing)} thing(s) do not balance: {'; '.join(failing)}.",
        all_good=not failing,
        checks=checks,
        failing=failing,
        figures=figures,
    )
