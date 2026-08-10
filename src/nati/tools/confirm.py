"""Two-phase confirmation for the rare, dangerous write tools.

The problem it solves: "confirm before you act" as a prompt rule is a
politeness, and a model under persuasion drops it (observed: the smaller
target model wrote on the first ask). This makes confirmation a MECHANISM:

1. The first call returns ``{ok: false, confirmation_required: true, token,
   summary}``. Nothing is written.
2. The write happens only when the tool is called AGAIN with the same
   arguments plus that token.

Three properties, in decreasing order of importance:

- **The token is unforgeable.** It is an HMAC over (conversation, tool,
  normalized arguments) with a per-process random key. The model cannot invent
  one, and a token minted for one set of arguments is invalid for any other —
  so what was confirmed is exactly what happens, byte for byte.
- **Single use, short lived.** A consumed token is gone; an unused one expires.
  No stockpiling confirmations early in a conversation to spend later.
- **The summary is deterministic.** The first call's `summary` is written by
  the tool, not the model, so what the administrator reads before saying "yes"
  is what the system will do — not the model's paraphrase of it.

The key is per-process and random: a restart voids pending confirmations,
which for a books-keeping agent is the safe direction to fail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from nati.tools.envelope import Figures

_KEY = secrets.token_bytes(32)
_TTL_SECONDS = 600.0

# token -> (expiry_monotonic, delivered_turn). Minted here, consumed here, nowhere
# else. `delivered_turn` is the turn in which the confirmation request actually
# REACHED the administrator, recorded by `delivered()` from the agent loop — see
# `_valid` for why the gate keys on delivery rather than on minting.
_pending: dict[str, tuple[float, int | None]] = {}


def _canonical(conv_id: str | None, tool: str, args: dict[str, Any]) -> bytes:
    """The exact bytes the token signs. `confirm_token` itself is excluded."""
    stripped = {k: v for k, v in args.items() if k != "confirm_token"}
    return json.dumps(
        [conv_id or "", tool, stripped],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _mint(
    conv_id: str | None, tool: str, args: dict[str, Any], turn: int | None
) -> str:
    digest = hmac.new(
        _KEY, _canonical(conv_id, tool, args), hashlib.sha256
    ).hexdigest()[:16]
    # Minting NEVER moves the gate — only `delivered()` does. Re-minting has to
    # carry the existing delivery forward, because the model may make a failed
    # attempt in the very turn she says yes (calling without the token first, or
    # with stale arguments). If that re-mint reset the delivery, her genuine
    # confirmation would be refused and she would be asked the same question
    # twice.
    existing = _pending.get(digest)
    delivered_turn = existing[1] if existing is not None else None
    _pending[digest] = (time.monotonic() + _TTL_SECONDS, delivered_turn)
    _sweep()
    return digest


def delivered(token: str, turn: int | None) -> None:
    """Record that this confirmation request actually REACHED the administrator.

    Called by the agent loop when a turn ends with a reply she can read. It is
    the only thing that opens the gate, and that is the point: minting happens
    whenever the model calls the tool, which it can do several times inside one
    message, whereas delivery happens at most once per turn and only when she
    is actually shown the summary.
    """
    if turn is None:
        return
    entry = _pending.get(token)
    if entry is None:
        return
    expiry, previous = entry
    if previous is not None and previous >= turn:
        return  # already recorded for this turn or a later one
    _pending[token] = (expiry, turn)


def _sweep() -> None:
    """Drop expired entries so `_pending` cannot grow for the process lifetime."""
    now = time.monotonic()
    for token in [t for t, (exp, _) in _pending.items() if exp < now]:
        _pending.pop(token, None)


def _valid(
    token: str, conv_id: str | None, tool: str, args: dict[str, Any],
    turn: int | None,
) -> bool:
    """Right signature, still pending, not expired, and answered by her NEXT message.

    Consumes the token on success.
    """
    expected = hmac.new(
        _KEY, _canonical(conv_id, tool, args), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(token, expected):
        return False
    entry = _pending.get(expected)
    if entry is None:
        return False
    expiry, delivered_turn = entry
    if time.monotonic() > expiry:
        _pending.pop(expected, None)
        return False
    # THE HUMAN GATE: the token is spendable only in the turn IMMEDIATELY after
    # the one in which she was shown the summary.
    #
    # The first version of this compared against the turn the token was MINTED
    # in, and required only a later turn. Both halves were wrong.
    #
    # Minting is the wrong event: the model mints, several times per message if
    # it likes, so a mint-based gate proved the model had repeated itself, not
    # that she agreed. Delivery is the right event — it happens once a turn, and
    # only when a reply she can read actually goes out.
    #
    # "Any later turn" was the wrong test: with the earliest-mint carried
    # forward, an action proposed in turn 2 and NEVER confirmed became writable
    # in any single later turn, because the model could mint and spend inside
    # turn 5 and the check compared 5 > 2. Requiring exactly the next turn is
    # what distinguishes "she answered this" from "she was asked this once, ages
    # ago, and said nothing". The cost is that a confirmation cannot be banked:
    # if she replies with a question instead of a yes, the summary is put to her
    # again. That is the safe direction, and it is what short-lived and
    # single-use already implied.
    if turn is not None:
        if delivered_turn is None:
            return False  # never actually put to her
        if turn != delivered_turn + 1:
            return False
        # NOT consumed on any failure above. The check is READ-ONLY until
        # everything passes: an early `pop` meant a premature attempt burned a
        # perfectly good confirmation, and only a lucky re-mint put it back.

    _pending.pop(expected, None)      # single use — spent only now that it is valid
    return True


def require(
    conv_id: str | None,
    tool: str,
    args: dict[str, Any],
    summary: str,
    figures: Figures | None = None,
    turn: int | None = None,
) -> dict[str, Any] | None:
    """The gate a dangerous tool calls FIRST, after validating its arguments.

    Returns None when a valid token for exactly these arguments was presented —
    proceed with the write. Otherwise returns the envelope to send back instead:
    the confirmation request (or a fresh one, if the token was stale, belongs to
    different arguments, or was minted in this same turn).

    `turn` is the administrator's message number in this conversation. A token is
    only spendable in the turn immediately AFTER the one in which the request was
    delivered to her (see `delivered` and `_valid`), which is what forces her to
    actually say yes.
    """
    token = args.get("confirm_token")
    if isinstance(token, str) and _valid(token, conv_id, tool, args, turn):
        return None

    fresh = _mint(conv_id, tool, args, turn)
    envelope: dict[str, Any] = {
        "ok": False,
        "confirmation_required": True,
        "token": fresh,
        "summary": summary,
        "message": (
            "Nothing has been recorded yet. Read this back to the administrator "
            "and, only if she confirms, call the tool again with the same "
            f"details plus confirm_token. To confirm: {summary}"
        ),
    }
    if isinstance(token, str) and token:
        envelope["message"] = (
            "That confirmation did not match, so nothing was recorded. "
            "A confirmation is valid once, briefly, and only for exactly what "
            "was confirmed. Read the new summary back to the administrator "
            f"and try again if she agrees: {summary}"
        )
    if figures is not None and figures.items:
        envelope["figures"] = figures.as_list()
    return envelope


def reset() -> None:
    """Forget every pending confirmation. For tests."""
    _pending.clear()
