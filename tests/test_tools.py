"""Tests for the tool layer: gates, envelope, and the 12 tools.

The aim is not to restate each gate's code, but to catch the regressions that
would cost money or reveal the controls:

- that an odd type (bool, float, text) never reaches the database;
- that the internal `reason` NEVER travels to the model;
- that no refusal leaks a figure (that would be an unaudited read);
- that the administrator's own money never mixes with the fund;
- that a refusal leaves the world exactly as it was.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nati import clock
from nati.tools import gates, registry
from nati.tools.envelope import usd
from nati.world import db

CYCLE_DAY = "2026-08-04"


@pytest.fixture(autouse=True)
def frozen_clock():
    """The whole module runs with the clock pinned to the seeded world."""
    with clock.frozen(CYCLE_DAY):
        yield


@pytest.fixture
def con(tmp_path: Path):
    path = tmp_path / "world.db"
    db.create(path, reset=True)
    connection = db.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def ctx() -> db.Ctx:
    return db.Ctx(conv_id="test-tools", seq=1)


def call(con, ctx, _tool: str, /, **args):
    # positional-only: `add_member` has an argument called `name`, which would
    # otherwise collide with this parameter.
    return registry.dispatch(con, _tool, args, ctx)


# --------------------------------------------------------------------------
# Envelope contracts
# --------------------------------------------------------------------------


def test_every_tool_has_a_valid_schema() -> None:
    """`parameters` must be a JSON OBJECT, not a string.

    Passing a string is the most common cause of a rejected tool definition, on
    any provider.
    """
    for schema in registry.schemas():
        fn = schema["function"]
        assert schema["type"] == "function"
        assert isinstance(fn["parameters"], dict)
        assert fn["parameters"]["type"] == "object"
        assert isinstance(fn["parameters"]["properties"], dict)
        assert isinstance(fn["description"], str) and fn["description"]


def test_there_are_nineteen_tools() -> None:
    assert len(registry.TOOLS) == 19
    assert sum(1 for t in registry.TOOLS if t.writes) == 11


def test_the_schema_hash_is_stable() -> None:
    """It anchors each trace to the exact tool version the model saw."""
    assert registry.schemas_hash() == registry.schemas_hash()
    assert len(registry.schemas_hash()) == 64


def test_all_tool_names_are_english() -> None:
    """Teammates read these names in traces."""
    for tool in registry.TOOLS:
        assert tool.name.isascii()
        assert tool.name == tool.name.lower()
        assert " " not in tool.name


@pytest.mark.parametrize("tool", [t.name for t in registry.TOOLS])
def test_no_tool_returns_empty(con, ctx, tool: str) -> None:
    """Some models reject an empty tool result, even for "I found nothing"."""
    r = call(con, ctx, tool)  # no arguments: many will fail, which is fine
    assert r.for_the_model, f"{tool} returned an empty envelope"
    assert "ok" in r.for_the_model
    assert r.for_the_model.get("message"), f"{tool} returned no sentence"


def test_a_search_with_no_results_still_returns_a_sentence(con, ctx) -> None:
    r = call(con, ctx, "members", search="zzzzz")
    assert r.for_the_model["ok"] is True
    assert r.for_the_model["count"] == 0
    assert r.for_the_model["message"]


def test_the_reason_never_travels_to_the_model(con, ctx) -> None:
    """If the model could see the reason, an attacker would enumerate the whole
    taxonomy of controls by deliberately asking for invalid things."""
    r = call(con, ctx, "record_saving",
             member_id=2, confirm_name="Carlos", amount=-5)
    assert r.reason == "non_positive_amount"        # kept for the trace
    assert "_reason" not in r.for_the_model
    assert "reason" not in r.for_the_model
    assert r.reason not in str(r.for_the_model)


def test_an_unknown_tool_does_not_break_anything(con, ctx) -> None:
    r = call(con, ctx, "delete_everything")
    assert r.for_the_model["ok"] is False
    assert r.reason == "unknown_tool"


def test_a_corrupt_money_setting_refuses_without_crashing(con, ctx) -> None:
    """REGRESSION: a bad money setting used to raise InvalidSetting straight
    through dispatch, killing the conversation loop that dispatch promises never
    to kill. It must become a clean refusal, and the bad value must not leak.

    The corrupt value is dated early so a non-future cut-off actually reads it.
    """
    from nati.world import db as _db

    _db.insert(
        con, "settings",
        {"key": "monthly_ticket_price", "value": "abc",
         "effective_from": "2026-01-02", "note": "poison"},
        ctx,
    )
    r = call(con, ctx, "fund_status", date="2026-03-01")
    assert r.for_the_model["ok"] is False        # refuses, does not raise
    assert r.reason == "internal_error"          # trace only
    assert "abc" not in r.for_the_model["message"]
    assert "monthly_ticket_price" not in r.for_the_model["message"]
    # the revealing detail is kept for the trace, not shown to the user
    assert "monthly_ticket_price" in (r.internal_error or "")


def test_refusals_do_not_leak_figures(con, ctx) -> None:
    """A refusal with figures would be an unaudited read through the write door —
    and a way to discover balances through failed attempts."""
    r = call(con, ctx, "record_payment", loan_id=1, amount=99_999_999)
    assert r.for_the_model["ok"] is False
    message = r.for_the_model["message"]
    assert "$" not in message, f"the refusal reveals a figure: {message!r}"
    assert "figures" not in r.for_the_model


# SQL-specific phrasings rather than bare words: in English several SQL keywords
# are ordinary vocabulary, so banning "check" alone would flag the perfectly good
# sentence "Check who it belongs to".
FORBIDDEN = (
    "trigger", "constraint", "sqlite", "traceback", "raise(abort",
    "lender_id", "member_id", "loan_id", "number_1", "number_2",
    "raffle_tickets", "loan_payments", "loan_interest", "audit_log",
)


def test_no_tool_message_reveals_the_control(con, ctx) -> None:
    attempts = [
        ("record_saving", dict(member_id=2, confirm_name="Rosa", amount=100)),
        ("record_saving", dict(member_id=999, confirm_name="X", amount=100)),
        ("record_saving", dict(member_id=True, confirm_name="Maria", amount=100)),
        ("record_saving", dict(member_id=2, confirm_name="Carlos", amount=0.5)),
        ("record_saving", dict(member_id=2, confirm_name="Carlos",
                               amount=100, date="2026-12-01")),
        ("record_payment", dict(loan_id=1, amount=99_999_999)),
        ("record_payment", dict(loan_id=999, amount=100)),
        ("sell_raffle_ticket", dict(member_id=5, confirm_name="Ana Vargas",
                                    kind="quarterly", month=3,
                                    number_1=1, number_2=2)),
        ("loan_status", dict(loan_id=999)),
        ("member_status", dict(member_id=999)),
    ]
    for name, args in attempts:
        r = call(con, ctx, name, **args)
        lowered = r.for_the_model["message"].lower()
        leaks = [p for p in FORBIDDEN if p in lowered]
        assert not leaks, f"{name}: {r.for_the_model['message']!r} leaks {leaks}"


# --------------------------------------------------------------------------
# Hostile types: nothing odd reaches the database
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [0, -1, 0.5, 100.5, True, False, None, "", "fifty", "5e4",
     "50.5", 2**63, [1], {"a": 1}],
)
def test_hostile_amounts_are_refused_without_touching_the_world(
    con, ctx, value
) -> None:
    """`True` is especially treacherous: bool IS int in Python, so
    `member_id=True` would be member 1 (the administrator) with nobody noticing."""
    before = db.world_hash(con)
    r = call(con, ctx, "record_saving",
             member_id=2, confirm_name="Carlos Ramirez", amount=value)
    assert r.for_the_model["ok"] is False, f"it accepted amount={value!r}"
    assert db.world_hash(con) == before


@pytest.mark.parametrize("value", ["50,000", "50000", " 50000 ", "$50,000", 50_000])
def test_valid_amounts_are_accepted(con, ctx, value) -> None:
    """US grouping is accepted: "50,000" is fifty thousand dollars."""
    r = call(con, ctx, "record_saving",
             member_id=2, confirm_name="Carlos Ramirez", amount=value)
    assert r.for_the_model["ok"] is True, r.for_the_model["message"]
    assert r.for_the_model["amount"] == 50_000


def test_the_thousands_separator_is_the_us_one(con, ctx) -> None:
    """THE currency trap of this project, and it is a 1000x money bug.

    In Colombia "50.000" means fifty thousand. In the United States it means
    FIFTY. Accepting it as 50,000 would multiply by a thousand what somebody
    typed exactly as they meant it.
    """
    accepted = call(con, ctx, "record_saving",
                    member_id=2, confirm_name="Carlos Ramirez", amount="50,000")
    assert accepted.for_the_model["ok"] is True
    assert accepted.for_the_model["amount"] == 50_000

    refused = call(con, ctx, "record_saving",
                   member_id=3, confirm_name="Luz Marina", amount="50.000")
    assert refused.for_the_model["ok"] is False, "'50.000' must not become 50,000"
    assert refused.reason == "fractional_amount"


def test_member_id_true_is_not_member_one(con, ctx) -> None:
    r = call(con, ctx, "record_saving",
             member_id=True, confirm_name="Maria Lopez", amount=100)
    assert r.for_the_model["ok"] is False


@pytest.mark.parametrize(
    "when",
    ["2026-W32-1", "20260805", "2026-8-5", "2026-08-05T00:00", "2026-02-30",
     "05/08/2026", "tomorrow", "2027-01-01", "2026-12-01", "2025-06-01"],
)
def test_hostile_dates_are_refused(con, ctx, when: str) -> None:
    """`date.fromisoformat` accepts "2026-W32-1" and "20260805". If one of those
    were stored in a TEXT column, the queries — which compare as text — would
    sort it wrongly and some would not see it at all."""
    before = db.world_hash(con)
    r = call(con, ctx, "record_saving", member_id=2,
             confirm_name="Carlos Ramirez", amount=100, date=when)
    assert r.for_the_model["ok"] is False, f"it accepted date={when!r}"
    assert db.world_hash(con) == before


def test_dates_are_stored_canonically(con, ctx) -> None:
    call(con, ctx, "record_saving", member_id=2,
         confirm_name="Carlos Ramirez", amount=100, date="2026-07-05")
    for table, column in [("savings", "date"), ("loan_payments", "date"),
                          ("loan_interest", "date"), ("loans", "started_on")]:
        bad = con.execute(
            f"SELECT COUNT(*) FROM {table} "  # noqa: S608
            f"WHERE {column} NOT GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        ).fetchone()[0]
        assert bad == 0, f"{table}.{column} has non-canonical dates"


# --------------------------------------------------------------------------
# Resistance to recording money against the wrong person
# --------------------------------------------------------------------------


def test_the_name_has_to_match(con, ctx) -> None:
    r = call(con, ctx, "record_saving",
             member_id=2, confirm_name="Rosa Elena Cardona", amount=100)
    assert r.for_the_model["ok"] is False
    assert r.reason == "name_mismatch"


def test_a_partial_name_is_enough(con, ctx) -> None:
    """"carlos" for "Carlos Ramirez": it is the same person."""
    r = call(con, ctx, "record_saving",
             member_id=2, confirm_name="carlos", amount=100)
    assert r.for_the_model["ok"] is True


def test_an_ambiguous_name_does_not_return_a_single_id(con, ctx) -> None:
    """The world has real traps: "Amparo" is two people."""
    r = call(con, ctx, "members", search="Amparo")
    assert r.for_the_model["ambiguous"] is True
    assert r.for_the_model["count"] >= 2
    assert "which one" in r.for_the_model["message"].lower()


@pytest.mark.parametrize("needle", ["Amparo", "Liliana"])
def test_the_worlds_ambiguous_names_are_detected(con, ctx, needle: str) -> None:
    r = call(con, ctx, "members", search=needle)
    assert r.for_the_model["ambiguous"] is True, f"{needle} should be ambiguous"


# --------------------------------------------------------------------------
# The fund / personal boundary
# --------------------------------------------------------------------------


def test_member_status_separates_personal_debt(con, ctx) -> None:
    """If it were not stated separately, Nati would tell a member they owe the
    fund nothing while they owe someone hundreds of that person's own money.

    The warning must NAME the lender. Several members lend privately, so a
    warning that said "the administrator" was simply false for most of them —
    in the one field whose whole job is keeping the two pockets apart.
    """
    row = con.execute(
        """SELECT l.member_id, le.name AS lender FROM loans l
             JOIN lenders le ON le.id = l.lender_id
            WHERE l.lender_id != 0 AND l.status = 'active' LIMIT 1"""
    ).fetchone()
    assert row is not None
    r = call(con, ctx, "member_status", member_id=int(row["member_id"]))
    body = r.for_the_model
    assert body["personal_debt_not_part_of_the_fund"] > 0
    warning = " ".join(body["warnings"])
    assert "private money" in warning
    assert row["lender"] in warning, f"the lender is not named: {warning!r}"
    assert "not to the common fund" in warning


def test_a_private_lender_is_never_mistaken_for_the_administrator(con, ctx) -> None:
    """A member borrowing from a teammate must not be told they owe the admin."""
    admin = con.execute(
        "SELECT name FROM lenders WHERE id = 1"
    ).fetchone()["name"]
    row = con.execute(
        """SELECT l.member_id, le.name AS lender FROM loans l
             JOIN lenders le ON le.id = l.lender_id
            WHERE l.lender_id NOT IN (0, 1) AND l.status = 'active' LIMIT 1"""
    ).fetchone()
    if row is None:
        pytest.skip("no loan from a non-administrator private lender")
    body = call(con, ctx, "member_status",
                member_id=int(row["member_id"])).for_the_model
    warning = " ".join(body["warnings"])
    assert row["lender"] in warning
    assert admin not in warning, f"credited the administrator instead: {warning!r}"


def test_every_loan_says_where_the_money_came_from(con, ctx) -> None:
    r = call(con, ctx, "list_loans")
    for loan in r.for_the_model["loans"]:
        assert loan["money_from"] in ("fund", "personal")


def test_the_interest_batch_does_not_touch_personal_money(con, ctx) -> None:
    """With no `loan_id` it covers ONLY the fund.

    If it covered everything, a single "charge July's interest" would hand money
    to the administrator in the same breath, without anyone asking for it.
    """
    r = call(con, ctx, "accrue_monthly_interest", month=8)
    assert r.for_the_model["ok"] is True
    sources = {c["money_from"] for c in r.for_the_model["charged"]}
    assert sources <= {"fund"}, f"the batch touched personal money: {sources}"
    assert any("Personally-funded" in w for w in r.for_the_model["warnings"])


def test_personal_interest_can_be_charged_one_at_a_time(con, ctx) -> None:
    personal = con.execute(
        "SELECT id FROM loans WHERE lender_id != 0 AND status='active' LIMIT 1"
    ).fetchone()
    r = call(con, ctx, "accrue_monthly_interest",
             month=8, loan_id=int(personal["id"]))
    assert r.for_the_model["ok"] is True
    assert r.for_the_model["charged"][0]["money_from"] == "personal"


# --------------------------------------------------------------------------
# The model does no arithmetic
# --------------------------------------------------------------------------


def test_interest_cannot_be_supplied_by_hand() -> None:
    """The interest tool's schema has no amount argument at all."""
    tool = registry.BY_NAME["accrue_monthly_interest"]
    props = set(tool.parameters["properties"])
    assert props == {"month", "loan_id"}
    assert not {"amount", "interest", "interest_amount"} & props


def test_interest_comes_from_the_original_amount(con, ctx) -> None:
    """Confirmed rule: on the ORIGINAL amount, not the balance."""
    r = call(con, ctx, "accrue_monthly_interest", month=8)
    for charged in r.for_the_model["charged"]:
        d = charged["derivation"]
        assert charged["interest"] == (d["base"] * d["rate"] + 50) // 100
        assert "original" in d["rule"]


def test_a_payment_reports_how_it_was_split(con, ctx) -> None:
    r = call(con, ctx, "record_payment", loan_id=1, amount=20)
    body = r.for_the_model
    assert body["ok"] is True
    assert body["to_interest"] + body["to_capital"] == body["paid"]
    assert body["to_interest"] > 0, "it should cover overdue interest first"


def test_a_payment_does_not_take_a_member_id() -> None:
    """It is derived from the loan. If it were passed, one person's payment could
    reduce someone else's debt."""
    props = set(registry.BY_NAME["record_payment"].parameters["properties"])
    assert "member_id" not in props
    assert props == {"loan_id", "amount", "date"}


def test_no_raffle_tool_takes_a_year() -> None:
    """It is derived from the cycle. `year=2027` would hide a debt from every query."""
    for name in ("sell_raffle_ticket", "raffle_status"):
        assert "year" not in registry.BY_NAME[name].parameters["properties"]


# --------------------------------------------------------------------------
# Closing is a consequence, not a decision
# --------------------------------------------------------------------------


def test_a_loan_closes_by_itself_at_zero(con, ctx) -> None:
    """There is deliberately no `close_loan` tool: closing is the result of
    reaching zero, not something anyone can ask for."""
    assert "close_loan" not in registry.BY_NAME
    balance = call(con, ctx, "loan_status", loan_id=1).for_the_model
    r = call(con, ctx, "record_payment", loan_id=1, amount=balance["balance"])
    assert r.for_the_model["loan_closed"] is True
    assert con.execute("SELECT status FROM loans WHERE id=1").fetchone()[0] == "closed"


def test_a_settled_loan_takes_no_more_payments(con, ctx) -> None:
    balance = call(con, ctx, "loan_status", loan_id=1).for_the_model
    call(con, ctx, "record_payment", loan_id=1, amount=balance["balance"])
    r = call(con, ctx, "record_payment", loan_id=1, amount=10)
    assert r.for_the_model["ok"] is False


def test_a_voided_loan_takes_no_payments(con, ctx) -> None:
    voided = con.execute(
        "SELECT id FROM loans WHERE status='voided' LIMIT 1"
    ).fetchone()
    r = call(con, ctx, "record_payment", loan_id=int(voided["id"]), amount=10)
    assert r.for_the_model["ok"] is False
    assert r.reason == "loan_voided"


# --------------------------------------------------------------------------
# The past cannot be moved
# --------------------------------------------------------------------------


def test_a_payment_cannot_predate_the_last_movement(con, ctx) -> None:
    """This closes an entire class: because the interest-first split is applied
    in chronological order at READ time, a movement dated earlier reclassifies
    capital already received as "interest paid"."""
    r = call(con, ctx, "record_payment", loan_id=1, amount=10, date="2026-03-05")
    assert r.for_the_model["ok"] is False
    assert r.reason == "backdated_movement"


def test_interest_is_not_charged_twice_in_a_month(con, ctx) -> None:
    """Idempotent: loans already charged are skipped for that reason.

    Note not ALL skips say "already": the seed deliberately contains a 0% loan,
    and that one is skipped for a different reason — writing a zero-interest row
    would occupy that month's slot forever, so if the rate were later corrected
    the real interest could never be charged.
    """
    first = call(con, ctx, "accrue_monthly_interest", month=8).for_the_model
    charged = {c["loan_id"] for c in first["charged"]}
    assert charged

    second = call(con, ctx, "accrue_monthly_interest", month=8).for_the_model
    assert not second["charged"]

    reasons = {s["loan_id"]: s["why"] for s in second["skipped"]}
    for loan_id in charged:
        assert "already" in reasons[loan_id], f"loan {loan_id}: {reasons[loan_id]}"


def test_a_loan_with_no_interest_leaves_no_zero_row(con, ctx) -> None:
    """A zero-interest row would occupy that month's slot forever."""
    no_rate = con.execute(
        "SELECT id FROM loans WHERE monthly_rate = 0 AND status='active' LIMIT 1"
    ).fetchone()
    assert no_rate is not None, "the seed should have a 0% loan"
    r = call(con, ctx, "accrue_monthly_interest",
             month=8, loan_id=int(no_rate["id"]))
    assert not r.for_the_model["charged"]
    assert "rate is zero" in r.for_the_model["skipped"][0]["why"]
    assert con.execute(
        "SELECT COUNT(*) FROM loan_interest WHERE loan_id = ? AND interest_amount = 0",
        (int(no_rate["id"]),),
    ).fetchone()[0] == 0


# --------------------------------------------------------------------------
# Raffle
# --------------------------------------------------------------------------


def test_the_quarterly_raffle_does_not_crash(con, ctx) -> None:
    """It used to raise TypeError: number_2 is NULL on quarterly tickets."""
    r = call(con, ctx, "raffle_status", kind="quarterly", month=3)
    assert r.for_the_model["ok"] is True
    assert r.for_the_model["drawn"] is True


def test_numbers_are_not_repeated_within_a_raffle(con, ctx) -> None:
    call(con, ctx, "sell_raffle_ticket", member_id=5,
         confirm_name="Ana Vargas", kind="monthly", month=8,
         number_1=33, number_2=77)
    r = call(con, ctx, "sell_raffle_ticket", member_id=9,
             confirm_name="Gloria Zapata", kind="monthly", month=8,
             number_1=77, number_2=88)
    assert r.for_the_model["ok"] is False


def test_monthly_takes_two_numbers_and_quarterly_one(con, ctx) -> None:
    missing = call(con, ctx, "sell_raffle_ticket", member_id=5,
                   confirm_name="Ana Vargas", kind="monthly",
                   month=8, number_1=33)
    assert missing.reason == "missing_number"
    extra = call(con, ctx, "sell_raffle_ticket", member_id=5,
                 confirm_name="Ana Vargas", kind="quarterly",
                 month=9, number_1=33, number_2=44)
    assert extra.reason == "extra_number"


def test_a_ticket_freezes_its_price(con, ctx) -> None:
    """No future settings change can revalue it."""
    r = call(con, ctx, "sell_raffle_ticket", member_id=5,
             confirm_name="Ana Vargas", kind="monthly", month=8,
             number_1=33, number_2=77)
    stored = con.execute(
        "SELECT price FROM raffle_tickets WHERE id=?",
        (r.for_the_model["ticket_id"],),
    ).fetchone()[0]
    assert stored == r.for_the_model["price"] == 10


def test_an_unpaid_ticket_becomes_a_debt(con, ctx) -> None:
    r = call(con, ctx, "sell_raffle_ticket", member_id=5,
             confirm_name="Ana Vargas", kind="monthly", month=8,
             number_1=33, number_2=77, paid=False)
    assert r.for_the_model["ok"] is True
    assert any("debt" in w for w in r.for_the_model["warnings"])
    status = call(con, ctx, "member_status", member_id=5).for_the_model
    assert status["raffle_debt"] >= 10


# --------------------------------------------------------------------------
# The figures the model may name
# --------------------------------------------------------------------------


def test_responses_declare_their_figures(con, ctx) -> None:
    """The output filter requires every number Nati writes to appear here.

    They carry a field name rather than being a flat list of values: one
    `raffle_status` call puts ~58 two-digit numbers in play, and with a flat list
    the model could write almost any small number and pass.
    """
    for name, args in [
        ("fund_status", {}),
        ("member_status", {"member_id": 2}),
        ("loan_status", {"loan_id": 1}),
        ("simulate_payout", {}),
    ]:
        body = call(con, ctx, name, **args).for_the_model
        assert body["figures"], f"{name} declared no figures"
        for figure in body["figures"]:
            assert set(figure) == {"field", "value", "text"}


def test_the_money_format_is_us_dollars() -> None:
    assert usd(1_000_000) == "$1,000,000"
    assert usd(13_170) == "$13,170"
    assert usd(0) == "$0"
    # The sign goes before the $: -$12,000, not $-12,000.
    assert usd(-12_000) == "-$12,000"


# --------------------------------------------------------------------------
# Simulation versus reality
# --------------------------------------------------------------------------


def test_simulate_payout_moves_nothing(con, ctx) -> None:
    before = db.world_hash(con)
    body = call(con, ctx, "simulate_payout").for_the_model
    assert body["is_simulation"] is True
    assert db.world_hash(con) == before
    assert any("simulation" in w for w in body["warnings"])


def test_the_payout_gives_everyone_the_same_profit(con, ctx) -> None:
    body = call(con, ctx, "simulate_payout").for_the_model
    profits = {s["profit"] for s in body["shares"]}
    assert max(profits) - min(profits) <= 1   # only the rounding dollar


def test_check_integrity_passes_on_the_seeded_world(con, ctx) -> None:
    body = call(con, ctx, "check_integrity").for_the_model
    assert body["all_good"] is True, body["failing"]
    assert len(body["checks"]) >= 6


# --------------------------------------------------------------------------
# A refusal leaves the world intact and evidence in the audit log
# --------------------------------------------------------------------------


def test_a_refusal_does_not_change_the_world(con, ctx) -> None:
    before = db.world_hash(con)
    for name, args in [
        ("record_saving", dict(member_id=2, confirm_name="Rosa", amount=1)),
        ("record_payment", dict(loan_id=1, amount=99_999_999)),
        ("sell_raffle_ticket", dict(member_id=5, confirm_name="Ana Vargas",
                                    kind="monthly", month=8, number_1=200)),
        ("accrue_monthly_interest", dict(month=12)),
    ]:
        r = call(con, ctx, name, **args)
        assert r.for_the_model["ok"] is False, name
        assert db.world_hash(con) == before, f"{name} changed the world"


def test_a_refusal_leaves_an_audit_row(con, ctx) -> None:
    """In this exercise the refusals ARE the product."""
    before = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    call(con, ctx, "record_saving", member_id=2, confirm_name="Rosa", amount=100)
    after = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before + 1
    row = con.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["tool"] == "record_saving"
    assert "refused" in row["args_json"]
    assert "name_mismatch" in row["args_json"]


def test_a_successful_write_reports_the_world_hash(con, ctx) -> None:
    """The checker diffs consecutive turns: a world change with no audit row
    stands out immediately."""
    r = call(con, ctx, "record_saving",
             member_id=2, confirm_name="Carlos Ramirez", amount=100)
    assert r.for_the_model["world_hash"] == db.world_hash(con)
    assert r.for_the_model["audit_id"] > 0


# --------------------------------------------------------------------------
# Pure gates
# --------------------------------------------------------------------------


def test_gate_messages_read_as_proper_english() -> None:
    """A money bot with broken grammar loses credibility."""
    messages = []
    for fn, args in [
        (gates.integer, (True, "member")),
        (gates.integer, (None, "member")),
        (gates.amount, (-1, "payment")),
        (gates.amount, (0.5, "saving")),
        (gates.iso_date, ("yesterday", "date")),
        (gates.cycle_month, (12,)),
        (gates.raffle_number, (200, "first number")),
    ]:
        with pytest.raises(gates.Refusal) as exc:
            fn(*args)
        messages.append(exc.value.message)

    for message in messages:
        assert message[0].isupper(), f"does not start capitalised: {message!r}"
        assert message.endswith((".", "!")), f"does not end in a period: {message!r}"
        for ugly in ("the the", "of the the", "{", "}", "None"):
            assert ugly not in message, f"{message!r} contains {ugly!r}"


def test_normalize_name_strips_accents_and_case() -> None:
    assert gates.normalize_name("  José  ANDRÉS  ") == "jose andres"
    assert gates.normalize_name("Muñoz") == "munoz"


def test_raffle_status_reports_the_real_schedule(con, ctx) -> None:
    """Nati answered "quarterly raffles in months 3, 6, 9, 11" from its own head.

    The world has 3 and 6. Month numbers are small enough to pass the output
    filter freely, so a fabricated schedule is invisible there — the fix is to
    return the fact rather than leave the model to guess it.
    """
    result = call(con, ctx, "raffle_status", kind="quarterly", month=3)
    env = result.for_the_model
    assert env["ok"] is True

    actual = [
        int(r["month"]) for r in con.execute(
            """SELECT DISTINCT month FROM raffle_tickets
                WHERE kind = 'quarterly' ORDER BY month"""
        )
    ]
    assert env["months_this_raffle_has_run"] == actual
    assert 9 not in env["months_this_raffle_has_run"]
    assert "schedule_note" in env


def test_an_out_of_range_id_refuses_instead_of_crashing(con, ctx) -> None:
    """REGRESSION (critical): the dispatcher promises it NEVER raises.

    An id of 2**63 does not fit a SQLite INTEGER, and the driver raises
    OverflowError — not IntegrityError — so it escaped every `except` clause in
    `dispatch` and surfaced as an unhandled 500, ending the conversation. A red
    team reached it from one chat message, on ten of the nineteen tools. That is
    the trivial denial of service the layered design exists to prevent.
    """
    huge = 2 ** 63
    everything = {
        "member_id": huge, "loan_id": huge, "saving_id": huge, "month": huge,
        "confirm_name": "x", "amount": huge, "monthly_rate": 1,
        "money_from": "fund", "kind": "monthly", "number_1": 1,
        "winning_number": 1, "key": "monthly_prize", "value": huge,
        "name": "Someone", "phone": "3001234567",
    }
    oversized = {k for k, v in everything.items() if v == huge}
    before = db.world_hash(con)
    checked = 0
    for tool in registry.TOOLS:
        args = {k: v for k, v in everything.items()
                if k in tool.parameters["properties"]}
        if not args:
            continue
        result = call(con, ctx, tool.name, **args)          # must not raise
        envelope = result.for_the_model
        assert envelope.get("message"), f"{tool.name} returned no sentence"
        # Only tools that actually RECEIVE an oversized value must refuse; one
        # that takes none of them (list_loans) is right to succeed.
        if oversized & set(args):
            assert envelope.get("ok") is False, f"{tool.name} accepted 2**63"
            checked += 1
    assert checked >= 10, f"expected to exercise the affected tools, got {checked}"
    assert db.world_hash(con) == before, "a refused call changed the world"


def test_a_negative_out_of_range_id_also_refuses(con, ctx) -> None:
    result = call(con, ctx, "member_status", member_id=-(2 ** 63) - 1)
    assert result.for_the_model["ok"] is False
    assert result.reason == "absurd_number"


def test_the_loan_book_answers_interest_in_one_result(con, ctx) -> None:
    """A real conversation asked "show everyone's active loans and how much
    interest they have paid" and got nothing.

    `list_loans` reported balances but not interest, so the model either had to
    call a second tool once per loan or work the split out itself — and working
    it out is precisely what it is not allowed to do. It correctly said it did
    not have the figures, which is the right answer to the wrong problem: the gap
    was in the tool. `reads.py`'s own rule is one question, one result.
    """
    body = call(con, ctx, "list_loans", status="active").for_the_model
    assert body["ok"] is True
    assert body["loans"], "no active loans in the seeded world"

    for loan in body["loans"]:
        for field in ("interest_charged", "interest_paid", "capital_repaid"):
            assert field in loan, f"{field} missing from the loan book"

    # And the total, so "how much interest have we collected?" is one call too.
    assert "interest_paid_total" in body
    assert body["interest_paid_total"] == sum(
        l["interest_paid"] for l in body["loans"]
    )

    # Every one of those amounts must be speakable AS MONEY, or the answer gets
    # suppressed on its own figures.
    from nati.agent import egress
    money = egress.allowed_money([body], "")
    for loan in body["loans"]:
        for field in ("interest_charged", "interest_paid", "capital_repaid"):
            assert str(loan[field]) in money, f"{field} is not money-tagged"


def test_the_loan_book_names_whose_money_funded_each_loan(con, ctx) -> None:
    """C-series. Whose money funded a loan is the sharpest rule in the domain and
    the SOP says it can never be inferred, only stated — so every tool that
    reports the fund/personal split must also say WHOSE.

    Asked to split the loan book by source, the model reported that Julian Marin's
    loan came from "his own money". Julian is the borrower; Maria Lopez funded it.
    `list_loans` had said `personal` and nothing more, so the model supplied the
    only name it had. No later layer catches that: the output filter judges
    figures, and a lender is a name.
    """
    env = registry.dispatch(con, "list_loans", {}, ctx).for_the_model
    for loan in env["loans"]:
        assert loan.get("lender"), f"loan {loan['loan_id']} does not name its funder"
        if loan["money_from"] == "fund":
            assert loan["lender"] == "Natillera Fund"
        else:
            # A personal loan names a person, and never the borrower themselves.
            assert loan["lender"] != loan["member"], (
                f"loan {loan['loan_id']} says {loan['member']} funded their own loan"
            )


def test_the_loan_book_totals_what_it_returns(con, ctx) -> None:
    """A total the answer obviously needs must come from the tool, or it is summed.

    Filtered to personal loans, the only totals here were the fund's — $0 by
    construction — so "give me my personal loans status" had no total in the
    result and the model added the four balances itself, reaching $1,699 and $199.
    Both were arithmetically right and both were suppressed, because no tool
    returned them. The rule cannot soften: the same model computed $494 for a
    $470 balance an hour earlier.
    """
    for money_from in ("personal", "fund", None):
        args = {} if money_from is None else {"money_from": money_from}
        env = registry.dispatch(con, "list_loans", args, ctx).for_the_model
        active = [l for l in env["loans"] if l["status"] == "active"]
        assert env["owed_total"] == sum(l["balance"] for l in active)
        assert env["interest_charged_total"] == \
            sum(l["interest_charged"] for l in env["loans"])
        assert env["interest_outstanding_total"] == \
            sum(l["interest_outstanding"] for l in active)


def test_every_total_the_loan_book_states_is_speakable_as_money(con, ctx) -> None:
    """Returning a total is not enough: it has to be TAGGED as money, or the
    filter will still suppress the sentence that reports it."""
    from nati.agent import egress

    env = registry.dispatch(
        con, "list_loans", {"money_from": "personal"}, ctx).for_the_model
    allowed = egress.allowed_money([env], "")
    for key in ("owed_total", "interest_charged_total",
                "interest_outstanding_total", "interest_paid_total"):
        assert str(env[key]) in allowed, f"{key} is not money-speakable"
