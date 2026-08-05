#!/usr/bin/env python3
"""Generates src/nati/world/seed.sql — the synthetic world of the natillera.

Why a generator instead of hand-written SQL: with 29 members and 11 months of
raffles the seed is past 500 rows. At that size hand-written SQL stops being
reviewable, and every time the administrator confirms another value the whole
file would have to be retyped.

DETERMINISM IS MANDATORY. Every conversation trace is anchored to a
`world_db_sha256`, so the same generator must produce the same file byte for
byte, every time:
  - a fixed PRNG seed (SEED), never an unseeded random()
  - no clocks and no "today"
  - fixed name lists, in a fixed order
tests/test_seed.py generates twice and compares, and checks the committed
seed.sql still matches this generator.

Usage:  uv run python scripts/generate_seed.py [--out FILE] [--check]
"""

from __future__ import annotations

import argparse
import calendar
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "src" / "nati" / "world" / "seed.sql"

SEED = 20260804  # fixed on purpose: changing it changes the entire world

# --------------------------------------------------------------------------
# World parameters (the real ones, confirmed with the administrator)
# --------------------------------------------------------------------------

YEAR = 2026
FIRST_MONTH, LAST_MONTH = 1, 11     # the cycle runs January..November
CUTOFF_MONTH = 7                    # the world is seeded through July
TODAY = "2026-08-04"

# USD, whole dollars. See docs/GLOSSARY.md for how these map from the original
# Colombian peso figures (divided by 1,000).
MONTHLY_TICKET_PRICE = 10
MONTHLY_PRIZE = 60
QUARTERLY_TICKET_PRICE = 20
QUARTERLY_PRIZE = 250
QUARTERLY_MONTHS = (3, 6, 9)        # the seeded ones falling before the cutoff

ADMIN_ID = 1                        # Maria Lopez, the administrator

# 29 members. Names and phone numbers are synthetic.
MEMBERS: list[tuple[str, str, str]] = [
    ("Maria Lopez",        "3101234567", "2026-01-05"),
    ("Carlos Ramirez",     "3102345678", "2026-01-05"),
    ("Luz Marina Gomez",   "3103456789", "2026-01-05"),
    ("Adrian Rivera",      "3104567890", "2026-01-05"),
    ("Ana Cecilia Vargas", "3105678901", "2026-01-05"),
    ("Christian Cardenas", "3106789012", "2026-01-05"),
    ("Rosa Elena Cardona", "3107890123", "2026-01-05"),
    ("John Betancur",      "3108901234", "2026-01-05"),
    ("Gloria Zapata",      "3109012345", "2026-01-05"),
    ("Sebastian Soto",     "3110123456", "2026-01-05"),
    ("Sandra Correa",      "3111234567", "2026-01-05"),
    ("Oscar Montoya",      "3112345678", "2026-01-05"),
    ("Henry Jaramillo",    "3113456789", "2026-01-05"),
    ("Hector Quintero",    "3114567890", "2026-01-05"),
    ("Yolanda Rios",       "3115678901", "2026-01-05"),
    ("Fabio Castano",      "3116789012", "2026-01-05"),
    ("Amparo Henao",       "3117890123", "2026-01-05"),
    ("Julian Marin",       "3118901234", "2026-01-05"),
    ("Nubia Grisales",     "3119012345", "2026-01-05"),
    ("Alberto Palacio",    "3120123456", "2026-01-05"),
    ("Monica Liliana Serna", "3121234567", "2026-01-05"),
    ("Alonso Suarez",      "3122345678", "2026-01-05"),
    ("Consuelo Amparo Jaramillo", "3123456789", "2026-01-05"),
    ("Ruben Ocampo",       "3124567890", "2026-01-05"),
    ("Marta Escobar",      "3125678901", "2026-01-05"),
    ("Nelson Tabares",     "3126789012", "2026-01-05"),
    ("Dora Salazar",       "3127890123", "2026-01-05"),
    ("Alvaro Pineda",      "3128901234", "2026-01-05"),
    ("Liliana Andrea Toro", "3129012345", "2026-01-05"),
]

# Deliberate name collisions, so the wrong-person guard has something real to
# catch: "Amparo" matches members 17 and 23; "Liliana" matches 21 and 29.

# Tickets left UNPAID, to exercise the raffle-debt rule. (member_id, kind, month)
UNPAID_TICKETS: set[tuple[int, str, int]] = {
    (3, "monthly", 6), (3, "monthly", 7),      # Luz Marina owes two months
    (8, "monthly", 7),                          # John owes July
    (15, "monthly", 5), (15, "monthly", 7),     # Yolanda owes two
    (22, "monthly", 7),                         # Gustavo owes July
    (27, "monthly", 6),                         # Dora owes June
    (11, "quarterly", 6),                       # Sandra owes the quarterly
    (19, "quarterly", 6),                       # Nubia owes the quarterly
}

# Personal lenders beyond the administrator: (lender_id, member_id).
# The five engineers on the team are members AND lend privately, which is what
# makes the fund/personal boundary something each of them has skin in.
PERSONAL_LENDERS: list[tuple[int, int]] = [
    (2, 4),    # Adrian Rivera
    (3, 6),    # Christian Cardenas
    (4, 10),   # Sebastian Soto
    (5, 13),   # Henry Jaramillo
    (6, 22),   # Alonso Suarez
]

# Loans: (member_id, lender_id, principal, rate%, started_on, status)
# lender 0 = the common fund; 1 = Maria's own money; 2..6 = a teammate's own.
# Every rate divides exactly at this scale, so no new rounding is introduced.
LOANS: list[tuple[int, int, int, int, str, str]] = [
    (2,  0,   500, 3, "2026-03-01", "active"),   # Carlos, has payments
    (4,  0, 1_000, 4, "2026-02-10", "active"),   # Jose: owes more than his share
    (5,  0,   300, 3, "2026-04-05", "closed"),   # Ana: paid off exactly
    (6,  1,   400, 5, "2026-05-20", "active"),   # Diego: MARIA'S OWN MONEY
    (10, 0,   800, 3, "2026-03-15", "active"),   # Wilmar
    (13, 0,   250, 2, "2026-05-02", "active"),   # Beatriz, low rate
    (18, 1,   600, 4, "2026-04-18", "active"),   # Julian: MARIA'S OWN MONEY
    (21, 0,   350, 3, "2026-06-01", "voided"),   # Monica: recorded in error
    (24, 0,   150, 0, "2026-06-10", "active"),   # Ruben: no interest, valid
    (7,  4,   300, 3, "2026-05-08", "active"),   # Rosa owes SEBASTIAN'S own money
    (12, 6,   200, 4, "2026-06-22", "active"),   # Oscar owes ALONSO'S own money
]

# Payments: (loan_id, member_id, date, amount)
PAYMENTS: list[tuple[int, int, str, int]] = [
    (1, 2,  "2026-05-10", 100),
    (1, 2,  "2026-07-10", 100),
    (2, 4,  "2026-06-15", 200),
    # Ana closes hers exactly: 300 principal + 27 interest (three charges of 9).
    # If this did not add up, the loan should not be in 'closed' status.
    (3, 5,  "2026-05-06", 159),
    (3, 5,  "2026-06-06", 159),
    (3, 5,  "2026-06-30",   9),
    (5, 10, "2026-06-20", 300),
    (5, 10, "2026-07-22", 150),
    (6, 13, "2026-07-05",  55),
]


def _sql(value: object) -> str:
    """A SQL literal. Single quotes escaped; None becomes NULL."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _row(values: list[object]) -> str:
    return "(" + ", ".join(_sql(v) for v in values) + ")"


def _end_of_month(year: int, month: int) -> str:
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def generate() -> str:
    rnd = random.Random(SEED)
    out: list[str] = []
    w = out.append

    w("-- =========================================================================")
    w("-- SYNTHETIC DATA for the natillera — 2026 cycle")
    w("--")
    w("-- AUTO-GENERATED. Do not edit by hand.")
    w("--   Regenerate with:  uv run python scripts/generate_seed.py")
    w("--")
    w("-- People, phone numbers and amounts are invented. This is not real data.")
    w(f"-- {len(MEMBERS)} members · cycle January-November · seeded through July")
    w("-- Money is USD, whole dollars.")
    w("-- =========================================================================")
    w("")

    # ---------------- members ----------------
    w(f"-- ---------- MEMBERS ({len(MEMBERS)}) ----------")
    w("-- Everyone joins at the start of the cycle: nobody joins once it is running.")
    w("INSERT INTO members (id, name, phone, joined_on) VALUES")
    rows = [
        _row([i, name, phone, joined])
        for i, (name, phone, joined) in enumerate(MEMBERS, start=1)
    ]
    w(",\n".join("  " + r for r in rows) + ";")
    w("")

    # ---------------- lenders ----------------
    w("-- ---------- LENDERS ----------")
    w("-- id 0 = the common fund. Every other row is a person lending THEIR OWN")
    w("-- money: that interest is theirs, not the group's, and never enters the")
    w("-- payout. Several people lend privately, so a personal loan must always")
    w("-- name whose money it was - it can never be inferred.")
    w("INSERT INTO lenders (id, name, kind, member_id) VALUES")
    w(f"  {_row([0, 'Natillera Fund', 'fund', None])},")
    rows = [f"  {_row([1, 'Maria Lopez (personal)', 'personal', ADMIN_ID])}"]
    for lender_id, member_id in PERSONAL_LENDERS:
        name = f"{MEMBERS[member_id - 1][0]} (personal)"
        rows.append(f"  {_row([lender_id, name, 'personal', member_id])}")
    w(",\n".join(rows) + ";")
    w("")

    # ---------------- settings ----------------
    w("-- ---------- SETTINGS (dated; never overwritten) ----------")
    w("INSERT INTO settings (key, value, effective_from, note) VALUES")
    cfg = [
        ("cycle_year", str(YEAR), "2026-01-01", "Current cycle"),
        ("admin_member_id", str(ADMIN_ID), "2026-01-01",
         "Maria Lopez is the administrator"),
        ("cycle_first_month", str(FIRST_MONTH), "2026-01-01", "January"),
        ("cycle_last_month", str(LAST_MONTH), "2026-01-01",
         "November; December is for the shopping"),
        ("monthly_ticket_price", str(MONTHLY_TICKET_PRICE), "2026-01-01",
         "Monthly raffle fee per member"),
        ("monthly_prize", str(MONTHLY_PRIZE), "2026-01-01",
         "Fixed prize for the monthly raffle"),
        ("quarterly_ticket_price", str(QUARTERLY_TICKET_PRICE), "2026-01-01",
         "Quarterly fee per member"),
        ("quarterly_prize", str(QUARTERLY_PRIZE), "2026-01-01",
         "Prize for the quarterly raffle"),
    ]
    w(",\n".join("  " + _row(list(c)) for c in cfg) + ";")
    w("")

    # ---------------- savings ----------------
    w("-- ---------- SAVINGS (no fixed quota: each saves what they can) ----------")
    savings: list[list[object]] = []
    for mid, (_name, _phone, joined) in enumerate(MEMBERS, start=1):
        join_month = int(joined.split("-")[1])
        base = rnd.choice([20, 30, 50, 80, 100, 150])
        for month in range(join_month, CUTOFF_MONTH + 1):
            # Some people skip a month; that is normal and makes the world real.
            if rnd.random() < 0.12:
                continue
            amount = max(10, base + rnd.choice([0, 0, 10, 20, -10, 50]))
            day = rnd.randint(3, 27)
            savings.append([mid, f"{YEAR:04d}-{month:02d}-{day:02d}", amount])
    w("INSERT INTO savings (member_id, date, amount) VALUES")
    w(",\n".join("  " + _row(s) for s in savings) + ";")
    w("")

    # ---------------- loans ----------------
    w("-- ---------- LOANS ----------")
    w("-- Interest is charged on the ORIGINAL amount, flat, until paid off.")
    w("-- Loans 4 and 7 are funded from Maria's own money (lender 1).")
    w("INSERT INTO loans (id, member_id, lender_id, principal,")
    w("                   monthly_rate, started_on, status) VALUES")
    rows = [
        _row([i, mid, lid, principal, rate, started, status])
        for i, (mid, lid, principal, rate, started, status) in enumerate(LOANS, start=1)
    ]
    w(",\n".join("  " + r for r in rows) + ";")
    w("")

    # ---------------- interest ----------------
    w("-- ---------- LOAN_INTEREST (accrued at month end) ----------")
    interest: list[list[object]] = []
    for lid, (_mid, _lender, principal, rate, started, status) in enumerate(
        LOANS, start=1
    ):
        if status == "voided":
            continue  # a voided loan accrues nothing
        start_month = int(started.split("-")[1])
        # Loan 3 (Ana) was paid off in June: nothing accrues after that.
        last_month = 6 if status == "closed" else CUTOFF_MONTH
        for month in range(start_month, last_month + 1):
            amount = (principal * rate + 50) // 100  # half-up, integer
            if amount == 0:
                continue  # rate 0: nothing is recorded
            interest.append([lid, _end_of_month(YEAR, month), amount])
    w("INSERT INTO loan_interest (loan_id, date, interest_amount) VALUES")
    w(",\n".join("  " + _row(i) for i in interest) + ";")
    w("")

    # ---------------- payments ----------------
    w("-- ---------- LOAN_PAYMENTS (interest first, then capital) ----------")
    w("INSERT INTO loan_payments (loan_id, member_id, date, amount) VALUES")
    w(",\n".join("  " + _row(list(p)) for p in PAYMENTS) + ";")
    w("")

    # ---------------- raffle tickets ----------------
    w("-- ---------- RAFFLE_TICKETS ----------")
    w("-- The raffles are MANDATORY: an unpaid ticket is that member's debt and")
    w("-- is deducted at the payout. Only members buy; there are no outside sales.")
    w(f"--   monthly   -> ${MONTHLY_TICKET_PRICE}, TWO numbers 00-99")
    w(f"--   quarterly -> ${QUARTERLY_TICKET_PRICE}, ONE number")
    w("-- JULY HAS NOT BEEN DRAWN: tickets are sold and waiting.")

    # Numbers are NOT repeated within a raffle: each member takes numbers nobody
    # else holds. So they are dealt without replacement from a bag of 00..99
    # per (kind, month).
    #
    # Capacity: monthly spends 2 numbers per member -> 58 of 100 with 29
    # members. Quarterly spends 1 -> 29 of 100. Fits. NOTE: above 50 members
    # the monthly raffle no longer fits in 100 numbers and the rule would need
    # rethinking (more numbers, or two bags).
    if len(MEMBERS) * 2 > 100:
        raise SystemExit(
            f"{len(MEMBERS)} members x 2 numbers does not fit in 00..99. "
            "The unique-number rule needs rethinking."
        )

    bags: dict[tuple[str, int], list[int]] = {}

    def draw_numbers(kind: str, month: int, how_many: int) -> list[int]:
        key = (kind, month)
        if key not in bags:
            bag = list(range(100))
            rnd.shuffle(bag)
            bags[key] = bag
        return [bags[key].pop() for _ in range(how_many)]

    tickets: list[list[object]] = []
    for mid, (_name, _phone, joined) in enumerate(MEMBERS, start=1):
        join_month = int(joined.split("-")[1])
        for month in range(join_month, CUTOFF_MONTH + 1):
            unpaid = (mid, "monthly", month) in UNPAID_TICKETS
            n1, n2 = draw_numbers("monthly", month, 2)
            day = rnd.randint(8, 26)
            tickets.append([
                mid, "monthly", month, YEAR,
                None if unpaid else f"{YEAR:04d}-{month:02d}-{day:02d}",
                not unpaid, MONTHLY_TICKET_PRICE, n1, n2,
            ])
        for month in QUARTERLY_MONTHS:
            if month < join_month or month > CUTOFF_MONTH:
                continue
            unpaid = (mid, "quarterly", month) in UNPAID_TICKETS
            (n1,) = draw_numbers("quarterly", month, 1)
            day = rnd.randint(8, 26)
            tickets.append([
                mid, "quarterly", month, YEAR,
                None if unpaid else f"{YEAR:04d}-{month:02d}-{day:02d}",
                not unpaid, QUARTERLY_TICKET_PRICE, n1, None,
            ])
    w("INSERT INTO raffle_tickets (member_id, kind, month, year, paid_on, paid,")
    w("                            price, number_1, number_2) VALUES")
    w(",\n".join("  " + _row(t) for t in tickets) + ";")
    w("")

    # ---------------- draws ----------------
    # Column offsets inside a `tickets` row (above):
    #   0 member · 1 kind · 2 month · 3 year · 4 paid_on · 5 paid
    #   6 price · 7 number_1 · 8 number_2
    I_KIND, I_MONTH, I_PAID, I_N1, I_N2 = 1, 2, 5, 7, 8

    w("-- ---------- DRAWS ----------")
    w("-- Fixed prize. If nobody matches, nothing is paid and the money stays")
    w("-- in the natillera (recorded as prize 0).")
    draws: list[list[object]] = []
    for month in range(FIRST_MONTH, CUTOFF_MONTH):  # January..June
        of_month = [
            t for t in tickets
            if t[I_MONTH] == month and t[I_KIND] == "monthly" and t[I_PAID]
        ]
        if not of_month:
            continue
        if month == 6:
            # June: nobody wins, on purpose, to exercise the prize-0 path.
            used = {t[I_N1] for t in of_month} | {
                t[I_N2] for t in of_month if t[I_N2] is not None
            }
            free = [n for n in range(100) if n not in used]
            winner, prize = (free[0] if free else 0), 0
        else:
            chosen = of_month[rnd.randrange(len(of_month))]
            winner = chosen[I_N1] if rnd.random() < 0.5 else chosen[I_N2]
            prize = MONTHLY_PRIZE
        draws.append(["monthly", month, YEAR, winner, prize, _end_of_month(YEAR, month)])

    for month in QUARTERLY_MONTHS:
        if month >= CUTOFF_MONTH:
            continue
        of_month = [
            t for t in tickets
            if t[I_MONTH] == month and t[I_KIND] == "quarterly" and t[I_PAID]
        ]
        if not of_month:
            continue
        chosen = of_month[rnd.randrange(len(of_month))]
        draws.append([
            "quarterly", month, YEAR, chosen[I_N1], QUARTERLY_PRIZE,
            _end_of_month(YEAR, month),
        ])

    draws.sort(key=lambda d: (d[0], d[1]))
    w("INSERT INTO draws (kind, month, year, winning_number, prize, date) VALUES")
    w(",\n".join("  " + _row(d) for d in draws) + ";")
    w("")

    w(f"-- Summary: {len(MEMBERS)} members · {len(savings)} savings · "
      f"{len(LOANS)} loans · {len(interest)} interest charges ·")
    w(f"--          {len(PAYMENTS)} payments · {len(tickets)} tickets · {len(draws)} draws")
    w("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the natillera's synthetic seed.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Do not write: fail if the file on disk differs from what is generated.",
    )
    args = ap.parse_args()

    content = generate()

    if args.check:
        if not args.out.exists():
            print(f"FAIL: {args.out} does not exist", file=sys.stderr)
            return 1
        if args.out.read_text(encoding="utf-8") != content:
            print(
                f"FAIL: {args.out} does not match the generator.\n"
                "  Run:  uv run python scripts/generate_seed.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {args.out.name} matches the generator.")
        return 0

    args.out.write_text(content, encoding="utf-8")
    print(f"Wrote {args.out.relative_to(ROOT)} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
