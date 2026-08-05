"""Argument validation. Pure functions: these do NOT touch the database.

These are the "shape" gates: types, ranges, dates. They run first, before any
read, for two reasons:

1. A refusal here reveals nothing about the state of the world. If a refusal
   said "the balance is $1,234", that would be an unaudited read through the
   write door — and a way to discover balances by asking invalid questions.
2. They are the cheapest.

The model sends whatever it comes up with: strings where integers belong,
floats, booleans, dates in odd formats. None of that may reach the database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from nati import clock

# Technical ceiling, not policy. Loans are uncapped by the natillera's decision;
# but 2**63 does not fit in a SQLite INTEGER, and that is a different matter.
MAX_AMOUNT = 999_999_999

# The widest integer SQLite can store. Anything outside it raises OverflowError
# from the driver — NOT an IntegrityError — so it escaped every `except` clause
# in the dispatcher and surfaced as an unhandled 500, killing the conversation.
# A red-team audit reached it from a single chat message with an id of 2**63,
# on ten of the nineteen tools. Every integer is bounded here rather than at ten
# call sites, because the constraint belongs to the storage layer, not to any
# one tool.
_SQLITE_MIN = -(2 ** 63)
_SQLITE_MAX = 2 ** 63 - 1


def _within_sqlite_range(value: int, field: str) -> int:
    if not _SQLITE_MIN <= value <= _SQLITE_MAX:
        raise Refusal(
            "absurd_number",
            f"{_The(field)} is far too large a number to be real. Check it.",
        )
    return value


_DIGITS_ONLY = re.compile(r"^-?[0-9]+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# US thousands grouping: 50,000 / 1,234,567. This is the OPPOSITE of the
# Colombian convention, where "50.000" means fifty thousand. In USD "50.000"
# means fifty, so accepting it as 50000 would be a thousand-fold error on a
# money field, entered by someone who typed exactly what they meant.
_US_GROUPED = re.compile(r"^-?\d{1,3}(,\d{3})+$")
_DECIMAL = re.compile(r"^-?\d+\.\d+$")


@dataclass(frozen=True)
class Refusal(Exception):
    """An argument will not do. `reason` is internal; `message` is for the person.

    The `reason` NEVER travels to the model: it is a stable code for the trace
    and the checker. If the model could see it, an attacker could enumerate the
    whole taxonomy of controls by deliberately asking for invalid things.
    """

    reason: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - debugging only
        return f"{self.reason}: {self.message}"


# Fields are named as bare nouns ("amount", "member") and these helpers add the
# article, so no message reads "the value of the the member".


def _the(field: str) -> str:
    return f"the {field}"


def _The(field: str) -> str:  # noqa: N802 - sentence start
    return f"The {field}"


# --------------------------------------------------------------------------
# Integers
# --------------------------------------------------------------------------


def integer(value: object, field: str) -> int:
    """Convert to int, strictly. Rejects bool, float and odd text.

    Why so careful:
      - `bool` IS `int` in Python, so `member_id=True` would silently become
        member 1 (the administrator) with nobody noticing.
      - SQLite has type affinity, not types: `amount=50000.5` passes
        `CHECK (amount > 0)` and stores a fractional dollar. That is the end of
        the rule that money is whole numbers.
    """
    if isinstance(value, bool):
        raise Refusal("invalid_type", f"The value of {_the(field)} is not a number.")
    if isinstance(value, int):
        return _within_sqlite_range(value, field)
    if isinstance(value, float):
        raise Refusal(
            "fractional_amount",
            f"{_The(field)} has to be a whole number of dollars.",
        )
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "").lstrip("$")
        # US thousands separators are accepted: "50,000" -> 50000.
        if _US_GROUPED.fullmatch(cleaned):
            cleaned = cleaned.replace(",", "")
        if _DIGITS_ONLY.fullmatch(cleaned):
            return _within_sqlite_range(int(cleaned), field)
        # "50.000" lands here on purpose. In USD that is fifty point zero, not
        # fifty thousand, so it is refused as fractional rather than silently
        # multiplied by a thousand.
        if _DECIMAL.fullmatch(cleaned):
            raise Refusal(
                "fractional_amount",
                f"{_The(field)} has to be a whole number of dollars.",
            )
        raise Refusal(
            "invalid_type", f"I did not understand the value of {_the(field)}."
        )
    raise Refusal("invalid_type", f"The value of {_the(field)} is missing.")


def amount(value: object, field: str = "amount") -> int:
    """A dollar amount: whole, positive, and under the technical ceiling."""
    n = integer(value, field)
    if n <= 0:
        raise Refusal(
            "non_positive_amount", f"{_The(field)} has to be greater than zero."
        )
    if n > MAX_AMOUNT:
        raise Refusal(
            "absurd_amount",
            f"{_The(field)} is far too large. Check whether there are extra zeros.",
        )
    return n


def in_range(value: object, field: str, low: int, high: int) -> int:
    n = integer(value, field)
    if not low <= n <= high:
        raise Refusal(
            "out_of_range", f"{_The(field)} has to be between {low} and {high}."
        )
    return n


def raffle_number(value: object, field: str = "number") -> int:
    return in_range(value, field, 0, 99)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def iso_date(value: object, field: str = "date") -> str:
    """Validate and CANONICALIZE a date. Always returns 'YYYY-MM-DD'.

    The regex runs BEFORE parsing on purpose: `date.fromisoformat` accepts forms
    like "2026-W32-1" and "20260805". If one of those were stored as-is in a TEXT
    column, every query — which compares dates as text — would sort it wrongly
    and some would not see it at all.

    And `d.isoformat()` is stored rather than the incoming string, so nothing
    like " 2026-08-05 " gets in.
    """
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise Refusal("invalid_date", f"{_The(field)} is missing.")
    cleaned = value.strip()
    if not _ISO_DATE.fullmatch(cleaned):
        raise Refusal(
            "invalid_date",
            f"I did not understand {_the(field)}. It is written like 2026-08-04 "
            "(year-month-day).",
        )
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise Refusal(
            "invalid_date", f"That {field} does not exist in the calendar."
        ) from exc
    return parsed.isoformat()


def date_not_future(value: object, field: str = "date") -> str:
    """A canonical date that cannot be ahead of today (in Bogota)."""
    iso = iso_date(value, field)
    if date.fromisoformat(iso) > clock.today():
        raise Refusal(
            "future_date",
            "I cannot record movements dated in the future. "
            f"Today is {clock.today_iso()}.",
        )
    return iso


def date_in_cycle(value: object, cycle_year: int, field: str = "date") -> str:
    """Inside the cycle (January to November of the current year) and not future.

    Without the lower bound, a saving dated 2025 would be added into the 2026
    payout, and next cycle the whole of 2026 would be counted again.
    """
    iso = date_not_future(value, field)
    parsed = date.fromisoformat(iso)
    if parsed.year != cycle_year:
        raise Refusal(
            "outside_cycle", f"That date is not part of the {cycle_year} cycle."
        )
    if parsed.month > 11:
        raise Refusal(
            "december_excluded",
            "December is not part of the cycle: the savings exist to be spent in "
            "December, so the cycle closes at the end of November.",
        )
    return iso


def cycle_month(value: object) -> int:
    n = integer(value, "month")
    if not 1 <= n <= 11:
        raise Refusal(
            "month_outside_cycle",
            "The cycle runs from January (1) to November (11). December does not "
            "count.",
        )
    return n


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


def normalize_name(value: str) -> str:
    """Lowercase, accents stripped, whitespace collapsed. For comparing names."""
    without_accents = "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(without_accents.lower().split())


def text(value: object, field: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise Refusal("invalid_text", f"{_The(field)} is missing.")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise Refusal("empty_text", f"{_The(field)} is missing.")
    if len(cleaned) > maximum:
        raise Refusal("text_too_long", f"What you put in {_the(field)} is too long.")
    return cleaned


def boolean(value: object, field: str) -> bool:
    """A yes/no, strictly. Anything ambiguous is refused rather than guessed.

    The old inline version treated "any string that is not no/false/0" as TRUE,
    so `paid="n"`, `"null"`, `"off"` and even `""` all marked a raffle ticket
    PAID. That is irreversible — there is no un-pay tool and nothing is ever
    deleted — so a coerced value permanently erased a member's debt and inflated
    the fund's income. A word we do not recognise is a question, not a yes.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("true", "yes", "y", "si", "sí", "1", "paid"):
            return True
        if cleaned in ("false", "no", "n", "0", "unpaid", "owes"):
            return False
    raise Refusal(
        "invalid_boolean",
        f"I need a clear yes or no for {_the(field)}.",
    )


def choice(value: object, field: str, valid: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip() not in valid:
        raise Refusal(
            "invalid_choice",
            f"{_The(field)} has to be one of these: {', '.join(valid)}.",
        )
    return value.strip()
