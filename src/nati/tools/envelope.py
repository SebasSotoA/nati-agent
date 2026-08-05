"""The envelope every tool returns. Never empty, never revealing.

Rules of the envelope:

1. **Never empty.** Some models reject a tool result with no content outright,
   and that includes "I found nothing" — so there is always at least an `ok`
   flag and a plain-English sentence. It costs nothing and removes a whole class
   of model-specific failure.
2. **The `reason` does not travel to the model.** It goes to the trace and the
   audit log. If the model could see it, an attacker would enumerate the whole
   taxonomy of controls by deliberately asking for invalid things.
3. **Refusals carry no figures.** "That payment exceeds the balance of $1,234"
   would be an unaudited read through the write door, and a way to discover
   balances through failed attempts.
4. **Every figure travels in `figures[]`**, tagged with its field. The output
   filter requires that any number Nati writes appears there; if it does not, the
   reply does not go out. That is why the list holds {field, value, text} rather
   than a flat set of values: one `raffle_status` call puts ~58 two-digit numbers
   in play, and with a flat value list the model could write almost any small
   number and pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


def usd(n: int) -> str:
    """US dollar formatting: comma thousands, no cents. The only formatter.

    The sign goes before the symbol so a negative reads -$12,000 and not
    $-12,000.
    """
    if n < 0:
        return f"-${abs(n):,}"
    return f"${n:,}"


@dataclass
class Figures:
    """Collects the figures the model is allowed to name."""

    items: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def money(self, field: str, value: int) -> int:
        self.items.append({"field": field, "value": value, "text": usd(value)})
        return value

    def number(self, field: str, value: int) -> int:
        self.items.append({"field": field, "value": value, "text": str(value)})
        return value

    def date(self, field: str, value: str) -> str:
        self.items.append({"field": field, "value": value, "text": value})
        return value

    def as_list(self) -> list[dict[str, Any]]:
        return self.items


def ok(
    message: str,
    *,
    figures: Figures | None = None,
    warnings: list[str] | None = None,
    **data: Any,
) -> dict[str, Any]:
    """A successful result. `message` is the sentence Nati may say verbatim."""
    envelope: dict[str, Any] = {"ok": True, "message": message}
    envelope.update(data)
    if figures is not None and figures.items:
        envelope["figures"] = figures.as_list()
    if warnings:
        # Deterministic warnings, written by us. The model never composes a
        # warning: it repeats one.
        envelope["warnings"] = warnings
    return envelope


def refuse(
    reason: str, message: str, *, can_retry: bool = True
) -> dict[str, Any]:
    """A refusal. The `reason` is stripped out later, before anything is sent.

    It is returned inside the envelope under the key `_reason` (leading
    underscore) so that `registry.dispatch` can pull it out for the trace and
    keep it away from the model.
    """
    return {
        "ok": False,
        "message": message,
        "can_retry": can_retry,
        "_reason": reason,
    }


def split_reason(envelope: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Remove `_reason` from the envelope. Returns (for_the_model, reason)."""
    if "_reason" not in envelope:
        return envelope, None
    clean = {k: v for k, v in envelope.items() if k != "_reason"}
    return clean, envelope["_reason"]
