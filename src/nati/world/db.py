"""Access to the world (SQLite), and the only place it is written.

Four invariants hold everything else up:

1. `PRAGMA foreign_keys = ON` on EVERY connection. A connection that skips it
   silently voids the schema's guarantees — that would be a donated break.
2. Every write goes through `insert()` / `update()`, which also write a row to
   `audit_log` carrying the trace's conv_id and turn number. A careless tool
   cannot skip the audit because there is no other way to write.
3. Each operation may only write the columns that belong to it
   (ALLOWED_COLUMNS). Enforcement lives at the chokepoint, not in 19 separate
   call sites that somebody has to remember.
4. EVERY attempt is recorded, including refused ones. A session that hits
   fifteen gates must leave fifteen rows of evidence: in this exercise, the
   refusals are the product.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nati import clock, config

# Tables the world exposes. The order is stable so the canonical dump is
# comparable and hashable.
TABLES = (
    "members",
    "lenders",
    "settings",
    "savings",
    "loans",
    "loan_payments",
    "loan_interest",
    "raffle_tickets",
    "draws",
)

# Which columns each operation may touch, keyed by (table, operation).
#
# Without this, `update()` accepts any column of any table and the only brake is
# each tool's discipline. With it, a tool that tries to rewrite a ticket's
# numbers while marking it paid fails here, not in a code review.
ALLOWED_COLUMNS: dict[tuple[str, str], frozenset[str]] = {
    ("members", "insert"): frozenset({"name", "phone", "joined_on"}),
    ("savings", "insert"): frozenset(
        {"member_id", "date", "amount", "reversal_of"}
    ),
    ("loans", "insert"): frozenset(
        {"member_id", "lender_id", "principal", "monthly_rate", "started_on", "status"}
    ),
    # Only a loan's status moves. Never the amount, the rate, or whose money
    # funded it.
    ("loans", "update"): frozenset({"status"}),
    ("loan_payments", "insert"): frozenset(
        {"loan_id", "member_id", "date", "amount"}
    ),
    ("loan_interest", "insert"): frozenset(
        {"loan_id", "date", "interest_amount"}
    ),
    ("raffle_tickets", "insert"): frozenset(
        {"member_id", "kind", "month", "year", "paid_on", "paid", "price",
         "number_1", "number_2"}
    ),
    # Marking a ticket paid must NOT be able to touch the numbers or the price.
    ("raffle_tickets", "update"): frozenset({"paid", "paid_on"}),
    ("draws", "insert"): frozenset(
        {"kind", "month", "year", "winning_number", "prize", "date"}
    ),
    ("settings", "insert"): frozenset({"key", "value", "effective_from", "note"}),
    # `lenders` is never written by a tool: it comes from the seed.
}


class WorldError(Exception):
    """A failed operation on the world, with a message FOR THE PERSON.

    Anything raised here can reach the administrator, so it never mentions
    tables, columns, triggers or constraints.
    """


class InvariantBroken(WorldError):
    """A post-write check failed; the write was rolled back."""


class ProgrammingError(Exception):
    """A tool asked for something no tool should ask for. This is OUR bug.

    Deliberately NOT a WorldError: its message is technical and must never
    reach the model or the person. The model does not choose columns — it
    chooses arguments, and tools translate those into columns — so only badly
    written code can trigger this, never an attack.
    """


# Operations a person might genuinely ask for, that are forbidden by design.
# These carry an English explanation because refusing them is legitimate.
FORBIDDEN_OPERATIONS: dict[tuple[str, str], str] = {
    ("settings", "update"): (
        "Settings are not overwritten. A new value is added with its own "
        "effective date, so nothing that already happened changes."
    ),
    ("savings", "update"): (
        "A recorded saving is not edited. If it went in wrong, it is corrected "
        "by recording an opposite entry, and both stay visible."
    ),
    ("loan_payments", "update"): (
        "A recorded payment is not edited. It is corrected with an opposite "
        "entry, so the trail of both remains."
    ),
    ("loan_interest", "update"): "Interest already charged is not edited.",
    ("draws", "update"): "A draw that has been recorded is not edited.",
    ("members", "update"): "A member's details are not changed from here.",
}


@dataclass(frozen=True)
class Ctx:
    """Who made a change and in which conversation. Goes to `audit_log`."""

    conv_id: str | None = None
    seq: int | None = None
    tool: str = "manual"
    args: dict[str, Any] | None = None
    actor: str = "admin"
    # Which of the administrator's messages this is (1 for her first). Distinct
    # from `seq`, which counts tool calls inside one turn. Confirmation tokens
    # use it to require that her "yes" actually arrived between the summary and
    # the write.
    turn: int | None = None
    # The arguments exactly as the model sent them, before normalization. Kept
    # separately because if only the normalized form were logged, the
    # normalization would hide the attack.
    raw_args: dict[str, Any] | None = field(default=None)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the world with foreign keys on. Always use this function.

    `isolation_level=None` turns off sqlite3's automatic transaction handling so
    transactions are opened by hand with BEGIN IMMEDIATE. That is what lets a
    gate read and write inside the SAME transaction; in the default mode the
    gate's read happens outside it.
    """
    path = path or config.DB_PATH
    if not path.exists():
        raise WorldError(
            f"There is no world at {path}.\n  Run:  uv run nati seed --reset"
        )
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def create(path: Path | None = None, *, reset: bool = False) -> Path:
    """Build the world from schema.sql + seed.sql. Always reproducible."""
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        if not reset:
            raise WorldError(
                f"{path} already exists.\n  Use --reset to rebuild it from scratch."
            )
        path.unlink()

    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8"))
        con.executescript(config.SEED_SQL.read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()
    return path


@contextmanager
def transaction(con: sqlite3.Connection):
    """A transaction that CAN be nested.

    The problem this solves: sqlite3's `with con:` performs a COMMIT on exit. So
    a batch operation calling `insert()` nine times was nine independent
    transactions — if the sixth hit a gate, the first five were already written
    and nothing rolled them back. The world was left half-done.

    Here, if a transaction is already open the block joins it (opening and
    closing nothing). Only the outermost block commits or rolls back, so a whole
    batch is all-or-nothing.

    BEGIN IMMEDIATE takes the write lock BEFORE a gate's first read, which is
    what stops the world changing between "I checked" and "I wrote".
    """
    if con.in_transaction:
        yield con  # already inside: do not open another
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _check_columns(table: str, operation: str, columns: Any) -> None:
    """No write may touch columns outside its allowlist.

    Two classes of refusal, on purpose:

    - `WorldError` when it is something a person could have asked for in good
      faith (editing a saving, changing a setting). It carries an English
      explanation and may reach the administrator.
    - `ProgrammingError` when no tool should ever have asked. That is our bug,
      its message is technical, and it must not travel out to the model.
    """
    forbidden = FORBIDDEN_OPERATIONS.get((table, operation))
    if forbidden is not None:
        raise WorldError(forbidden)

    allowed = ALLOWED_COLUMNS.get((table, operation))
    if allowed is None:
        raise ProgrammingError(f"{operation} on {table} is not defined")
    if "id" in columns:
        # An explicit id breaks the canonical dump's ORDER BY id (SQLite accepts
        # explicit and even negative rowids without complaint).
        raise ProgrammingError("ids are not assigned by hand")
    extra = set(columns) - allowed
    if extra:
        raise ProgrammingError(
            f"{operation} on {table} cannot touch: {', '.join(sorted(extra))}"
        )


def _row_as_dict(con: sqlite3.Connection, table: str, pk: int) -> dict | None:
    cur = con.execute(f"SELECT * FROM {table} WHERE id = ?", (pk,))  # noqa: S608
    row = cur.fetchone()
    return dict(row) if row else None


def _audit(
    con: sqlite3.Connection,
    ctx: Ctx,
    *,
    table: str,
    pk: int | None,
    before: dict | None,
    after: dict | None,
    outcome: str = "ok",
    reason: str | None = None,
) -> int:
    """Write one audit row. Used for successes AND for refusals."""
    args: dict[str, Any] = {"effective": ctx.args or {}}
    if ctx.raw_args is not None and ctx.raw_args != ctx.args:
        # If only the normalized form were stored, normalization would hide the
        # attack.
        args["raw"] = ctx.raw_args
    if outcome != "ok":
        args["outcome"] = outcome
        if reason:
            args["reason"] = reason

    cur = con.execute(
        """INSERT INTO audit_log
             (ts, conv_id, seq, actor, tool, args_json,
              table_name, row_pk, before_json, after_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            clock.now_iso(),
            ctx.conv_id,
            ctx.seq,
            ctx.actor,
            ctx.tool,
            json.dumps(args, ensure_ascii=False, sort_keys=True, default=str),
            table,
            pk,
            json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None,
            json.dumps(after, ensure_ascii=False, sort_keys=True) if after else None,
        ),
    )
    return int(cur.lastrowid)


def audit_refusal(
    con: sqlite3.Connection, ctx: Ctx, reason: str, table: str | None = None
) -> int:
    """Record an attempt that did NOT happen, in its own transaction.

    Separate on purpose: if the refusal were audited inside the transaction
    being rolled back, the ROLLBACK would erase the evidence of the attempt
    along with it. First undo the write, then note that it was attempted.
    """
    with transaction(con):
        return _audit(
            con, ctx, table=table or "-", pk=None, before=None, after=None,
            outcome="refused", reason=reason,
        )


# Exceptions a write can raise that need translating. OverflowError is included
# because 2**63 does not fit in a SQLite INTEGER and surfaces as OverflowError,
# not IntegrityError — without this the model received a traceback.
_WRITE_ERRORS = (sqlite3.Error, OverflowError, ValueError)


def insert(
    con: sqlite3.Connection,
    table: str,
    values: dict[str, Any],
    ctx: Ctx,
    *,
    verify: Callable[[sqlite3.Connection, int], None] | None = None,
) -> dict[str, int]:
    """INSERT plus its audit row: atomic and composable.

    `verify` runs AFTER the insert and BEFORE the commit, receiving
    (connection, pk). If it raises, the write and its audit row are rolled back
    together.

    It is the only correct way to check invariants that can only be evaluated
    against state that has ALREADY been written — for example "this payment left
    no overpayment" or "the fund's cash did not go negative". Checking those
    beforehand is wrong as soon as dates can vary, because a loan's balance
    depends on the full chronological order of its movements.

    Returns {'id': pk, 'audit_id': n}. Never empty.
    """
    if table not in TABLES:
        raise WorldError(f"unknown table: {table!r}")
    if not values:
        raise WorldError("insert() with no values")
    _check_columns(table, "insert", values.keys())

    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sql = f"INSERT INTO {table} ({columns}) VALUES ({marks})"  # noqa: S608

    try:
        with transaction(con):
            cur = con.execute(sql, tuple(values.values()))
            pk = int(cur.lastrowid)
            after = _row_as_dict(con, table, pk)
            audit_id = _audit(con, ctx, table=table, pk=pk, before=None, after=after)
            if verify is not None:
                verify(con, pk)
    except sqlite3.IntegrityError as exc:
        raise WorldError(_translate_integrity(exc, table)) from exc
    except InvariantBroken:
        raise
    except _WRITE_ERRORS as exc:
        raise WorldError(_translate_generic(exc)) from exc

    return {"id": pk, "audit_id": audit_id}


def update(
    con: sqlite3.Connection,
    table: str,
    pk: int,
    changes: dict[str, Any],
    ctx: Ctx,
    *,
    verify: Callable[[sqlite3.Connection, int], None] | None = None,
) -> dict[str, Any]:
    """UPDATE plus its audit row with before/after. Atomic and composable."""
    if table not in TABLES:
        raise WorldError(f"unknown table: {table!r}")
    if not changes:
        raise WorldError("update() with no changes")
    _check_columns(table, "update", changes.keys())

    assignments = ", ".join(f"{c} = ?" for c in changes)
    sql = f"UPDATE {table} SET {assignments} WHERE id = ?"  # noqa: S608

    try:
        with transaction(con):
            # `before` is read INSIDE the transaction. Reading it outside left a
            # window in which before_json could go stale: a TOCTOU on the audit
            # trail itself.
            before = _row_as_dict(con, table, pk)
            if before is None:
                raise WorldError(f"there is no {table} with id {pk}")
            con.execute(sql, (*changes.values(), pk))
            after = _row_as_dict(con, table, pk)
            audit_id = _audit(con, ctx, table=table, pk=pk, before=before, after=after)
            if verify is not None:
                verify(con, pk)
    except sqlite3.IntegrityError as exc:
        raise WorldError(_translate_integrity(exc, table)) from exc
    except (WorldError, InvariantBroken):
        raise
    except _WRITE_ERRORS as exc:
        raise WorldError(_translate_generic(exc)) from exc

    return {"id": pk, "audit_id": audit_id, "before": before, "after": after}


def _translate_generic(exc: Exception) -> str:
    """Non-integrity errors, without leaking technical detail."""
    if isinstance(exc, OverflowError):
        return "That number is too large to record."
    return "I could not record that: the data is not the shape I expected."


# Prefixes SQLite puts on its own integrity errors. Anything that does not start
# like this is a message of ours, hand-written in a RAISE(ABORT).
_SQLITE_PREFIXES = (
    "UNIQUE constraint failed",
    "CHECK constraint failed",
    "FOREIGN KEY constraint failed",
    "NOT NULL constraint failed",
    "datatype mismatch",
)

# UNIQUE violations by (table, telltale column) → English explanation.
_UNIQUE_MESSAGES: dict[tuple[str, str], str] = {
    ("raffle_tickets", "member_id"):
        "That person already has a ticket for that month. Only one is allowed.",
    ("draws", "kind"): "There is already a draw recorded for that month.",
    ("members", "phone"): "That phone number already belongs to someone else.",
    # `settings` no longer has a UNIQUE (key, effective_from), so this branch is
    # unreachable today. It stays as a safety net: if a uniqueness rule is ever
    # reintroduced there, the administrator gets a sentence rather than raw
    # SQLite constraint text naming the table and index.
    ("settings", "key"):
        "There is already an entry for that setting with the same effective date.",
    ("loan_interest", "loan_id"):
        "That loan already has its interest recorded for that month.",
}


def _translate_integrity(exc: sqlite3.IntegrityError, table: str) -> str:
    """Turn an integrity error into something Nati can say out loud.

    Two rules:

    1. If we wrote the message ourselves (a RAISE(ABORT) in the schema), it is
       already plain English aimed at a person, so it passes through unchanged.
       That keeps the message next to the rule that raises it, with no second
       copy to drift — which is how a table name leaked out once before.
    2. If the message came from SQLite, it is NEVER forwarded raw: it gives away
       table, column, index and constraint names, which is the map of the
       controls.
    """
    text = str(exc)
    raw_from_sqlite = any(text.startswith(p) for p in _SQLITE_PREFIXES)

    if not raw_from_sqlite:
        return text  # our own schema message, already readable

    if text.startswith("UNIQUE"):
        for (t, col), explanation in _UNIQUE_MESSAGES.items():
            if t == table and col in text:
                return explanation
        return "That record already exists and cannot be duplicated."
    if text.startswith("FOREIGN KEY"):
        return "I am pointing at something that does not exist. Check who it belongs to."
    if text.startswith("NOT NULL"):
        return "A required piece of information is missing, so I cannot record that."
    if text.startswith("datatype mismatch"):
        return "One of the values is not the right kind of data."
    # CHECK and anything else: say a rule was not met, without saying which one.
    return "That data does not meet one of the natillera's rules, so I did not record it."


# --------------------------------------------------------------------------
# Canonical dump: for hashing the world and for readable diffs
# --------------------------------------------------------------------------


def canonical_dump(con: sqlite3.Connection, *, include_audit: bool = False) -> str:
    """A deterministic JSON view of the world: sorted keys, rows by id.

    Hashed into every trace (`world_db_sha256`) so a finding is anchored to the
    exact world it happened in. The goal is not a byte-identical .db file
    (SQLite has too much internal nondeterminism) but an identical dump.
    """
    world: dict[str, Any] = {}
    tables = list(TABLES) + (["audit_log"] if include_audit else [])
    for table in tables:
        rows = con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()  # noqa: S608
        world[table] = [dict(r) for r in rows]
    return json.dumps(world, ensure_ascii=False, sort_keys=True, indent=2)


def world_hash(con: sqlite3.Connection) -> str:
    """sha256 of the canonical dump, excluding the audit log (it always grows)."""
    return hashlib.sha256(canonical_dump(con).encode("utf-8")).hexdigest()
