"""Tests for the web UI's markdown renderer.

The renderer is JavaScript, so the assertions live in `test_markdown.mjs` and
this module runs them under Node. Node is NOT a project requirement — the app
ships one static HTML file and needs no build step — so if Node is absent the
test skips rather than failing. Anyone changing `index.html`'s parser should
install Node and run this.

Two things checked in Python directly, because they need no JS runtime and they
guard the security property the whole design rests on: the page never sends
untrusted text into `innerHTML`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "src" / "nati" / "web"
INDEX = WEB / "index.html"
MJS = Path(__file__).resolve().parent / "test_markdown.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_markdown_renderer_behaves() -> None:
    """Runs the JS assertions: formatting, robustness, and 12 XSS probes."""
    result = subprocess.run(
        [shutil.which("node"), str(MJS)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"\n{result.stdout}\n{result.stderr}"
    assert "passed" in result.stdout


# --------------------------------------------------------------------------
# The escape-before-format ordering, asserted against the source itself
# --------------------------------------------------------------------------


def test_the_only_innerhtml_targets_are_our_own_strings() -> None:
    """Model text must reach the DOM through md.render() or textContent.

    A model reply is untrusted input. If a future edit assigns raw reply text to
    innerHTML, "make Nati emit a script tag" becomes a real break — worse than
    anything reachable in the ledger. So the allowed right-hand sides are fixed:
    md.render(...) (which escapes first), our own literal markup, or "".
    """
    source = INDEX.read_text(encoding="utf-8")
    assignments = re.findall(r"innerHTML\s*=\s*([^\n;]+)", source)
    assert assignments, "expected at least one innerHTML assignment to vet"
    for rhs in assignments:
        rhs = rhs.strip()
        allowed = (
            rhs.startswith("md.render(")     # escapes before formatting
            or rhs.startswith('""')          # clearing the log
            or rhs.startswith("`<div")       # our own static empty-state markup
            or rhs.startswith('"<span>')
            or rhs.startswith('"<span></span>')
        )
        assert allowed, f"unvetted innerHTML assignment: {rhs}"


def test_escaping_happens_before_formatting() -> None:
    """`render` must escape the whole text before any pattern is applied.

    This ordering IS the security argument. If the escape moved after the
    formatting pass, every emphasis regex would be operating on live markup.
    """
    source = INDEX.read_text(encoding="utf-8")
    body = source[source.index("function render(raw)"):]
    body = body[: body.index("function startsBlock")]
    escape_at = body.index("escapeHtml(text)")
    # No formatting may be applied to `text` before it has been escaped.
    for pattern in ("inline(", "<strong>", "<em>"):
        first = body.find(pattern)
        if first != -1:
            assert first > escape_at, (
                f"{pattern!r} is applied before escapeHtml in render()"
            )


def test_links_never_become_anchors() -> None:
    """A destination chosen by the model is a phishing surface we do not need."""
    source = INDEX.read_text(encoding="utf-8")
    module = source[source.index("const md = (() => {"):
                    source.index("// --- render: turn state into DOM")]
    assert "<a " not in module and "href" not in module


# --------------------------------------------------------------------------
# conv_id is untrusted input — it names a directory
# --------------------------------------------------------------------------


def test_a_conv_id_cannot_escape_the_trace_directory(tmp_path) -> None:
    """The audit's worst software bug: an absolute conv_id escaped entirely.

    `Path("traces") / "/tmp/evil"` == `Path("/tmp/evil")` — pathlib discards the
    base when the right side is absolute. A single POST created that directory
    and wrote meta.json / turns.jsonl / raw/*.json into it, with the request text
    landing verbatim in the JSONL.
    """
    import pytest

    from nati.agent.tracer import InvalidConvId, Tracer, new_conv_id, valid_conv_id

    hostile = [
        "/tmp/claude-1000/ABSPWNED",     # absolute — the confirmed escape
        "../../../../tmp/escaped",       # relative traversal
        "..",
        "examples",                      # overwrite the committed examples
        "20260805T203627Z-03b543/../x",
        "",
        "a" * 300,
        "has spaces",
        "20260805T203627Z-03B543",       # uppercase hex is not what we generate
    ]
    for bad in hostile:
        assert not valid_conv_id(bad), f"{bad!r} was accepted as a conv_id"
        with pytest.raises(InvalidConvId):
            Tracer(conv_id=bad, root=tmp_path)

    # Non-strings must not reach the filesystem either (a dict caused a 500).
    for bad_type in ({"a": 1}, ["x"], 42, None):
        assert not valid_conv_id(bad_type)
        with pytest.raises(InvalidConvId):
            Tracer(conv_id=bad_type, root=tmp_path)

    # A generated id is of course fine, and writes under the root.
    good = new_conv_id()
    tracer = Tracer(conv_id=good, root=tmp_path)
    tracer.conv_start({"model": "test"})
    assert (tmp_path / good / "meta.json").exists()
    assert tracer.dir.resolve().is_relative_to(tmp_path.resolve())


def test_the_endpoint_rejects_a_hostile_conv_id() -> None:
    """The server must answer 400, not 500, and must not adopt the id."""
    source = INDEX.parent.parent.joinpath("server.py").read_text(encoding="utf-8")
    assert "valid_conv_id" in source, "the endpoint does not validate conv_id"
    assert "Invalid conversation id" in source
