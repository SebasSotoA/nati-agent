"""Cycle start, reset, and production bootstrap."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from nati import clock
from nati.config import WORLD_TODAY
from nati.tools import confirm, gates, registry
from nati.world import db
from nati.world import queries as q

CYCLE_DAY = WORLD_TODAY  # 2026-08-04


@pytest.fixture(autouse=True)
def frozen_clock():
    with clock.frozen(CYCLE_DAY):
        yield


@pytest.fixture(autouse=True)
def fresh_confirmations():
    confirm.reset()
    yield
    confirm.reset()


@pytest.fixture
def challenge(tmp_path: Path):
    path = tmp_path / "challenge.db"
    db.create(path, reset=True, seed="challenge")
    con = db.connect(path)
    yield con
    con.close()


@pytest.fixture
def prod(tmp_path: Path):
    path = tmp_path / "prod.db"
    db.create(path, reset=True, seed="prod")
    con = db.connect(path)
    yield con
    con.close()


@pytest.fixture
def ctx() -> db.Ctx:
    return db.Ctx(conv_id="test-cycle", seq=1)


def call(con, ctx, tool_name: str, /, **args):
    return registry.dispatch(con, tool_name, args, ctx)


def confirmed(con, ctx, tool_name: str, /, **args):
    first = call(con, ctx, tool_name, **args)
    assert first.for_the_model.get("confirmation_required"), first.for_the_model
    token = first.for_the_model["token"]
    return call(con, ctx, tool_name, **args, confirm_token=token)


def test_prod_bootstrap_is_empty(prod) -> None:
    """Without NATI_SEED=challenge, create() yields the empty production world."""
    assert os.environ.get("NATI_SEED") == "challenge"  # tests default; we passed prod
    members = prod.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    lenders = list(prod.execute("SELECT id, name, kind, member_id FROM lenders"))
    assert members == 0
    assert len(lenders) == 1
    assert int(lenders[0]["id"]) == 0
    assert lenders[0]["name"] == "Natillera Fund"
    assert lenders[0]["kind"] == "fund"
    assert lenders[0]["member_id"] is None
    assert q.cycle_year(prod) == clock.today().year
    assert ("cycle_archives", "insert") not in db.ALLOWED_COLUMNS
    assert ("_cycle_reset_guard", "insert") not in db.ALLOWED_COLUMNS
    assert ("_cycle_reset_guard", "update") not in db.ALLOWED_COLUMNS


def test_start_cycle_requires_confirm_then_advances(challenge, ctx) -> None:
    before = q.cycle_year(challenge)
    first = call(challenge, ctx, "start_cycle")
    assert first.for_the_model["ok"] is False
    assert first.for_the_model.get("confirmation_required")
    assert q.cycle_year(challenge) == before

    result = confirmed(challenge, ctx, "start_cycle")
    assert result.for_the_model["ok"] is True
    assert q.cycle_year(challenge) == before + 1

    added = confirmed(
        challenge, ctx, "add_member", name="Nueva Persona", phone="3129999999"
    )
    assert added.for_the_model["ok"] is True
    row = challenge.execute(
        "SELECT name FROM members WHERE id = ?",
        (added.for_the_model["member_id"],),
    ).fetchone()
    assert row["name"] == "Nueva Persona"


def test_date_in_cycle_respects_last_month_12(prod, ctx) -> None:
    db.bootstrap_admin(prod, "Ana Tester", "3101111111", ctx)
    db.insert(
        prod,
        "settings",
        {
            "key": "cycle_last_month",
            "value": "12",
            "effective_from": clock.today_iso(),
            "note": "test",
        },
        ctx,
    )
    with clock.frozen("2026-12-04"):
        assert (
            gates.date_in_cycle("2026-12-01", 2026, last_month=12) == "2026-12-01"
        )
        with pytest.raises(gates.Refusal) as exc:
            gates.date_in_cycle("2026-12-01", 2026, last_month=11)
        assert exc.value.reason == "december_excluded"

        first, last = q.cycle_months(prod)
        assert last == 12
        saving = call(
            prod,
            ctx,
            "record_saving",
            member_id=1,
            confirm_name="Ana Tester",
            amount=10,
            date="2026-12-01",
        )
        assert saving.for_the_model["ok"] is True, saving.for_the_model


def test_reset_cycle_archives_keeps_members_guard_still_blocks(
    challenge, ctx
) -> None:
    n_members = challenge.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    n_savings = challenge.execute("SELECT COUNT(*) FROM savings").fetchone()[0]
    assert n_members > 0 and n_savings > 0

    with pytest.raises(sqlite3.IntegrityError):
        challenge.execute("DELETE FROM savings")

    first = call(challenge, ctx, "reset_cycle")
    assert first.for_the_model["ok"] is False
    assert first.for_the_model.get("confirmation_required")
    assert "Members and lenders stay" in first.for_the_model["summary"]
    assert challenge.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == n_savings

    result = confirmed(challenge, ctx, "reset_cycle")
    assert result.for_the_model["ok"] is True

    assert challenge.execute("SELECT COUNT(*) FROM members").fetchone()[0] == n_members
    assert challenge.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == 0
    assert challenge.execute("SELECT COUNT(*) FROM lenders").fetchone()[0] == 2

    archive = challenge.execute(
        "SELECT * FROM cycle_archives ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert archive is not None
    snapshot = json.loads(archive["snapshot"])
    assert snapshot["cycle_year"] == q.cycle_year(challenge)
    assert len(snapshot["savings"]) == n_savings
    assert (
        challenge.execute(
            "SELECT allow FROM _cycle_reset_guard WHERE id = 1"
        ).fetchone()[0]
        == 0
    )

    with pytest.raises(sqlite3.IntegrityError):
        challenge.execute("DELETE FROM members")
    with pytest.raises(sqlite3.IntegrityError):
        challenge.execute("DELETE FROM cycle_archives")


def test_reset_refuses_when_nothing_to_reset(prod, ctx) -> None:
    result = call(prod, ctx, "reset_cycle")
    assert result.for_the_model["ok"] is False
    assert result.reason == "nothing_to_reset"
    assert result.for_the_model.get("confirmation_required") is None


def test_reset_after_start_cycle_clears_the_year_just_left(challenge, ctx) -> None:
    """start_cycle advances the setting; reset still archives the prior year."""
    n_savings = challenge.execute("SELECT COUNT(*) FROM savings").fetchone()[0]
    assert n_savings > 0
    before = q.cycle_year(challenge)
    confirmed(challenge, ctx, "start_cycle")
    assert q.cycle_year(challenge) == before + 1
    assert challenge.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == n_savings

    result = confirmed(challenge, ctx, "reset_cycle")
    assert result.for_the_model["ok"] is True
    assert result.for_the_model["year"] == before
    assert challenge.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == 0
    archive = challenge.execute(
        "SELECT cycle_year FROM cycle_archives ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert int(archive["cycle_year"]) == before
