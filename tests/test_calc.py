"""Tests for the money arithmetic. No database, no network.

If anything here fails, Nati is telling a group of people the wrong numbers
about their own money. This is the part of the system that cannot be wrong.
"""

from __future__ import annotations

import pytest

from nati.tools import calc
from nati.tools.calc import Movement as M

# --------------------------------------------------------------------------
# percent
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "rate", "expected"),
    [
        (500, 3, 15),      # loan 1 in the seed
        (1_000, 4, 40),    # loan 2
        (300, 3, 9),       # loan 3
        (400, 5, 20),      # loan 4 (the administrator's own money)
        (800, 3, 24),      # loan 5
        (250, 2, 5),       # loan 6
        (600, 4, 24),      # loan 7
        (0, 5, 0),
        (100, 0, 0),       # an interest-free loan is valid
        (333, 3, 10),      # 9.99 rounds half up
        (350, 3, 11),      # 10.5 rounds half up
        (1, 50, 1),        # 0.5 -> 1
        (1, 49, 0),        # 0.49 -> 0
    ],
)
def test_percent(base: int, rate: int, expected: int) -> None:
    assert calc.percent(base, rate) == expected


def test_percent_always_returns_an_integer() -> None:
    for base in range(1, 2_000, 7):
        for rate in (1, 3, 7, 33, 99):
            assert isinstance(calc.percent(base, rate), int)


def test_percent_rejects_a_rate_out_of_range() -> None:
    with pytest.raises(ValueError, match="rate out of range"):
        calc.percent(100, 100)
    with pytest.raises(ValueError, match="rate out of range"):
        calc.percent(100, -1)


def test_an_invalid_date_explains_itself() -> None:
    with pytest.raises(ValueError, match="invalid date"):
        calc.to_date("04/08/2026")


# --------------------------------------------------------------------------
# loan_balance: interest first, then capital
# --------------------------------------------------------------------------


def test_a_loan_with_no_movements_owes_all_its_capital() -> None:
    b = calc.loan_balance(500, [])
    assert b.capital_outstanding == 500
    assert b.interest_outstanding == 0
    assert b.total == 500
    assert not b.is_paid_off


def test_a_payment_covers_interest_before_capital() -> None:
    b = calc.loan_balance(
        500,
        [
            M("2026-03-31", "interest", 15),
            M("2026-04-30", "interest", 15),
            M("2026-05-10", "payment", 100),
        ],
    )
    # $30 of overdue interest is covered first; $70 reduces capital.
    assert b.interest_paid == 30
    assert b.capital_paid == 70
    assert b.capital_outstanding == 430
    assert b.interest_outstanding == 0


def test_a_payment_smaller_than_the_interest_does_not_touch_capital() -> None:
    b = calc.loan_balance(
        500, [M("2026-03-31", "interest", 15), M("2026-04-01", "payment", 10)]
    )
    assert b.interest_paid == 10
    assert b.capital_paid == 0
    assert b.capital_outstanding == 500
    assert b.interest_outstanding == 5


def test_interest_on_the_same_day_is_charged_before_the_payment() -> None:
    """Otherwise a payment would cut capital while leaving that day's interest."""
    b = calc.loan_balance(
        100, [M("2026-04-30", "payment", 10), M("2026-04-30", "interest", 3)]
    )
    assert b.interest_paid == 3
    assert b.capital_paid == 7


def test_anas_loan_is_settled_exactly() -> None:
    """Loan 3 in the seed: $300 at 3%, three interest charges of $9, payments
    of 159 + 159 + 9 = $327 exactly."""
    b = calc.loan_balance(
        300,
        [
            M("2026-04-30", "interest", 9),
            M("2026-05-06", "payment", 159),
            M("2026-05-31", "interest", 9),
            M("2026-06-06", "payment", 159),
            M("2026-06-30", "interest", 9),
            M("2026-06-30", "payment", 9),
        ],
    )
    assert b.interest_charged == 27
    assert b.interest_paid == 27
    assert b.capital_paid == 300
    assert b.total == 0
    assert b.is_paid_off
    assert b.overpaid == 0


def test_overpayment_is_reported_not_absorbed() -> None:
    """Paying too much must be visible so a gate can refuse it."""
    b = calc.loan_balance(100, [M("2026-04-01", "payment", 150)])
    assert b.capital_paid == 100
    assert b.overpaid == 50
    assert b.total == 0


def test_input_order_does_not_change_the_result() -> None:
    """The calculation sorts by date: list order must not matter."""
    movements = [
        M("2026-05-10", "payment", 100),
        M("2026-03-31", "interest", 15),
        M("2026-07-10", "payment", 100),
        M("2026-04-30", "interest", 15),
        M("2026-06-30", "interest", 15),
    ]
    a = calc.loan_balance(500, movements)
    b = calc.loan_balance(500, list(reversed(movements)))
    assert a == b


def test_the_splits_explain_each_payment() -> None:
    b = calc.loan_balance(
        500,
        [
            M("2026-03-31", "interest", 15),
            M("2026-04-30", "interest", 15),
            M("2026-05-10", "payment", 100),
        ],
    )
    assert len(b.splits) == 1
    split = b.splits[0]
    assert (split.to_interest, split.to_capital, split.overpaid) == (30, 70, 0)
    assert split.to_interest + split.to_capital + split.overpaid == split.amount


def test_conservation_every_dollar_paid_is_assigned() -> None:
    """Invariant: no dollar paid gets lost along the way."""
    movements = [
        M("2026-03-31", "interest", 15),
        M("2026-04-15", "payment", 7),
        M("2026-04-30", "interest", 15),
        M("2026-05-10", "payment", 100),
        M("2026-06-30", "interest", 15),
        M("2026-07-01", "payment", 500),
    ]
    b = calc.loan_balance(500, movements)
    total_paid = sum(m.amount for m in movements if m.kind == "payment")
    assert b.interest_paid + b.capital_paid + b.overpaid == total_paid


def test_principal_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        calc.loan_balance(0, [])


# --------------------------------------------------------------------------
# Fund: interest charged is NOT cash
# --------------------------------------------------------------------------


def _seed_fund() -> calc.Fund:
    """Roughly the seed's shape, at the USD scale."""
    return calc.Fund(
        savings=15_220,
        capital_out=3_000,
        capital_recovered=480,
        payments_in=1_232,
        raffle_income=3_080,
        prizes_paid=800,
        interest_charged=477,
        interest_collected=353,
    )


def test_interest_charged_does_not_enter_the_cash() -> None:
    """The bug I made once: adding interest CHARGED as well as the payments.

    Interest reaches the cash box INSIDE a payment. Adding both counts it twice.
    """
    f = _seed_fund()
    expected = 15_220 - 3_000 + 1_232 + 3_080 - 800
    assert f.cash == expected
    # Proof it cannot creep back: moving the charged figure changes nothing.
    import dataclasses

    f2 = dataclasses.replace(f, interest_charged=99_999)
    assert f2.cash == f.cash


def test_profits_use_interest_collected_not_charged() -> None:
    """Money that has not arrived is not distributed."""
    f = _seed_fund()
    assert f.profits == f.interest_collected + f.raffle_profit
    assert f.profits < f.interest_charged + f.raffle_profit


def test_raffle_profit_can_be_negative() -> None:
    """If prizes exceed what was collected the raffle lost money. It must show."""
    f = calc.Fund(
        savings=1_000, capital_out=0, capital_recovered=0, payments_in=0,
        raffle_income=50, prizes_paid=150, interest_charged=0,
        interest_collected=0,
    )
    assert f.raffle_profit == -100


def test_loans_outstanding_is_what_is_out() -> None:
    f = _seed_fund()
    assert f.loans_outstanding == f.capital_out - f.capital_recovered
    assert f.equity == f.cash + f.loans_outstanding


# --------------------------------------------------------------------------
# Payout: equal profits, balanced to the dollar
# --------------------------------------------------------------------------


def test_profits_are_split_equally() -> None:
    """Confirmed by the administrator: profit is NOT proportional.

    Savings belong to each person, but the profit divides evenly.
    """
    members = [(1, "Ana", 100, 0, 0), (2, "Beto", 300, 0, 0)]
    p = calc.compute_payout(members, total_profits=40, cash_available=500)
    by_id = {s.member_id: s for s in p.shares}
    assert by_id[1].profit == 20   # half, even though they saved less
    assert by_id[2].profit == 20   # half
    # But each gets their own savings back.
    assert by_id[1].gross == 120
    assert by_id[2].gross == 320
    assert p.balances


def test_the_payout_balances_with_an_ugly_division() -> None:
    """Three members and 100 dollars: flooring would leave one dollar unowned."""
    members = [(1, "A", 500, 0, 0), (2, "B", 300, 0, 0), (3, "C", 100, 0, 0)]
    p = calc.compute_payout(members, total_profits=100, cash_available=10_000)
    assert sum(s.profit for s in p.shares) == 100
    assert p.balances
    assert sorted(s.profit for s in p.shares) == [33, 33, 34]


@pytest.mark.parametrize("profits", [0, 1, 7, 99, 100, 2_633])
def test_the_payout_always_balances(profits: int) -> None:
    members = [(1, "A", 150, 0, 0), (2, "B", 350, 0, 0), (3, "C", 77, 0, 0)]
    p = calc.compute_payout(members, profits, cash_available=1_000_000)
    assert sum(s.profit for s in p.shares) == profits
    assert p.balances


def test_the_payout_deducts_loan_debt() -> None:
    """Someone who owes the fund receives less: it is netted off."""
    members = [(1, "Ana", 400, 0, 0), (2, "Beto", 400, 150, 0)]
    p = calc.compute_payout(members, 80, 2_000)
    by_id = {s.member_id: s for s in p.shares}
    assert by_id[2].gross == 440
    assert by_id[2].net == 290
    assert by_id[1].net == 440


def test_raffle_debt_is_also_deducted() -> None:
    """The raffles are mandatory: an unpaid ticket is netted at payout time."""
    members = [(1, "Ana", 200, 0, 30), (2, "Beto", 200, 50, 10)]
    p = calc.compute_payout(members, 20, 1_000_000)
    ana = next(s for s in p.shares if s.member_id == 1)
    beto = next(s for s in p.shares if s.member_id == 2)
    assert ana.total_debt == 30
    assert ana.to_pay == 200 + 10 - 30
    assert beto.total_debt == 60
    assert beto.to_pay == 200 + 10 - 60


def test_nobody_is_paid_a_negative_amount() -> None:
    """If someone owes more than their share, they get 0 and still owe."""
    members = [(1, "Ana", 400, 0, 0), (2, "Beto", 100, 500, 0)]
    p = calc.compute_payout(members, 0, cash_available=1_000)
    beto = next(s for s in p.shares if s.member_id == 2)
    assert beto.net == -400          # their position is negative
    assert beto.to_pay == 0          # but nobody is paid negative
    assert beto.still_owes == 400
    ana = next(s for s in p.shares if s.member_id == 1)
    assert ana.to_pay == 400


def test_a_negative_position_does_not_reduce_what_must_be_paid_out() -> None:
    """The bug: comparing cash against total_net overstates capacity.

    One person's debt does not finance everyone else's payment.
    """
    members = [(1, "Ana", 900, 0, 0), (2, "Beto", 100, 900, 0)]
    p = calc.compute_payout(members, 0, cash_available=850)
    assert p.total_net == 100          # misleading: -800 offsets 900
    assert p.total_to_pay == 900       # what actually leaves
    assert p.total_still_owed == 800
    assert not p.cash_is_enough        # 850 < 900


def test_the_payout_warns_when_cash_is_short() -> None:
    """With money out on loan, cash may not cover the payout."""
    members = [(1, "Ana", 1_000, 0, 0)]
    p = calc.compute_payout(members, 100, cash_available=400)
    assert p.total_to_pay == 1_100
    assert not p.cash_is_enough
    assert calc.compute_payout(members, 100, 2_000).cash_is_enough


def test_a_payout_with_no_savings_still_splits_the_profits() -> None:
    members = [(1, "Ana", 0, 0, 0), (2, "Beto", 0, 0, 0)]
    p = calc.compute_payout(members, 50, 50)
    assert all(s.profit == 25 for s in p.shares)
    assert p.remainder == 0
    assert p.balances


def test_a_payout_with_no_members() -> None:
    p = calc.compute_payout([], 1_000, 1_000)
    assert p.shares == ()
    assert p.remainder == 1_000
    assert p.balances


def test_the_payout_rejects_negative_profits() -> None:
    with pytest.raises(ValueError, match="negative"):
        calc.compute_payout([(1, "A", 100, 0, 0)], -1, 0)


# --------------------------------------------------------------------------
# The leftover dollar must not always favour the same person
# --------------------------------------------------------------------------


def test_the_leftover_dollar_rotates_with_the_cycle() -> None:
    """It used to always go to member_id 1, who is the administrator.

    "The administrator always gets the leftover dollar" is exactly the thing
    nobody wants to have to explain in a meeting.
    """
    members = [(i, f"M{i}", 1_000, 0, 0) for i in range(1, 30)]  # 29 members

    def who(cycle: int) -> set[int]:
        p = calc.compute_payout(members, 100, 10_000_000, rotation_seed=cycle)
        top = max(s.profit for s in p.shares)
        return {s.member_id for s in p.shares if s.profit == top}

    a, b = who(2026), who(2027)
    assert a != b, "the rotation did not change from one cycle to the next"
    # And it stays deterministic: the same cycle gives the same result.
    assert who(2026) == a


def test_the_administrator_no_longer_always_gets_the_extra() -> None:
    members = [(i, f"M{i}", 1_000, 0, 0) for i in range(1, 30)]
    cycles_favouring_member_1 = 0
    for cycle in range(29):
        p = calc.compute_payout(members, 100, 10_000_000, rotation_seed=cycle)
        top = max(s.profit for s in p.shares)
        if any(s.member_id == 1 and s.profit == top for s in p.shares):
            cycles_favouring_member_1 += 1
    # It used to be 29 out of 29.
    assert cycles_favouring_member_1 < 29


def test_the_rotation_still_hands_out_the_same_number_of_dollars() -> None:
    members = [(i, f"M{i}", 1_000, 0, 0) for i in range(1, 30)]
    for cycle in (0, 2026, 2027, 9_999):
        p = calc.compute_payout(members, 100, 10_000_000, rotation_seed=cycle)
        assert sum(s.profit for s in p.shares) == 100
        assert p.balances


# --------------------------------------------------------------------------
# The real check: against independent figures
# --------------------------------------------------------------------------


def test_balances_only_looks_inward() -> None:
    """`Payout.balances` is internal consistency, not a real verification.

    Documented on purpose: for any Payout that `compute_payout` produces it is
    true by construction.
    """
    p = calc.compute_payout([(1, "A", 100, 0, 0), (2, "B", 200, 0, 0)], 77, 999_999)
    assert p.balances


def test_check_balance_detects_inflated_profits() -> None:
    """This is the check that CAN fail."""
    members = [(1, "A", 100, 0, 0), (2, "B", 300, 0, 0)]
    p = calc.compute_payout(members, 40, 999_999)

    good = calc.check_balance(p, savings_independent=400, profits_independent=40)
    assert good.ok, good.text

    # The database says there were only 39 dollars of profit: one is missing.
    bad = calc.check_balance(p, savings_independent=400, profits_independent=39)
    assert not bad.ok
    assert bad.difference == 1
    assert "profits" in bad.detail


def test_check_balance_detects_savings_that_do_not_match() -> None:
    p = calc.compute_payout([(1, "A", 100, 0, 0)], 10, 999_999)
    bad = calc.check_balance(p, savings_independent=250, profits_independent=10)
    assert not bad.ok
    assert "savings" in bad.detail
    assert "DOES NOT balance" in bad.text


# --------------------------------------------------------------------------
# Raffle
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n1", "n2", "winner", "wins"),
    [
        (7, 34, 34, True),    # either number can win
        (7, 34, 7, True),
        (7, 34, 12, False),
        (30, 99, 99, True),
        (0, 50, 0, True),     # 00 is a valid number
    ],
)
def test_ticket_wins(n1: int, n2: int, winner: int, wins: bool) -> None:
    assert calc.ticket_wins(n1, n2, winner) is wins


def test_a_quarterly_ticket_has_a_single_number() -> None:
    """REGRESSION: quarterly tickets carry number_2 = NULL and this crashed.

    It was a live TypeError comparing None against an integer, and the seeded
    world already contains quarterly draws.
    """
    assert calc.ticket_wins(45, None, 45) is True
    assert calc.ticket_wins(45, None, 46) is False


def test_a_number_out_of_range_fails() -> None:
    with pytest.raises(ValueError, match="out of range"):
        calc.ticket_wins(7, 34, 100)
