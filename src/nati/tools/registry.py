"""The tools' JSON schemas and the dispatcher.

Two robustness rules, both learned the hard way and both model-independent:

- `parameters` has to be a JSON OBJECT, not a string. Passing a string is the
  most common cause of a rejected tool definition.
- A tool NEVER returns empty content. Some models reject an empty tool result
  outright; returning a sentence always is free and works everywhere.

The dispatcher is also where a refusal's `reason` is separated from the envelope:
it goes to the trace and the audit log, never to the model. If the model could see
it, an attacker would enumerate the entire taxonomy of controls through invalid
requests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from nati.tools import gates, reads, writes, writes_rare
from nati.tools.envelope import refuse, split_reason
from nati.world import db
from nati.world import queries


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., dict[str, Any]]
    writes: bool

    def schema(self) -> dict[str, Any]:
        """The schema in the shape the API expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_MEMBER_ID = {
    "type": "integer",
    "description": "The member's number (member_id). If you do not know it, look "
                   "it up first with the members tool.",
}
_CONFIRM_NAME = {
    "type": "string",
    "description": "That person's name, to confirm it is the right one. Some "
                   "members have similar names, so this prevents recording money "
                   "against somebody else.",
}
_DATE = {
    "type": "string",
    "description": "Date as 2026-08-04 (year-month-day). If you leave it out, I "
                   "use today.",
}
_RAFFLE_KIND = {"type": "string", "enum": ["monthly", "quarterly"]}
_MONTH = {
    "type": "integer",
    "description": "Month of the cycle, 1 (January) to 11 (November). December is "
                   "not part of the cycle.",
}
_CONFIRM_TOKEN = {
    "type": "string",
    "description": "Leave this out on the first call. The tool will answer with "
                   "a summary and a token; read the summary to the administrator "
                   "and, ONLY if she confirms, call again with the same details "
                   "plus this token.",
}


TOOLS: tuple[Tool, ...] = (
    # ---------------- READS ----------------
    Tool(
        "members",
        "Lists the natillera's members with how much each one has saved. If you "
        "pass `search`, it filters by name. When a name matches more than one "
        "person it returns all of them and flags it as ambiguous: in that case "
        "ask the administrator which one they mean.",
        _obj({"search": {"type": "string", "description": "Part of a name."}}, []),
        lambda con, ctx, **kw: reads.members(con, kw.get("search")),
        writes=False,
    ),
    Tool(
        "member_status",
        "Everything about one person in a single place: how much they have saved, "
        "their loans and balances, how much they owe the fund, how much they owe "
        "on the administrator's own money (reported separately), and any raffle "
        "tickets they have not paid.",
        _obj({"member_id": _MEMBER_ID, "date": _DATE}, ["member_id"]),
        lambda con, ctx, **kw: reads.member_status(
            con, kw.get("member_id"), kw.get("date")
        ),
        writes=False,
    ),
    Tool(
        "fund_status",
        "The state of the common fund: cash available, capital out on loan, "
        "equity, income and distributable profits. The administrator's own money "
        "is reported separately and is NOT part of these figures. "
        "ALSO returns, with no arguments needed, the facts about this cycle you "
        "must never guess: which year the cycle is, when it ends, when the next "
        "one starts, and exactly which months each raffle has run in. Call this "
        "whenever you are asked about the calendar or the raffle schedule.",
        _obj({"date": _DATE}, []),
        lambda con, ctx, **kw: reads.fund_status(con, kw.get("date")),
        writes=False,
    ),
    Tool(
        "loan_status",
        "The detail of one loan: how much capital is owed, how much overdue "
        "interest, and how each payment was applied.",
        _obj({"loan_id": {"type": "integer"}, "date": _DATE}, ["loan_id"]),
        lambda con, ctx, **kw: reads.loan_status(
            con, kw.get("loan_id"), kw.get("date")
        ),
        writes=False,
    ),
    Tool(
        "list_loans",
        "The loan book: who owes money. Can be filtered by status (active, "
        "closed, voided) and by source (fund or personal).",
        _obj(
            {
                "status": {"type": "string", "enum": ["active", "closed", "voided"]},
                "money_from": {"type": "string", "enum": ["fund", "personal"]},
            },
            [],
        ),
        lambda con, ctx, **kw: reads.list_loans(
            con, kw.get("status"), kw.get("money_from")
        ),
        writes=False,
    ),
    Tool(
        "raffle_status",
        "A raffle's tickets for one month: who holds which numbers, who paid and "
        "who still owes, how much was collected, and the draw result if it has "
        "already happened.",
        _obj({"kind": _RAFFLE_KIND, "month": _MONTH}, ["kind", "month"]),
        lambda con, ctx, **kw: reads.raffle_status(con, kw.get("kind"), kw.get("month")),
        writes=False,
    ),
    Tool(
        "simulate_payout",
        "Simulates the year-end payout: what each member would receive. Profits "
        "are split EQUALLY; savings belong to each person. This does NOT move any "
        "money, it is only a calculation.",
        _obj({"date": _DATE}, []),
        lambda con, ctx, **kw: reads.simulate_payout(con, kw.get("date")),
        writes=False,
    ),
    Tool(
        "check_integrity",
        "Checks that the natillera's books balance: that cash is not negative, "
        "that the payout adds up exactly, that no loan has been overpaid, and "
        "that personal money has not been mixed into the fund.",
        _obj({}, []),
        lambda con, ctx, **kw: reads.check_integrity(con),
        writes=False,
    ),
    # ---------------- WRITES ----------------
    Tool(
        "record_saving",
        "Records a savings contribution from a member. There is no fixed quota: "
        "each person saves what they can, when they can.",
        _obj(
            {
                "member_id": _MEMBER_ID,
                "confirm_name": _CONFIRM_NAME,
                "amount": {"type": "integer", "description": "Whole US dollars."},
                "date": _DATE,
            },
            ["member_id", "confirm_name", "amount"],
        ),
        lambda con, ctx, **kw: writes.record_saving(
            con, ctx, kw.get("member_id"), kw.get("confirm_name"),
            kw.get("amount"), kw.get("date"),
        ),
        writes=True,
    ),
    Tool(
        "record_payment",
        "Records a payment against a loan. The payment covers overdue interest "
        "first and whatever is left reduces the capital; the system works that "
        "out. If the payment settles the loan, it closes by itself.",
        _obj(
            {
                "loan_id": {
                    "type": "integer",
                    "description": "The loan's number. If you do not know it, "
                                   "check that person's member_status.",
                },
                "amount": {"type": "integer", "description": "Whole US dollars paid."},
                "date": _DATE,
            },
            ["loan_id", "amount"],
        ),
        lambda con, ctx, **kw: writes.record_payment(
            con, ctx, kw.get("loan_id"), kw.get("amount"), kw.get("date")
        ),
        writes=True,
    ),
    Tool(
        "accrue_monthly_interest",
        "Charges a month's interest. The system computes the amount from each "
        "loan's rate; it cannot be given by hand. If you do not pass a loan, it "
        "charges every active FUND loan (personally-funded ones have to be done "
        "one at a time).",
        _obj(
            {
                "month": _MONTH,
                "loan_id": {
                    "type": "integer",
                    "description": "Optional. A single loan instead of all of them.",
                },
            },
            ["month"],
        ),
        lambda con, ctx, **kw: writes.accrue_monthly_interest(
            con, ctx, kw.get("month"), kw.get("loan_id")
        ),
        writes=True,
    ),
    Tool(
        "sell_raffle_ticket",
        "Sells a raffle ticket. The monthly raffle takes TWO numbers from 00 to "
        "99; the quarterly takes ONE. Numbers cannot repeat within the same "
        "raffle. If it is not paid on the spot, it becomes a debt.",
        _obj(
            {
                "member_id": _MEMBER_ID,
                "confirm_name": _CONFIRM_NAME,
                "kind": _RAFFLE_KIND,
                "month": _MONTH,
                "number_1": {"type": "integer", "description": "From 0 to 99."},
                "number_2": {
                    "type": "integer",
                    "description": "From 0 to 99. Monthly raffle only.",
                },
                "paid": {
                    "type": "boolean",
                    "description": "Whether they paid it or still owe it.",
                },
            },
            ["member_id", "confirm_name", "kind", "month", "number_1"],
        ),
        lambda con, ctx, **kw: writes.sell_raffle_ticket(
            con, ctx, kw.get("member_id"), kw.get("confirm_name"),
            kw.get("kind"), kw.get("month"), kw.get("number_1"),
            kw.get("number_2"), kw.get("paid", True),
        ),
        writes=True,
    ),
    Tool(
        "mark_ticket_paid",
        "Marks a member's unpaid raffle ticket as paid, at the price it was "
        "sold at. Takes the person and the month, never a ticket number.",
        _obj(
            {
                "member_id": _MEMBER_ID,
                "confirm_name": _CONFIRM_NAME,
                "kind": _RAFFLE_KIND,
                "month": _MONTH,
            },
            ["member_id", "confirm_name", "kind", "month"],
        ),
        lambda con, ctx, **kw: writes_rare.mark_ticket_paid(
            con, ctx, kw.get("member_id"), kw.get("confirm_name"),
            kw.get("kind"), kw.get("month"),
        ),
        writes=True,
    ),
    # ------------- RARE WRITES (two-phase confirmation) -------------
    Tool(
        "add_member",
        "Adds a new member. Only possible before the cycle starts moving "
        "money: nobody joins mid-cycle. Asks for confirmation first.",
        _obj(
            {
                "name": {"type": "string", "description": "The person's full name."},
                "phone": {"type": "string", "description": "Exactly 10 digits."},
                "confirm_token": _CONFIRM_TOKEN,
            },
            ["name", "phone"],
        ),
        lambda con, ctx, **kw: writes_rare.add_member(
            con, ctx, kw.get("name"), kw.get("phone"), kw.get("confirm_token"),
        ),
        writes=True,
    ),
    Tool(
        "record_loan",
        "Creates a loan to a member. Interest accrues monthly on the ORIGINAL "
        "amount at the given rate. Whose money funds it — the fund's or the "
        "administrator's own — is fixed forever at creation. Asks for "
        "confirmation first.",
        _obj(
            {
                "member_id": _MEMBER_ID,
                "confirm_name": _CONFIRM_NAME,
                "amount": {"type": "integer", "description": "Whole US dollars."},
                "monthly_rate": {
                    "type": "integer",
                    "description": "Monthly interest percent, e.g. 3 for 3%.",
                },
                "money_from": {
                    "type": "string", "enum": ["fund", "personal"],
                    "description": "fund = the natillera's common money; "
                                   "personal = the administrator's own money "
                                   "(she is the only one who lends privately).",
                },
                "date": _DATE,
                "confirm_token": _CONFIRM_TOKEN,
            },
            ["member_id", "confirm_name", "amount", "monthly_rate", "money_from"],
        ),
        lambda con, ctx, **kw: writes_rare.record_loan(
            con, ctx, kw.get("member_id"), kw.get("confirm_name"),
            kw.get("amount"), kw.get("monthly_rate"), kw.get("money_from"),
            kw.get("date"), kw.get("confirm_token"),
        ),
        writes=True,
    ),
    Tool(
        "void_loan",
        "Voids a loan entered wrongly THIS month, before it has any payment or "
        "interest. The loan stays visible as voided. Older loans and loans "
        "with history cannot be voided. Asks for confirmation first.",
        _obj(
            {
                "loan_id": {"type": "integer"},
                "confirm_token": _CONFIRM_TOKEN,
            },
            ["loan_id"],
        ),
        lambda con, ctx, **kw: writes_rare.void_loan(
            con, ctx, kw.get("loan_id"), kw.get("confirm_token"),
        ),
        writes=True,
    ),
    Tool(
        "record_reversal",
        "Corrects a savings entry that was recorded wrongly, by adding an "
        "opposite entry that undoes it. Both entries stay visible. Only "
        "savings can be corrected this way. Asks for confirmation first.",
        _obj(
            {
                "saving_id": {
                    "type": "integer",
                    "description": "The id of the saving to reverse, from "
                                   "member_status or from when it was recorded.",
                },
                "confirm_token": _CONFIRM_TOKEN,
            },
            ["saving_id"],
        ),
        lambda con, ctx, **kw: writes_rare.record_reversal(
            con, ctx, kw.get("saving_id"), kw.get("confirm_token"),
        ),
        writes=True,
    ),
    Tool(
        "record_draw",
        "Records a raffle draw: the administrator reports the winning number, "
        "and the system works out whether anyone holds it and what prize is "
        "paid. If nobody matches, no prize is paid and the money stays in the "
        "natillera. One draw per raffle per month. Asks for confirmation first.",
        _obj(
            {
                "kind": _RAFFLE_KIND,
                "month": _MONTH,
                "winning_number": {
                    "type": "integer", "description": "From 0 to 99.",
                },
                "confirm_token": _CONFIRM_TOKEN,
            },
            ["kind", "month", "winning_number"],
        ),
        lambda con, ctx, **kw: writes_rare.record_draw(
            con, ctx, kw.get("kind"), kw.get("month"),
            kw.get("winning_number"), kw.get("confirm_token"),
        ),
        writes=True,
    ),
    Tool(
        "set_setting",
        "Changes a raffle ticket price or prize, effective today onward. "
        "Nothing already sold or drawn changes value. Asks for confirmation "
        "first.",
        _obj(
            {
                "key": {
                    "type": "string",
                    "enum": ["monthly_ticket_price", "quarterly_ticket_price",
                             "monthly_prize", "quarterly_prize"],
                },
                "value": {"type": "integer", "description": "Whole US dollars."},
                "confirm_token": _CONFIRM_TOKEN,
            },
            ["key", "value"],
        ),
        lambda con, ctx, **kw: writes_rare.set_setting(
            con, ctx, kw.get("key"), kw.get("value"), kw.get("confirm_token"),
        ),
        writes=True,
    ),
)

BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def schemas() -> list[dict[str, Any]]:
    """The schemas to send to the model."""
    return [t.schema() for t in TOOLS]


def schemas_hash() -> str:
    """sha256 of the schemas, to anchor each trace to the exact version."""
    raw = json.dumps(schemas(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Result:
    """What comes out of a dispatch: the model's part and the trace's, separated."""

    for_the_model: dict[str, Any]
    reason: str | None
    wrote: bool
    internal_error: str | None = None


def dispatch(
    con: sqlite3.Connection, name: str, arguments: dict[str, Any], ctx: db.Ctx
) -> Result:
    """Runs a tool. NEVER raises: an exception would kill the conversation.

    A confused model being able to take the agent down would be a trivial
    denial of service, so everything is translated into a refusal with a message.
    """
    tool = BY_NAME.get(name)
    if tool is None:
        envelope, reason = split_reason(
            refuse("unknown_tool", "I do not have a tool with that name.")
        )
        return Result(envelope, reason, wrote=False)

    tool_ctx = db.Ctx(
        conv_id=ctx.conv_id, seq=ctx.seq, tool=name,
        args=arguments, raw_args=arguments, actor=ctx.actor, turn=ctx.turn,
    )

    try:
        raw = tool.fn(con, tool_ctx, **arguments)
    except gates.Refusal as r:
        # Shape refusal: it never reached the database.
        envelope, _ = split_reason(refuse(r.reason, r.message))
        db.audit_refusal(con, tool_ctx, r.reason)
        return Result(envelope, r.reason, wrote=False)
    except db.InvariantBroken as exc:
        # The write happened and was undone: the world is untouched.
        envelope, _ = split_reason(refuse("invariant_broken", str(exc)))
        db.audit_refusal(con, tool_ctx, "invariant_broken")
        return Result(envelope, "invariant_broken", wrote=False)
    except db.WorldError as exc:
        envelope, _ = split_reason(refuse("world_refused", str(exc)))
        db.audit_refusal(con, tool_ctx, "world_refused")
        return Result(envelope, "world_refused", wrote=False)
    except (db.ProgrammingError, queries.InvalidSetting) as exc:
        # Our bug, or corrupted configuration data — either way not something the
        # user did. The model gets something generic; the detail (which may name
        # a settings key) goes only to the trace. Catching InvalidSetting here is
        # what keeps this function's "NEVER raises" promise: a poisoned money
        # setting must refuse the turn, not kill the conversation.
        envelope, _ = split_reason(
            refuse(
                "internal_error",
                "I could not do that. I have noted it so it can be looked at.",
                can_retry=False,
            )
        )
        db.audit_refusal(con, tool_ctx, "internal_error")
        return Result(envelope, "internal_error", wrote=False,
                      internal_error=str(exc))
    except (TypeError, ValueError, OverflowError) as exc:
        envelope, _ = split_reason(
            refuse("invalid_arguments", "I did not understand the details you gave me.")
        )
        db.audit_refusal(con, tool_ctx, "invalid_arguments")
        return Result(envelope, "invalid_arguments", wrote=False,
                      internal_error=f"{type(exc).__name__}: {exc}")

    envelope, reason = split_reason(raw)
    if not envelope or "ok" not in envelope:
        # Contract: never empty. Some models reject an empty tool result.
        envelope = {"ok": False, "message": "I got no result from that query."}
        reason = "empty_result"

    # SOP F6: a rejected attempt leaves evidence. The five `except` branches above
    # already audit, but most refusals are RETURNED as an envelope rather than
    # raised — unknown_loan, cycle_started, already_paid, resourced_loan, and
    # every confirmation request. Those wrote nothing to `audit_log`, so an
    # attacker could probe the whole fiduciary boundary and leave no trace in the
    # database at all. In this exercise the refusals ARE the product, so they are
    # recorded here, at the one place every tool result passes through.
    # NOT gated on `tool.writes`: an attacker enumerating member and loan ids
    # through the READ tools was leaving no database trace at all, which is
    # exactly the reconnaissance F6 exists to record.
    if not envelope.get("ok"):
        if envelope.get("confirmation_required"):
            db.audit_refusal(con, tool_ctx, "confirmation_required")
        elif reason:
            db.audit_refusal(con, tool_ctx, reason)

    wrote = bool(tool.writes and envelope.get("ok"))
    return Result(envelope, reason, wrote=wrote)
