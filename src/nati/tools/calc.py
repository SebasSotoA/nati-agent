"""All of the natillera's money arithmetic. Pure functions, no database.

This module is the money chokepoint: **the model never does arithmetic**. If a
dollar figure appears in one of Nati's replies, it came from here. That is what
turns most of the SOP into something code can enforce instead of something the
model has to remember.

Conventions:
  - US dollars as whole integers. There are no cents.
  - Dates as ISO 'YYYY-MM-DD'.
  - Explicit half-up rounding, never floats for money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------


def to_date(iso: str) -> date:
    """Parse 'YYYY-MM-DD'. Raises ValueError with a useful message if it fails."""
    try:
        return date.fromisoformat(iso)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid date {iso!r}; expected 'YYYY-MM-DD'") from exc


def percent(base: int, rate: int) -> int:
    """`rate` percent of `base`, in whole dollars, rounded half up.

    Integer arithmetic on purpose: 333 at 3% is 9.99 → 10, with no float in
    sight.
    """
    if base < 0:
        raise ValueError("the base cannot be negative")
    if not 0 <= rate <= 99:
        raise ValueError(f"rate out of range: {rate} (expected 0..99)")
    return (base * rate + 50) // 100


# --------------------------------------------------------------------------
# A loan's balance: interest-first application
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Movement:
    """One event in a loan's life: interest charged, or a payment received."""

    date: str
    kind: str  # 'interest' | 'payment'
    amount: int


@dataclass(frozen=True)
class PaymentSplit:
    """How one payment was divided. Used to explain it to the administrator."""

    date: str
    amount: int
    to_interest: int
    to_capital: int
    overpaid: int


@dataclass(frozen=True)
class LoanBalance:
    """The complete state of a loan at a date."""

    capital_outstanding: int
    interest_outstanding: int
    interest_charged: int
    interest_paid: int
    capital_paid: int
    overpaid: int
    splits: tuple[PaymentSplit, ...] = field(default=())

    @property
    def total(self) -> int:
        """What is left to pay: capital plus overdue interest."""
        return self.capital_outstanding + self.interest_outstanding

    @property
    def is_paid_off(self) -> bool:
        return self.total == 0


def loan_balance(principal: int, movements: list[Movement]) -> LoanBalance:
    """Walks the movements in order, applying **interest first, then capital**.

    This is the rule the administrator confirmed. It is applied in chronological
    order because interest accrues between payments: a payment can only cover
    interest that already existed when it arrived.

    Overpayment (paying more than the balance) is reported but not absorbed — a
    tool gate should refuse that payment before it ever gets here.
    """
    if principal <= 0:
        raise ValueError("principal must be positive")

    capital = principal
    interest_due = 0
    interest_charged = 0
    interest_paid = 0
    capital_paid = 0
    overpaid_total = 0
    splits: list[PaymentSplit] = []

    # Stable order: by date, and on a tie interest is charged before a payment
    # (otherwise a payment would reduce capital while leaving that same day's
    # interest outstanding).
    order = {"interest": 0, "payment": 1}
    for movement in sorted(
        movements, key=lambda m: (to_date(m.date), order.get(m.kind, 9))
    ):
        if movement.amount < 0:
            raise ValueError(f"negative amount in movement {movement}")

        if movement.kind == "interest":
            interest_due += movement.amount
            interest_charged += movement.amount
            continue

        if movement.kind != "payment":
            raise ValueError(f"unknown movement kind: {movement.kind!r}")

        to_interest = min(movement.amount, interest_due)
        interest_due -= to_interest
        interest_paid += to_interest

        remainder = movement.amount - to_interest
        to_capital = min(remainder, capital)
        capital -= to_capital
        capital_paid += to_capital

        left_over = remainder - to_capital
        overpaid_total += left_over
        splits.append(
            PaymentSplit(
                date=movement.date,
                amount=movement.amount,
                to_interest=to_interest,
                to_capital=to_capital,
                overpaid=left_over,
            )
        )

    return LoanBalance(
        capital_outstanding=capital,
        interest_outstanding=interest_due,
        interest_charged=interest_charged,
        interest_paid=interest_paid,
        capital_paid=capital_paid,
        overpaid=overpaid_total,
        splits=tuple(splits),
    )


# --------------------------------------------------------------------------
# The common fund
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fund:
    """The state of the natillera's fund. Fund money only (lender 0).

    One critical distinction, and the source of a mistake I made once:
    interest **charged** is not cash, it is a receivable. Money reaches the cash
    box when a payment arrives, and that payment already includes its interest
    portion. Adding both counts it twice.
    """

    savings: int
    capital_out: int
    capital_recovered: int
    payments_in: int
    raffle_income: int
    prizes_paid: int
    interest_charged: int
    interest_collected: int

    @property
    def cash(self) -> int:
        """Money actually available."""
        return (
            self.savings
            - self.capital_out
            + self.payments_in
            + self.raffle_income
            - self.prizes_paid
        )

    @property
    def loans_outstanding(self) -> int:
        """Capital that is out, in the hands of the fund's borrowers."""
        return self.capital_out - self.capital_recovered

    @property
    def raffle_profit(self) -> int:
        return self.raffle_income - self.prizes_paid

    @property
    def profits(self) -> int:
        """Distributable profit: interest COLLECTED plus the raffle's net.

        Collected, not charged: money that has not arrived is not distributed.
        Interest charged but uncollected carries into the next cycle.
        """
        return self.interest_collected + self.raffle_profit

    @property
    def equity(self) -> int:
        """Everything that belongs to the members: cash plus loans outstanding."""
        return self.cash + self.loans_outstanding


# --------------------------------------------------------------------------
# Year-end payout
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberShare:
    member_id: int
    name: str
    saved: int
    profit: int
    loan_debt: int
    ticket_debt: int = 0

    @property
    def total_debt(self) -> int:
        return self.loan_debt + self.ticket_debt

    @property
    def gross(self) -> int:
        return self.saved + self.profit

    @property
    def net(self) -> int:
        """Their net position. Can be NEGATIVE if they owe more than they have."""
        return self.gross - self.total_debt

    @property
    def to_pay(self) -> int:
        """Money leaving the cash box for this person. Never negative.

        Nobody can be "paid" a negative amount: if they owe more than their
        share is worth, they receive nothing and still owe the difference.
        """
        return max(0, self.net)

    @property
    def still_owes(self) -> int:
        """What they still owe after their savings and profit are applied."""
        return max(0, -self.net)


@dataclass(frozen=True)
class Payout:
    shares: tuple[MemberShare, ...]
    total_profits: int
    total_savings: int
    cash_available: int
    remainder: int

    @property
    def total_gross(self) -> int:
        return sum(s.gross for s in self.shares)

    @property
    def total_net(self) -> int:
        """Aggregate net position. Includes negatives; not what leaves the box."""
        return sum(s.net for s in self.shares)

    @property
    def total_to_pay(self) -> int:
        """Money that actually leaves the cash box. This is what must be there."""
        return sum(s.to_pay for s in self.shares)

    @property
    def total_still_owed(self) -> int:
        """Still to collect from those who owe more than their share."""
        return sum(s.still_owes for s in self.shares)

    @property
    def balances(self) -> bool:
        """Internal consistency: every dollar of profit was assigned to someone.

        Note what this does NOT do: it only looks inside this object. For any
        Payout that `compute_payout` produces it is true by construction, so it
        CANNOT detect that the profits were computed wrongly further upstream.

        The real check is `check_balance()`, which re-adds against independent
        sums from the database.
        """
        assigned = sum(s.profit for s in self.shares)
        return assigned + self.remainder == self.total_profits

    @property
    def cash_is_enough(self) -> bool:
        """Is there cash for the real payments? If not, collect the loans first.

        Compared against `total_to_pay`, not `total_net`: a member with a
        negative position does not reduce the money that must be handed to
        everyone else.
        """
        return self.cash_available >= self.total_to_pay


def compute_payout(
    members: list[tuple[int, str, int, int, int]],
    total_profits: int,
    cash_available: int,
    *,
    rotation_seed: int = 0,
) -> Payout:
    """Year-end payout. `members` is (id, name, saved, loan_debt, ticket_debt).

    Two different rules, both confirmed by the administrator:

      - SAVINGS belong to each person: they get their own money back. Whoever
        saved more receives more, because it is their money.
      - PROFITS are split EQUALLY among all members, regardless of how much
        anyone saved.

    Leftover dollars from the division are handed out one at a time so the sum
    balances **exactly**: 308 split among 8 is clean, 100 among 3 is not.
    Without this the invariant "sum of shares = savings + profits" would fail on
    rounding, which is precisely the kind of discrepancy that makes a savings
    group stop trusting the books.
    """
    if total_profits < 0:
        raise ValueError("profits cannot be negative")

    total_savings = sum(m[2] for m in members)

    if not members:
        return Payout(
            shares=(),
            total_profits=total_profits,
            total_savings=total_savings,
            cash_available=cash_available,
            remainder=total_profits,
        )

    count = len(members)
    base, leftover = divmod(total_profits, count)

    # Dollars left over by the division are handed out one each. Since everyone
    # receives the same base amount there is no "largest remainder" to break the
    # tie, so somebody has to be chosen.
    #
    # It used to be the lowest member ids — which starts at 1, the
    # administrator. "The administrator always gets the leftover dollar" is
    # exactly the thing nobody wants to have to explain in a meeting.
    #
    # Now the list rotates with the cycle: still fully deterministic (the same
    # world produces the same payout), but the favour moves each year.
    ids = sorted(m[0] for m in members)
    start = (rotation_seed % count) if count else 0
    rotated = ids[start:] + ids[:start]
    gets_extra = set(rotated[:leftover])

    shares = tuple(
        MemberShare(
            member_id=member_id,
            name=name,
            saved=saved,
            profit=base + (1 if member_id in gets_extra else 0),
            loan_debt=loan_debt,
            ticket_debt=ticket_debt,
        )
        for member_id, name, saved, loan_debt, ticket_debt in members
    )

    return Payout(
        shares=shares,
        total_profits=total_profits,
        total_savings=total_savings,
        cash_available=cash_available,
        remainder=0,
    )


@dataclass(frozen=True)
class BalanceCheck:
    """The result of re-adding a payout against independent figures."""

    ok: bool
    expected: int
    computed: int
    difference: int
    detail: str

    @property
    def text(self) -> str:
        if self.ok:
            return f"balances: {self.computed}"
        return (
            f"DOES NOT balance: expected {self.expected}, computed {self.computed} "
            f"(difference {self.difference}) — {self.detail}"
        )


def check_balance(
    payout: Payout, savings_independent: int, profits_independent: int
) -> BalanceCheck:
    """Re-adds the payout against sums that did NOT come from `compute_payout`.

    This is the check that can actually fail. `Payout.balances` only looks inside
    the object; here the total saved and the profits are summed separately
    (straight from the database) and compared against what the payout claims to
    distribute.

    If this fails, somebody invented money or lost it along the way.
    """
    expected = savings_independent + profits_independent
    computed = payout.total_gross + payout.remainder
    difference = computed - expected

    problems: list[str] = []
    if payout.total_savings != savings_independent:
        problems.append(
            f"savings: payout says {payout.total_savings}, "
            f"the database says {savings_independent}"
        )
    if payout.total_profits != profits_independent:
        problems.append(
            f"profits: payout says {payout.total_profits}, "
            f"the database says {profits_independent}"
        )
    assigned = sum(s.profit for s in payout.shares)
    if assigned + payout.remainder != payout.total_profits:
        problems.append(
            f"assigned profit {assigned} + remainder {payout.remainder} "
            f"!= total {payout.total_profits}"
        )

    return BalanceCheck(
        ok=difference == 0 and not problems,
        expected=expected,
        computed=computed,
        difference=difference,
        detail="; ".join(problems) if problems else "no differences",
    )


# --------------------------------------------------------------------------
# Raffle
# --------------------------------------------------------------------------


def ticket_wins(number_1: int, number_2: int | None, winning_number: int) -> bool:
    """A ticket wins if any of its numbers is the winning one.

    `number_2` is None for the quarterly raffle, which carries a single number.
    This used to blow up with a TypeError comparing None against an integer —
    and the seeded world already contains quarterly draws, so it was a live
    crash.
    """
    for n in (number_1, number_2, winning_number):
        if n is None:
            continue
        if not 0 <= n <= 99:
            raise ValueError(f"number out of range 00-99: {n}")
    return winning_number == number_1 or (
        number_2 is not None and winning_number == number_2
    )
