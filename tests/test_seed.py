"""The seed generator must be deterministic.

Every conversation trace is anchored to a `world_db_sha256`, so the same
generator must produce the same seed.sql byte for byte — otherwise two people
who "reset the world" get different worlds and cannot compare findings. The
generator's own docstring promises this test exists; here it is.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_seed.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("generate_seed", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generation_is_byte_identical_across_runs(gen) -> None:
    assert gen.generate() == gen.generate()


def test_the_committed_seed_matches_the_generator(gen) -> None:
    """If someone hand-edits seed.sql, this fails — the seed is generated, not
    written. Re-run:  uv run python scripts/generate_seed.py"""
    committed = (ROOT / "src" / "nati" / "world" / "seed.sql").read_text(encoding="utf-8")
    assert gen.generate() == committed, (
        "seed.sql is out of sync with the generator; "
        "run `uv run python scripts/generate_seed.py`"
    )


def test_the_generated_seed_uses_us_dollars(gen) -> None:
    """A sanity check on the currency migration: the small USD figures, not the
    old thousands-scale peso values."""
    sql = gen.generate()
    assert "'monthly_ticket_price', '10'" in sql
    assert "'monthly_prize', '60'" in sql
    assert "10000" not in sql and "60000" not in sql  # no peso-scale leftovers
