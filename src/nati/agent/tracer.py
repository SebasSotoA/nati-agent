"""Local conversation tracing. No network, no external service.

Every conversation is one directory under `traces/`, and every event in it is
one JSON line. Two rules make the trace trustworthy as evidence:

1. The raw model request and the raw response envelope are written **verbatim**,
   before anything interprets them — so the Chain of Thought and any dispute
   about "what did the model actually say" can be re-derived from stored bytes,
   not from our summary of them.

2. The trace shares `conv_id` + `seq` with the `audit_log` rows the tools write,
   so a world mutation can always be tied back to the exact turn that caused it.

Layout:

    traces/<conv_id>/
      meta.json                 profile, model, sha256 of prompt/tools/world, git rev
      turns.jsonl               one event per line, monotonic `seq`
      raw/<seq>.request.json     the exact body sent to the model
      raw/<seq>.response.json    the exact envelope received

`conv_id` is a UTC timestamp plus a short random tag, formed once at conv_start.
Traces are gitignored except `traces/examples/`.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nati import config


#  The EXACT shape new_conv_id() produces, and the only shape accepted.
_CONV_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


class InvalidConvId(ValueError):
    """A conversation id that will not be used as a directory name."""


def new_conv_id() -> str:
    """A sortable, unique conversation id: '20260804T140355Z-a1b2c3'."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def valid_conv_id(value: object) -> bool:
    """Whether `value` is a conv_id we are willing to turn into a path."""
    return isinstance(value, str) and bool(_CONV_ID.fullmatch(value))


@dataclass
class Tracer:
    """Writes one conversation's trace. One Tracer per conversation."""

    conv_id: str
    operator: str = "anonymous"
    root: Path = field(default_factory=lambda: config.TRACES_DIR)
    _seq: int = 0
    # Model calls so far in this conversation. Numbers the raw/ files, and never
    # resets — one turn's envelopes must not overwrite another's.
    _calls: int = 0

    def __post_init__(self) -> None:
        """Refuse to exist with a conv_id that is not safe as a directory name.

        The check lives HERE rather than only at the HTTP boundary because the
        conv_id becomes a filesystem path, and a caller that forgets to validate
        would hand an attacker an arbitrary-write primitive. The web endpoint
        takes conv_id straight from the request body, and `Path.__truediv__`
        silently DISCARDS the base when the right-hand side is absolute:

            Path("traces") / "/tmp/evil"   ==   Path("/tmp/evil")

        so an absolute conv_id escaped the trace directory entirely and wrote
        meta.json / turns.jsonl / raw/*.json wherever it pointed. Relative
        '../' escaped too. Worse than the escape: passing the id of an existing
        conversation overwrote that conversation's meta.json and appended forged
        lines to its turns.jsonl — tampering with the evidence the exercise is
        judged on.
        """
        if not valid_conv_id(self.conv_id):
            raise InvalidConvId(f"not a conversation id: {self.conv_id!r}")

    @property
    def dir(self) -> Path:
        path = self.root / self.conv_id
        # Belt and braces: even with the format check above, never write outside
        # the root. A future edit to the pattern cannot reintroduce the escape.
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise InvalidConvId(f"path escapes the trace root: {path}")
        return path

    def _write_line(self, event_type: str, data: dict[str, Any]) -> int:
        """Append one event to turns.jsonl. Returns its seq."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "conv_id": self.conv_id,
            "type": event_type,
            "data": data,
        }
        with (self.dir / "turns.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self._seq

    def conv_start(self, meta: dict[str, Any]) -> None:
        """Write meta.json once, and a conv_start line."""
        self.dir.mkdir(parents=True, exist_ok=True)
        full = {"conv_id": self.conv_id, "operator": self.operator, **meta}
        (self.dir / "meta.json").write_text(
            json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._write_line("conv_start", full)

    def user_message(self, text: str) -> int:
        return self._write_line("user_message", {"text": text})

    def llm_request(self, body: dict[str, Any]) -> None:
        """Record the request line AND persist the verbatim body to raw/.

        The file number comes from `_calls`, which counts every model call in the
        CONVERSATION. It used to come from the caller's per-turn index, which
        resets to 1 on each new question — so turn 2 overwrote turn 1's
        `raw/0001.*`, and a three-question conversation kept the envelopes of
        only the last one. The Chain of Thought survived in turns.jsonl, but the
        byte-exact envelopes that make a trace evidence did not.
        """
        self._calls += 1
        self._write_line(
            "llm_request",
            {"call": self._calls,
             "n_messages": len(body.get("messages", [])),
             "model": body.get("model"),
             "has_tools": bool(body.get("tools"))},
        )
        self._write_raw(self._calls, "request", body)

    def llm_response(
        self,
        *,
        content: str,
        reasoning: str,
        reasoning_leaked: bool,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, int],
        latency_ms: int,
        raw_response: dict[str, Any],
    ) -> None:
        """Record the response — including the Chain of Thought — and the raw envelope."""
        self._write_line(
            "llm_response",
            {
                "call": self._calls,
                "content": content,
                "reasoning": reasoning,           # the Chain of Thought, kept in full
                "reasoning_leaked": reasoning_leaked,
                "tool_calls": tool_calls,
                "usage": usage,
                "latency_ms": latency_ms,
            },
        )
        self._write_raw(self._calls, "response", raw_response)

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self._write_line("tool_call", {"name": name, "arguments": arguments})

    def tool_result(
        self, name: str, *, ok: bool, reason: str | None,
        result: dict[str, Any], internal_error: str | None,
    ) -> None:
        # The refusal `reason` and any internal_error are recorded here — this is
        # where the red team's near-misses live — but they never went to the model.
        self._write_line(
            "tool_result",
            {"name": name, "ok": ok, "reason": reason,
             "result": result, "internal_error": internal_error},
        )

    def egress(
        self, *, ok: bool, stripped_think: bool, prompt_leak: bool,
        unverified: list[str], original: str,
    ) -> None:
        """The output filter's verdict on a reply. `original` is what the model
        wrote; when blocked, what the user saw instead is a fixed sentence."""
        self._write_line(
            "egress",
            {"ok": ok, "stripped_think": stripped_think,
             "prompt_leak": prompt_leak, "unverified": unverified,
             "original": original},
        )

    def note(self, text: str, author: str = "operator") -> int:
        return self._write_line("note", {"text": text, "author": author})

    def conv_end(self, reason: str, totals: dict[str, Any]) -> None:
        self._write_line("conv_end", {"reason": reason, "totals": totals})

    def resume_seq(self) -> None:
        """Continue numbering from an existing turns.jsonl. Does not rewrite meta."""
        path = self.dir / "turns.jsonl"
        if not path.exists():
            return
        last_seq = 0
        last_call = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            last_seq = max(last_seq, int(record.get("seq") or 0))
            data = record.get("data") or {}
            if record.get("type") in ("llm_request", "llm_response"):
                last_call = max(last_call, int(data.get("call") or 0))
        self._seq = last_seq
        self._calls = last_call

    def _write_raw(self, seq_hint: int, kind: str, payload: dict[str, Any]) -> None:
        raw_dir = self.dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{seq_hint:04d}.{kind}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
