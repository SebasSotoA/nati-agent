"""The 6 rare write tools, every one two-phase — plus `mark_ticket_paid`.

`mark_ticket_paid` is daily and single-phase and belongs in `writes.py`; it ended
up here and the filename now lies about it. A move, not a rewrite, whenever the
write path is next open for changes.

Everything here is something the administrator does a few times a year — a new
loan, a draw, a correction — and each one can move real money the wrong way if
it lands wrong. So on top of the shape gates and the database's own rules,
these require the two-phase confirmation from `confirm.py`: the first call
returns a deterministic summary and a token, and the write happens only when
the same call comes back with that token.

What is deliberately NOT here, so it cannot be talked into:

- No `close_loan` (closing is a consequence of reaching zero, in writes.py).
- No un-void, no un-pay, no edit, no delete of anything.
- `record_reversal` covers savings entries ONLY. A loan payment cannot be
  reversed: the interest-first arithmetic is order-dependent, so a negative
  payment has no sound meaning. Overpayment is refused at entry, which removes
  the worst payment typo; the residue is documented in the SOP.
- `record_draw` accepts the winning number the administrator reports, records
  it, and derives everything else (prize, winner, date).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from nati import clock
from nati.tools import calc, confirm, gates
from nati.tools.envelope import Figures, ok, refuse, usd
from nati.tools.writes import _confirmed_member, _movement_date
from nati.world import db
from nati.world import queries as q

FUND_ID = q.FUND_ID

_PHONE = re.compile(r"^\d{10}$")

# The only settings a tool may write. Everything else in `settings` — who the
# administrator is, which year the cycle is — is not reachable from chat at all.
_SETTABLE_KEYS = ("monthly_ticket_price", "quarterly_ticket_price",
                  "monthly_prize", "quarterly_prize")
_SETTING_CEILING = 10_000  # a raffle prize is tens of dollars, not millions


# --------------------------------------------------------------------------
# add_member
# --------------------------------------------------------------------------


def add_member(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    name: object,
    phone: object,
    confirm_token: object = None,
) -> dict[str, Any]:
    """Adds a member — only while the cycle has not started moving money.

    The administrator's rule: nobody joins mid-cycle. Otherwise a member added
    in November would take an equal share of a year of profits for nothing,
    diluting everyone who saved all year.
    """
    member_name = gates.text(name, "name", maximum=80)
    phone_text = gates.text(phone, "phone", maximum=20).replace(" ", "")
    if not _PHONE.fullmatch(phone_text):
        return refuse(
            "invalid_phone", "The phone number has to be exactly 10 digits."
        )

    year = q.cycle_year(con)
    moved = con.execute(
        """SELECT EXISTS (SELECT 1 FROM savings  WHERE substr(date,1,4) = :y)
             OR EXISTS (SELECT 1 FROM raffle_tickets WHERE year = :y)""",
        {"y": f"{year:04d}"},
    ).fetchone()[0]
    if moved:
        # The date is stated here on purpose: "wait for the next cycle" invites
        # exactly the question "when is that?", and a calendar date is not a
        # figure about anyone's money, so naming it is no read-oracle risk.
        return refuse(
            "cycle_started",
            f"The {year} cycle already has money moving, and nobody joins "
            "mid-cycle: a new member would take an equal share of profits the "
            f"others built. The {year} cycle closes at the end of November "
            f"{year}, and {member_name} can join when the {year + 1} cycle "
            f"starts on {year + 1}-01-01.",
            can_retry=False,
        )

    pending = confirm.require(
        ctx.conv_id, "add_member", ctx.raw_args or {},
        f"Add {member_name} (phone {phone_text}) as a member of the natillera, "
        f"joining today, {clock.today_iso()}.",
        turn=ctx.turn,
    )
    if pending is not None:
        return pending

    result = db.insert(
        con, "members",
        {"name": member_name, "phone": phone_text, "joined_on": clock.today_iso()},
        ctx,
    )
    return ok(
        f"Done: {member_name} is now a member, joined {clock.today_iso()}.",
        member_id=result["id"], audit_id=result["audit_id"],
        member=member_name, joined_on=clock.today_iso(),
        world_hash=db.world_hash(con),
    )


# --------------------------------------------------------------------------
# record_loan
# --------------------------------------------------------------------------


def _personal_lender(
    con: sqlite3.Connection, lender_member_id: object = None
) -> sqlite3.Row:
    """Whose private money funds a loan. Named explicitly, never inferred.

    Several people in this natillera lend privately, not just the administrator.
    Interest on a personal loan belongs to that person alone and never enters the
    fund or the payout, so putting it in the wrong pocket is taking from someone.
    Guessing is therefore not an option: if more than one person lends and the
    caller has not said which, this refuses.
    """
    rows = con.execute(
        """SELECT le.id, le.name, le.member_id, m.name AS member_name
             FROM lenders le JOIN members m ON m.id = le.member_id
            WHERE le.kind = 'personal' ORDER BY le.id"""
    ).fetchall()
    if not rows:
        raise gates.Refusal(
            "no_personal_lender",
            "Nobody here is set up to lend their own money, so I cannot record "
            "that.",
        )

    if lender_member_id not in (None, ""):
        wanted = gates.integer(lender_member_id, "lender")
        for row in rows:
            if int(row["member_id"]) == wanted:
                return row
        raise gates.Refusal(
            "not_a_lender",
            "That person does not lend their own money through the natillera, "
            "so a loan cannot be funded from them.",
        )

    if len(rows) == 1:
        return rows[0]

    who = ", ".join(r["member_name"] for r in rows)
    raise gates.Refusal(
        "ambiguous_lender",
        "Several people lend their own money, so I need you to tell me whose "
        f"money this loan is: {who}.",
    )


def record_loan(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    member_id: object,
    confirm_name: object,
    amount: object,
    monthly_rate: object,
    money_from: object,
    when: object = None,
    lender_member_id: object = None,
    confirm_token: object = None,
) -> dict[str, Any]:
    """Creates a loan. Whose money funds it is decided HERE and never changes."""
    member = _confirmed_member(con, member_id, confirm_name)
    principal = gates.amount(amount, "loan amount")
    rate = gates.in_range(monthly_rate, "monthly rate", 0, 99)
    source = gates.choice(money_from, "source of the money", ("fund", "personal"))
    date_used = _movement_date(con, when)

    if date_used < member["joined_on"]:
        return refuse(
            "before_joining",
            f"{member['name']} joined on {member['joined_on']}, so a loan "
            "cannot start before that.",
        )

    if source == "fund":
        lender_id, lender_name = FUND_ID, "the fund"
    else:
        lender = _personal_lender(con, lender_member_id)
        lender_id, lender_name = int(lender["id"]), lender["member_name"]

    # Re-sourcing a voided loan — void as fund, recreate as personal — is how the
    # group's interest becomes one person's income without touching the protected
    # lender column.
    #
    # Every detail of the replacement is chosen by whoever is asking, so no
    # detail can be part of the test. Two earlier versions learned this the hard
    # way: matching on (member, principal, started_on) fell to "$1 more", and
    # matching on the replacement's MONTH fell to moving the date four days into
    # the previous month — which re-sourced $1,000 of the group's money into one
    # person's pocket while `check_integrity` still said "everything balances".
    #
    # So the test is now only about facts the asker does not control: this member
    # has a loan voided in this cycle that was funded from a different pocket.
    # Any private replacement is refused, whatever its amount, rate or date. A
    # re-entry from the SAME source stays allowed, which is the only thing a
    # genuine data-entry correction needs.
    resourced = con.execute(
        """SELECT 1 FROM loans
            WHERE member_id = ? AND status = 'voided' AND lender_id != ?
              AND substr(started_on, 1, 4) = ?""",
        (int(member["id"]), lender_id, f"{q.cycle_year(con):04d}"),
    ).fetchone()
    if resourced:
        return refuse(
            "resourced_loan",
            f"{member['name']} already has a loan from that month that was "
            "voided and funded from the other source. Whose money funds a loan "
            "is not changed by voiding it and recording a new one.",
            can_retry=False,
        )

    figures = Figures()
    figures.money("principal", principal)
    figures.number("monthly_rate", rate)
    figures.date("started_on", date_used)
    pending = confirm.require(
        ctx.conv_id, "record_loan", ctx.raw_args or {},
        f"Lend {usd(principal)} to {member['name']} at {rate}% monthly "
        f"(charged on the original amount), funded by {lender_name}, "
        f"starting {date_used}.",
        figures,
        turn=ctx.turn,
    )
    if pending is not None:
        return pending

    def fund_covers(c: sqlite3.Connection, _pk: int) -> None:
        """POST-write invariant: the fund's cash never goes negative."""
        if q.fund(c, clock.today_iso()).cash < 0:
            raise db.InvariantBroken(
                "The fund does not have enough cash to lend that amount."
            )

    result = db.insert(
        con, "loans",
        {"member_id": int(member["id"]), "lender_id": lender_id,
         "principal": principal, "monthly_rate": rate, "started_on": date_used},
        ctx, verify=fund_covers if lender_id == FUND_ID else None,
    )

    monthly_interest = calc.percent(principal, rate)
    figures.money("monthly_interest", monthly_interest)
    return ok(
        f"Done: I recorded a loan of {usd(principal)} to {member['name']} at "
        f"{rate}% monthly, funded by {lender_name}, starting {date_used}. Each "
        f"month it accrues {usd(monthly_interest)} of interest until repaid.",
        loan_id=result["id"], audit_id=result["audit_id"],
        member=member["name"], money_from=source,
        world_hash=db.world_hash(con),
        figures=figures,
        warnings=(
            [] if source == "fund" else
            [f"This loan is {lender_name}'s own money: its interest and "
             "repayments are theirs personally and never enter the fund."]
        ),
    )


# --------------------------------------------------------------------------
# void_loan
# --------------------------------------------------------------------------


def void_loan(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    loan_id: object,
    confirm_token: object = None,
) -> dict[str, Any]:
    """Voids a loan — a SAME-MONTH data-entry correction, not a revision.

    Once a loan has any payment or any interest, or its month has passed, it is
    history: voiding it would erase a real debt or real income.
    """
    lid = gates.integer(loan_id, "loan")
    loan = con.execute(
        """SELECT l.*, m.name FROM loans l JOIN members m ON m.id = l.member_id
            WHERE l.id = ?""", (lid,),
    ).fetchone()
    if loan is None:
        return refuse("unknown_loan", "I have no record of that loan.")
    if loan["status"] != "active":
        return refuse(
            "not_active", f"That loan is already {loan['status']}.", can_retry=False
        )

    has_movements = con.execute(
        """SELECT EXISTS (SELECT 1 FROM loan_payments WHERE loan_id = :l)
             OR EXISTS (SELECT 1 FROM loan_interest WHERE loan_id = :l)""",
        {"l": lid},
    ).fetchone()[0]
    if has_movements:
        return refuse(
            "loan_has_history",
            "That loan already has payments or interest on its book, so it is "
            "part of the natillera's history and cannot be voided.",
            can_retry=False,
        )
    if loan["started_on"][:7] != clock.today_iso()[:7]:
        return refuse(
            "correction_window_closed",
            "Voiding is only for correcting a loan entered wrongly this same "
            "month. That loan is older, so it stays on the books.",
            can_retry=False,
        )

    figures = Figures()
    figures.money("principal", int(loan["principal"]))
    pending = confirm.require(
        ctx.conv_id, "void_loan", ctx.raw_args or {},
        f"Void loan {lid}: {usd(int(loan['principal']))} to {loan['name']} "
        f"started {loan['started_on']}. It stays visible as voided and stops "
        "counting everywhere.",
        figures,
        turn=ctx.turn,
    )
    if pending is not None:
        return pending

    result = db.update(con, "loans", lid, {"status": "voided"}, ctx)
    return ok(
        f"Done: loan {lid} ({usd(int(loan['principal']))} to {loan['name']}) is "
        "voided. It remains visible in the books and no longer counts anywhere. "
        "If the loan was real, record it again with the right details.",
        loan_id=lid, audit_id=result["audit_id"], member=loan["name"],
        world_hash=db.world_hash(con), figures=figures,
    )


# --------------------------------------------------------------------------
# record_reversal
# --------------------------------------------------------------------------


def record_reversal(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    saving_id: object,
    confirm_token: object = None,
) -> dict[str, Any]:
    """Reverses ONE savings entry with an opposite entry. Nothing is erased.

    Both rows stay visible and every total nets out. Partial corrections do not
    exist: the whole entry is reversed, then the right one is recorded.
    """
    sid = gates.integer(saving_id, "saving")
    row = con.execute(
        """SELECT s.*, m.name FROM savings s JOIN members m ON m.id = s.member_id
            WHERE s.id = ?""", (sid,),
    ).fetchone()
    if row is None:
        return refuse("unknown_saving", "I have no record of that saving.")
    if row["reversal_of"] is not None:
        return refuse(
            "reversal_of_reversal",
            "That entry is itself a correction, and corrections are not "
            "corrected. If the original was right after all, record it again "
            "as a new saving.",
            can_retry=False,
        )
    already = con.execute(
        "SELECT id FROM savings WHERE reversal_of = ?", (sid,)
    ).fetchone()
    if already:
        return refuse(
            "already_reversed",
            "That saving was already reversed once, and a second reversal "
            "would refund it twice.",
            can_retry=False,
        )

    amount = int(row["amount"])
    figures = Figures()
    figures.money("amount", amount)
    figures.date("original_date", row["date"])
    pending = confirm.require(
        ctx.conv_id, "record_reversal", ctx.raw_args or {},
        f"Reverse saving {sid}: {usd(amount)} recorded for {row['name']} on "
        f"{row['date']}. An opposite entry dated today undoes it; both stay "
        "visible.",
        figures,
        turn=ctx.turn,
    )
    if pending is not None:
        return pending

    def fund_stays_solvent(c: sqlite3.Connection, _pk: int) -> None:
        """POST-write invariant: B2 says the fund's cash can NEVER go negative.

        Undoing a saving takes money back out, so a reversal moves cash exactly
        like a loan does — but this hook was only wired to `record_loan`, so
        lending the fund down to zero and then reversing a saving left cash at
        -$200. `check_integrity` reported it afterwards; B2 says *never*, which
        means refusing it, not noticing it.
        """
        if q.fund(c, clock.today_iso()).cash < 0:
            raise db.InvariantBroken(
                "Undoing that saving would leave the fund with less cash than "
                "it has. Collect what is owed first, or correct it after the "
                "next repayment."
            )

    result = db.insert(
        con, "savings",
        {"member_id": int(row["member_id"]), "date": clock.today_iso(),
         "amount": -amount, "reversal_of": sid},
        ctx, verify=fund_stays_solvent,
    )
    total = int(
        con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM savings WHERE member_id = ?",
            (int(row["member_id"]),),
        ).fetchone()[0]
    )
    figures.money("total_saved", total)
    return ok(
        f"Done: I reversed the {usd(amount)} saving for {row['name']} from "
        f"{row['date']}. Their savings now total {usd(total)}. If the real "
        "amount was different, record it now as a new saving.",
        reversal_id=result["id"], audit_id=result["audit_id"],
        reversed_saving_id=sid, member=row["name"],
        world_hash=db.world_hash(con), figures=figures,
    )


# --------------------------------------------------------------------------
# record_draw
# --------------------------------------------------------------------------


def record_draw(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    kind: object,
    month: object,
    winning_number: object,
    confirm_token: object = None,
) -> dict[str, Any]:
    """Records a raffle draw. The winner and the prize are DERIVED, never given.

    The administrator reports the winning number; the system checks it against
    the tickets. If nobody holds it, the prize is 0 and the money stays in the
    natillera — that is the confirmed house rule.
    """
    which = gates.choice(kind, "raffle kind", ("monthly", "quarterly"))
    m = gates.cycle_month(month)
    n = gates.raffle_number(winning_number, "winning number")
    year = q.cycle_year(con)
    today = clock.today()

    if (year, m) > (today.year, today.month):
        return refuse(
            "future_month",
            f"Month {m} has not happened yet, so its draw cannot be recorded.",
        )
    if con.execute(
        "SELECT 1 FROM draws WHERE kind = ? AND month = ? AND year = ?",
        (which, m, year),
    ).fetchone():
        return refuse(
            "draw_exists",
            f"The {which} draw for month {m} is already recorded, and a draw "
            "is not edited or repeated.",
            can_retry=False,
        )

    if which == "monthly":
        winner = con.execute(
            """SELECT t.member_id, mm.name FROM raffle_tickets t
                 JOIN members mm ON mm.id = t.member_id
                WHERE t.kind = ? AND t.month = ? AND t.year = ?
                  AND ? IN (t.number_1, t.number_2)""",
            (which, m, year, n),
        ).fetchone()
    else:
        winner = con.execute(
            """SELECT t.member_id, mm.name FROM raffle_tickets t
                 JOIN members mm ON mm.id = t.member_id
                WHERE t.kind = ? AND t.month = ? AND t.year = ?
                  AND t.number_1 = ?""",
            (which, m, year, n),
        ).fetchone()

    prize_key = "monthly_prize" if which == "monthly" else "quarterly_prize"
    prize = q.setting_int(con, prize_key, clock.today_iso(), 0) if winner else 0

    if prize > q.fund(con, clock.today_iso()).cash:
        return refuse(
            "insufficient_cash",
            "The fund does not have enough cash to pay that prize right now.",
        )

    # A prize may never exceed what its OWN month collected. Without this, the
    # prize was valued at today's setting and checked only against total cash, so
    # raising the prize above the year's profits and then recording one draw drove
    # distributable profits negative — and negative profits make the payout
    # arithmetic raise, which killed `check_integrity` and `simulate_payout` for
    # the rest of the cycle. Draws are never edited or deleted, so that was
    # irreversible: the two tools that detect every other kind of tampering could
    # be switched off permanently by one legitimate-looking pair of calls.
    collected = int(
        con.execute(
            """SELECT COALESCE(SUM(price), 0) FROM raffle_tickets
                WHERE kind = ? AND month = ? AND year = ? AND paid = 1""",
            (which, m, year),
        ).fetchone()[0]
    )
    if prize > collected:
        return refuse(
            "prize_exceeds_income",
            f"The {which} raffle for month {m} did not take in enough to pay a "
            "prize that size. A raffle pays out of what it collected.",
        )

    outcome = (
        f"{winner['name']} holds number {n:02d} and wins {usd(prize)}."
        if winner
        else f"nobody holds number {n:02d}, so no prize is paid and the money "
             "stays in the natillera."
    )
    figures = Figures()
    figures.number("winning_number", n)
    figures.money("prize", prize)
    pending = confirm.require(
        ctx.conv_id, "record_draw", ctx.raw_args or {},
        f"Record the {which} draw for month {m}: winning number {n:02d} — "
        f"{outcome}",
        figures,
        turn=ctx.turn,
    )
    if pending is not None:
        return pending

    result = db.insert(
        con, "draws",
        {"kind": which, "month": m, "year": year,
         "winning_number": n, "prize": prize, "date": clock.today_iso()},
        ctx,
    )
    return ok(
        f"Done: the {which} draw for month {m} is recorded — {outcome}",
        draw_id=result["id"], audit_id=result["audit_id"],
        kind=which, month=m, year=year,
        winner=(winner["name"] if winner else None),
        world_hash=db.world_hash(con), figures=figures,
        warnings=(
            [] if winner is None else
            [f"The {usd(prize)} prize comes out of the fund's cash."]
        ),
    )


# --------------------------------------------------------------------------
# set_setting
# --------------------------------------------------------------------------


def set_setting(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    key: object,
    value: object,
    confirm_token: object = None,
) -> dict[str, Any]:
    """Changes a raffle price or prize, FROM TODAY ONWARD. The past keeps its
    values: each ticket froze its price at sale and each month is valued with
    the entry that applied to it."""
    which = gates.choice(key, "setting", _SETTABLE_KEYS)
    amount = gates.amount(value, "value")
    if amount > _SETTING_CEILING:
        return refuse(
            "implausible_setting",
            f"That value is far outside what a raffle {which.split('_')[-1]} "
            "could plausibly be. Check the number.",
        )

    current = q.setting_int(con, which, clock.today_iso(), 0)
    label = which.replace("_", " ")
    figures = Figures()
    figures.money("current", current)
    figures.money("new", amount)
    pending = confirm.require(
        ctx.conv_id, "set_setting", ctx.raw_args or {},
        f"Change the {label} from {usd(current)} to {usd(amount)}, effective "
        f"today, {clock.today_iso()}. Everything already sold or drawn keeps "
        "its old value.",
        figures,
        turn=ctx.turn,
    )
    if pending is not None:
        return pending

    result = db.insert(
        con, "settings",
        {"key": which, "value": str(amount),
         "effective_from": clock.today_iso(), "note": "changed via chat"},
        ctx,
    )
    return ok(
        f"Done: the {label} is {usd(amount)} from {clock.today_iso()} onward. "
        f"It was {usd(current)}, and everything before today keeps that value.",
        setting_id=result["id"], audit_id=result["audit_id"],
        key=which, world_hash=db.world_hash(con), figures=figures,
    )


# --------------------------------------------------------------------------
# mark_ticket_paid
# --------------------------------------------------------------------------


def mark_ticket_paid(
    con: sqlite3.Connection,
    ctx: db.Ctx,
    member_id: object,
    confirm_name: object,
    kind: object,
    month: object,
) -> dict[str, Any]:
    """Marks a member's unpaid ticket as paid today.

    It takes the person and the month — never a ticket id — so a hallucinated
    id cannot erase somebody's real debt. Single-phase: it is day-to-day money
    coming IN, at the price already frozen on the ticket.
    """
    member = _confirmed_member(con, member_id, confirm_name)
    which = gates.choice(kind, "raffle kind", ("monthly", "quarterly"))
    m = gates.cycle_month(month)
    year = q.cycle_year(con)

    ticket = con.execute(
        """SELECT * FROM raffle_tickets
            WHERE member_id = ? AND kind = ? AND month = ? AND year = ?""",
        (int(member["id"]), which, m, year),
    ).fetchone()
    if ticket is None:
        return refuse(
            "no_ticket",
            f"{member['name']} has no {which} ticket for month {m}.",
        )
    if int(ticket["paid"]) == 1:
        return refuse(
            "already_paid",
            f"{member['name']}'s {which} ticket for month {m} is already paid.",
            can_retry=False,
        )

    result = db.update(
        con, "raffle_tickets", int(ticket["id"]),
        {"paid": 1, "paid_on": clock.today_iso()},
        ctx,
    )
    figures = Figures()
    figures.money("price", int(ticket["price"]))
    return ok(
        f"Done: {member['name']}'s {which} ticket for month {m} is paid "
        f"({usd(int(ticket['price']))}, the price it was sold at). That debt "
        "is settled.",
        ticket_id=int(ticket["id"]), audit_id=result["audit_id"],
        member=member["name"], kind=which, month=m,
        world_hash=db.world_hash(con), figures=figures,
    )
