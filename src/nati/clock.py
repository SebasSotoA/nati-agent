"""The one clock in the system. Anything that depends on "today" goes through here.

Why a whole module for this: the natillera is in Colombia (UTC-5). If a
"no future dates" gate used UTC, then between 19:00 and midnight Bogota time the
UTC date is ALREADY tomorrow, so the gate would happily accept tomorrow. One
clock, in the right zone, removes the entire class of error.

The synthetic world is also seeded up to a fixed date, so tests and scenarios
need to be able to pin "today" explicitly.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Bogota")

# When set, `today()` returns this instead of the real date. Set by `freeze()`
# (tests, scenarios) or by `--date` on the CLI.
_frozen: date | None = None


def today() -> date:
    """Today's date in Bogota, or the frozen date if one is set."""
    if _frozen is not None:
        return _frozen
    return datetime.now(ZONE).date()


def today_iso() -> str:
    return today().isoformat()


def now_iso() -> str:
    """Timestamp for the audit trail, in Bogota time with its offset.

    The offset (-05:00) is stored on purpose: a timestamp without a zone is a
    trap for later, because there is no way to know what it meant.
    """
    if _frozen is not None:
        # With the clock frozen, freeze the time too, so audit dumps are
        # reproducible.
        return datetime.combine(_frozen, datetime.min.time(), tzinfo=ZONE).isoformat(
            timespec="seconds"
        )
    return datetime.now(ZONE).isoformat(timespec="seconds")


def set_today(value: date | str | None) -> None:
    """Freeze today's date, or unfreeze it with None."""
    global _frozen
    if value is None:
        _frozen = None
        return
    _frozen = date.fromisoformat(value) if isinstance(value, str) else value


@contextmanager
def frozen(value: date | str):
    """Freeze the clock inside the block and restore it on the way out."""
    previous = _frozen
    set_today(value)
    try:
        yield today()
    finally:
        set_today(previous)
