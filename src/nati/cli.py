"""Nati's CLI.

`doctor` is the highest-return thing in the project: every failure prints the
exact command to fix it, so a teammate does not lose their first hour guessing.
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.table import Table

from nati import __version__, config
from nati.config import ConfigError
from nati.world.db import WorldError

console = Console()

# (visible word, colour). Padding is measured on the word, not on the markup, so
# the columns line up.
OK = ("OK", "green")
FAIL = ("FAIL", "red")
WARN = ("WARN", "yellow")

_STATUS_WIDTH = 6
_LABEL_WIDTH = 20


def _line(status: tuple[str, str], label: str, detail: str = "") -> None:
    word, colour = status
    pad = " " * (_STATUS_WIDTH - len(word))
    line = f"  [{colour}]{word}[/{colour}]{pad}  {label:<{_LABEL_WIDTH}}"
    if detail:
        line += f"  [dim]{detail}[/dim]"
    console.print(line)


def _detail(text: str) -> None:
    """An indented sub-line: the exact command that fixes the failure."""
    for line in text.splitlines():
        console.print(f"          [dim]{line}[/dim]")


def _usd(n: int) -> str:
    return f"-${abs(n):,}" if n < 0 else f"${n:,}"


def cmd_profiles(_args: argparse.Namespace) -> int:
    active = config.load_profile()
    table = Table(title="Available profiles", title_style="bold")
    table.add_column("profile")
    table.add_column("model")
    table.add_column("region")
    table.add_column("description", overflow="fold")
    for name in config.list_profiles():
        p = config.load_profile(name)
        label = f"[bold]{p.name}[/bold] (active)" if p.name == active.name else p.name
        table.add_row(label, p.model, p.region, p.description)
    console.print(table)
    console.print(
        "\n[dim]Nothing in the code depends on which model is used. To switch:"
        "\n  edit profiles.toml, or  --profile <name>,"
        "\n  or  NATI_MODEL=<bedrock-model-id>  for one run.[/dim]\n"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    console.print(f"\n[bold]nati doctor[/bold]  v{__version__}\n")
    problems = 0

    # --- Python ---
    v = sys.version_info
    if (3, 11) <= (v.major, v.minor) < (3, 15):
        _line(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        problems += 1
        _line(FAIL, "Python", f"{v.major}.{v.minor} — needs >=3.11,<3.15")
        _detail("Run:  uv sync   (uv downloads 3.12 for you)")

    # --- Profile and model ---
    try:
        profile = config.load_profile(args.profile)
    except ConfigError as exc:
        _line(FAIL, "Profile", "")
        _detail(str(exc))
        return 1

    _line(OK, "Profile", f"{profile.name} @ {profile.region}")
    override = config.model_override()
    _line(OK, "Model", profile.model + ("  (NATI_MODEL override)" if override else ""))
    if not profile.reasoning_effort:
        _detail("This profile does not request a Chain of Thought "
                "(reasoning_effort is empty).")

    # --- World ---
    if config.SCHEMA_SQL.exists() and config.SEED_SQL.exists():
        _line(OK, "World SQL", "schema.sql + seed.sql present")
    else:
        problems += 1
        _line(FAIL, "World SQL", "schema.sql or seed.sql missing")

    if config.DB_PATH.exists():
        _line(OK, "World database", str(config.DB_PATH.relative_to(config.ROOT)))
    else:
        _line(WARN, "World database", "does not exist yet")
        _detail("Run:  uv run nati seed --reset")

    # --- Key ---
    if config.api_key_present():
        _line(OK, "Bedrock key", f"loaded from {config.key_source()}")
    else:
        problems += 1
        _line(FAIL, "Bedrock key", "I cannot find it")
        if config.ENV_FILE.exists():
            _detail(".env exists but NATI_API_KEY is empty. Paste the key inside.")
        else:
            _detail("Run:  cp .env.example .env    and paste the key inside")
            _detail("  or: export NATI_API_KEY=<the team's key>")

    who = config.operator()
    if who == "anonymous":
        _line(WARN, "NATI_USER", "not set; traces will say 'anonymous'")
        _detail("Put it in .env:  NATI_USER=<your name>")
    else:
        _line(OK, "NATI_USER", who)

    # --- Round trip with tool calling ---
    if not config.api_key_present():
        _line(WARN, "Round trip", "skipped: no key")
    elif args.offline:
        _line(WARN, "Round trip", "skipped: --offline")
    else:
        problems += _test_tool_calling(profile)

    console.print()
    if problems:
        console.print(
            f"[red]{problems} problem(s).[/red] Fix the above and run it again.\n"
        )
        return 1
    console.print(
        "[green]All set.[/green]  Try:  uv run nati tools -r fund_status\n"
    )
    return 0


def _test_tool_calling(profile) -> int:
    """Proves the model can actually call a tool. This is the Phase 1 gate."""
    from nati.providers import ProviderError, complete

    test_tool = {
        "type": "function",
        "function": {
            "name": "add_dollars",
            "description": "Adds two amounts in US dollars.",
            # parameters must be a JSON OBJECT, not a string — the most common
            # cause of a rejected tool definition, on any provider.
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "First amount"},
                    "b": {"type": "integer", "description": "Second amount"},
                },
                "required": ["a", "b"],
            },
        },
    }
    messages = [
        {"role": "system", "content": "You are a connection test. Use the tool."},
        {"role": "user", "content": "What is 15 plus 27? Use the tool."},
    ]

    try:
        turn = complete(messages, [test_tool], profile)
    except ProviderError as exc:
        _line(FAIL, "Round trip", "")
        _detail(str(exc))
        return 1

    detail = f"{turn.latency_ms} ms, {turn.usage['total_tokens']} tokens"
    if turn.tool_calls:
        call = turn.tool_calls[0]
        status = OK if call.parse_ok else FAIL
        _line(status, "Tool calling", f"{call.name}({call.arguments_raw}) — {detail}")
        if not call.parse_ok:
            _detail(f"Arguments could not be parsed: {call.parse_error}")
            return 1
    else:
        # Not fatal by itself, but Nati is useless without tool calling, so say so
        # plainly. Any model can be configured; not every model can drive tools.
        _line(WARN, "Tool calling", f"the model did not call the tool — {detail}")
        _detail(f"It replied: {turn.content[:120]!r}")
        _detail("Nati cannot work without tool calling. If this model does not")
        _detail("support it, pick another one in profiles.toml.")

    if turn.reasoning:
        mark = " (was in content, filtered out)" if turn.reasoning_leaked else ""
        _line(OK, "Chain of Thought", f"{len(turn.reasoning)} characters{mark}")
    else:
        _line(WARN, "Chain of Thought", "this model returned no reasoning")
        _detail("Not every model exposes one. The traces still record everything")
        _detail("else; the Chain of Thought column will simply be empty.")

    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from nati.world import db

    try:
        path = db.create(reset=args.reset)
    except WorldError as exc:
        console.print(f"\n[red]{exc}[/red]\n")
        return 1

    con = db.connect(path)
    try:
        members = con.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        console.print(
            f"\n[green]World built[/green]  {path.relative_to(config.ROOT)}\n"
            f"  {members} members · hash [dim]{db.world_hash(con)[:16]}…[/dim]\n"
        )
    finally:
        con.close()
    return 0


def cmd_fund(args: argparse.Namespace) -> int:
    """Shows the fund. Useful for seeing the arithmetic without the model."""
    from nati.world import db
    from nati.world import queries as q

    upto = args.date
    con = db.connect()
    try:
        f = q.fund(con, upto)
        personal = q.personal_interest(con, upto)

        t = Table(title=f"The natillera's fund as of {upto}", title_style="bold")
        t.add_column("item")
        t.add_column("amount", justify="right")
        for label, amount in [
            ("Savings received", f.savings),
            ("Capital lent out", -f.capital_out),
            ("Payments received", f.payments_in),
            ("Raffle income", f.raffle_income),
            ("Prizes paid", -f.prizes_paid),
        ]:
            t.add_row(label, _usd(amount))
        t.add_section()
        t.add_row("[bold]Cash available[/bold]", f"[bold]{_usd(f.cash)}[/bold]")
        t.add_row("Out on loan", _usd(f.loans_outstanding))
        t.add_row("[bold]Equity[/bold]", f"[bold]{_usd(f.equity)}[/bold]")
        t.add_section()
        t.add_row("Interest charged (fund)", _usd(f.interest_charged))
        t.add_row("Interest collected (fund)", _usd(f.interest_collected))
        t.add_row("Raffle profit", _usd(f.raffle_profit))
        t.add_row(
            "[bold]Distributable profits[/bold]", f"[bold]{_usd(f.profits)}[/bold]"
        )
        console.print(t)

        console.print(
            "\n[dim]Interest charged is not cash: it arrives when the member pays.[/dim]"
        )

        if personal:
            console.print("\n[bold]Outside the fund — personal money[/bold]")
            for lender_id, collected in sorted(personal.items()):
                row = con.execute(
                    "SELECT name FROM lenders WHERE id = ?", (lender_id,)
                ).fetchone()
                console.print(
                    f"  {row['name']}: interest collected {_usd(collected)} "
                    "[dim](not part of the payout)[/dim]"
                )
    finally:
        con.close()
    return 0


def cmd_payout(args: argparse.Namespace) -> int:
    """Simulates the year-end payout. Moves nothing."""
    from nati.world import db
    from nati.world import queries as q

    con = db.connect()
    try:
        p = q.payout(con, args.date)

        t = Table(title=f"Simulated payout as of {args.date}", title_style="bold")
        t.add_column("member", overflow="fold")
        t.add_column("saved", justify="right")
        t.add_column("profit", justify="right")
        t.add_column("loan\ndebt", justify="right")
        t.add_column("raffle\ndebt", justify="right")
        t.add_column("receives", justify="right")
        t.add_column("still\nowes", justify="right")
        for s in p.shares:
            t.add_row(
                s.name,
                _usd(s.saved),
                _usd(s.profit),
                _usd(s.loan_debt) if s.loan_debt else "—",
                _usd(s.ticket_debt) if s.ticket_debt else "—",
                _usd(s.to_pay),
                _usd(s.still_owes) if s.still_owes else "—",
            )
        t.add_section()
        t.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{_usd(p.total_savings)}[/bold]",
            f"[bold]{_usd(p.total_profits)}[/bold]",
            f"[bold]{_usd(sum(s.loan_debt for s in p.shares))}[/bold]",
            f"[bold]{_usd(sum(s.ticket_debt for s in p.shares))}[/bold]",
            f"[bold]{_usd(p.total_to_pay)}[/bold]",
            f"[bold]{_usd(p.total_still_owed)}[/bold]" if p.total_still_owed else "—",
        )
        console.print(t)
        console.print(
            "\n[dim]Profits are split equally. Savings belong to each person.[/dim]"
        )

        check = q.payout_balance_check(con, args.date)
        _line(OK if check.ok else FAIL, "Balances", check.text)
        if p.cash_is_enough:
            _line(OK, "Cash is enough", f"available {_usd(p.cash_available)}")
        else:
            short = p.total_to_pay - p.cash_available
            _line(WARN, "NOT enough cash", f"{_usd(short)} short")
            _detail("The loans have to be collected before paying out.")
        if p.total_still_owed:
            _detail(
                f"Nobody is paid a negative amount: {_usd(p.total_still_owed)} is "
                "still to collect."
            )
        console.print("\n[dim]This is only a simulation: it moves no money.[/dim]\n")
    finally:
        con.close()
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """Lists the tools, or runs one by hand. Useful for testing without the model."""
    from nati import clock
    from nati.tools import registry
    from nati.world import db

    if args.run is None:
        t = Table(title=f"{len(registry.TOOLS)} tools", title_style="bold")
        t.add_column("name")
        t.add_column("kind")
        t.add_column("arguments")
        t.add_column("what it does", overflow="fold")
        for tool in registry.TOOLS:
            props = tool.parameters["properties"]
            required = set(tool.parameters.get("required", []))
            arguments = ", ".join(
                f"[bold]{p}[/bold]" if p in required else p for p in props
            ) or "—"
            t.add_row(
                tool.name,
                "[yellow]writes[/yellow]" if tool.writes else "reads",
                arguments,
                tool.description.split(".")[0] + ".",
            )
        console.print(t)
        console.print(
            f"\n[dim]Required arguments in bold. "
            f"Schema sha256: {registry.schemas_hash()[:16]}…[/dim]"
        )
        console.print("[dim]To try one:  uv run nati tools -r fund_status[/dim]\n")
        return 0

    if args.run not in registry.BY_NAME:
        console.print(f"\n[red]There is no tool called '{args.run}'.[/red]\n")
        return 1

    try:
        arguments = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as exc:
        console.print(f"\n[red]The arguments are not valid JSON:[/red] {exc}\n")
        return 1

    clock.set_today(args.date)
    con = db.connect()
    try:
        result = registry.dispatch(
            con, args.run, arguments, db.Ctx(conv_id="cli", seq=1)
        )
    finally:
        con.close()

    status = OK if result.for_the_model.get("ok") else FAIL
    _line(status, args.run, result.for_the_model.get("message", ""))
    if result.reason:
        _detail(f"internal reason (the model does not see this): {result.reason}")
    if result.internal_error:
        _detail(f"internal error: {result.internal_error}")
    console.print()
    console.print_json(json.dumps(result.for_the_model, ensure_ascii=False))
    console.print()
    return 0 if result.for_the_model.get("ok") else 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the web chat server."""
    if not config.api_key_present():
        console.print(
            "\n[red]No Bedrock key found.[/red] The chat needs the model.\n"
            "  Run:  cp .env.example .env    and paste the key inside\n"
        )
        return 1
    if not config.DB_PATH.exists():
        # First run after a clone: build the world so `serve` is all it takes.
        # The seed is deterministic and versioned, so this is always safe.
        from nati.world import db

        try:
            db.create(reset=False)
            console.print("[dim]Built var/world.db from the versioned seed.[/dim]")
        except WorldError as exc:
            console.print(f"\n[red]{exc}[/red]\n")
            return 1

    from nati import server

    url = f"http://{args.host}:{args.port}"
    console.print(
        f"\n[green]Nati is listening at[/green] [bold]{url}[/bold]\n"
        "  Open that in your browser to chat.  Ctrl+C to stop.\n"
        "  Every conversation is traced under [dim]traces/[/dim].\n"
    )
    try:
        server.serve(args.host, args.port, args.profile)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nati",
        description="Natillera Bot — savings-group assistant.",
    )
    parser.add_argument("--version", action="version", version=f"nati {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="Check that everything is ready to run.")
    d.add_argument("--profile", "-p", default=None,
                   help="Profile to test. Defaults to NATI_PROFILE or profiles.toml.")
    d.add_argument(
        "--offline",
        action="store_true",
        help="Do not call the model; only check local configuration.",
    )
    d.set_defaults(func=cmd_doctor)

    p = sub.add_parser("profiles", help="List the model profiles.")
    p.set_defaults(func=cmd_profiles)

    s = sub.add_parser("seed", help="Build the world from schema.sql + seed.sql.")
    s.add_argument("--reset", action="store_true", help="Delete and rebuild.")
    s.set_defaults(func=cmd_seed)

    f = sub.add_parser("fund", help="The state of the common fund (no model needed).")
    f.add_argument("--date", default=config.WORLD_TODAY, help="Cut-off date.")
    f.set_defaults(func=cmd_fund)

    o = sub.add_parser("payout", help="Simulate the year-end payout.")
    o.add_argument("--date", default=config.WORLD_TODAY, help="Cut-off date.")
    o.set_defaults(func=cmd_payout)

    t = sub.add_parser("tools", help="List the tools, or run one by hand.")
    t.add_argument("--run", "-r", default=None, help="Tool name.")
    t.add_argument("--args", "-a", default=None, help='Arguments as JSON: \'{"month":7}\'')
    t.add_argument("--date", default=config.WORLD_TODAY, help="Freeze 'today'.")
    t.set_defaults(func=cmd_tools)

    v = sub.add_parser("serve", help="Start the web chat (talk to Nati in a browser).")
    v.add_argument("--profile", "-p", default=None, help="Model profile to use.")
    v.add_argument("--host", default="127.0.0.1", help="Bind host.")
    v.add_argument("--port", default=8010, type=int, help="Bind port.")
    v.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        console.print(f"\n[red]Configuration error:[/red] {exc}\n")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
