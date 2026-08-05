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

# token -> (expiry_monotonic, minted_turn). Minted here, consumed here, nowhere
# else. `minted_turn` is what makes the administrator's "yes" mandatory: see
# `require`.
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
    _pending[digest] = (time.monotonic() + _TTL_SECONDS, turn)
    _sweep()
    return digest


def _sweep() -> None:
    """Drop expired entries so `_pending` cannot grow for the process lifetime."""
    now = time.monotonic()
    for token in [t for t, (exp, _) in _pending.items() if exp < now]:
        _pending.pop(token, None)


def _valid(
    token: str, conv_id: str | None, tool: str, args: dict[str, Any],
    turn: int | None,
) -> bool:
    """Right signature, still pending, not expired, and from an EARLIER turn.

    Consumes the token on success.
    """
    expected = hmac.new(
        _KEY, _canonical(conv_id, tool, args), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(token, expected):
        return False
    entry = _pending.pop(expected, None)
    if entry is None:
        return False
    expiry, minted_turn = entry
    if time.monotonic() > expiry:
        return False
    # THE HUMAN GATE. Without this the model could mint a token and spend it
    # inside the same user turn — the loop allows several model calls per
    # message — so what the mechanism enforced was "the model repeated itself",
    # not "the administrator agreed". Requiring a later turn means her message
    # has to arrive in between.
    if turn is not None and minted_turn is not None and turn <= minted_turn:
        return False
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
    only spendable in a LATER turn than the one that minted it, which is what
    forces her to actually say yes.
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
