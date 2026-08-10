# Natillera Bot ("Nati")

A **natillera** is a Colombian community savings group: people save together
through the year, lend to each other with interest, run raffles, and split
everything before December. One member, the **administrator**, keeps the books.

Nati is her assistant. It answers questions about the fund and records the day's
entries — savings, loan payments, interest, raffle tickets — under rules that the
code enforces rather than merely describes.

## Quickstart

```bash
uv sync                     # Python 3.12 + deps
cp .env.example .env        # paste your AWS Bedrock key
uv run nati serve           # http://127.0.0.1:8010
```

The key is the only thing you configure. The model is fixed in
[`profiles.toml`](profiles.toml); the database builds itself on first run.
`uv run nati doctor` diagnoses anything wrong; `uv run nati seed --reset`
restores clean books.

⚠️ **`.env` is gitignored — never commit it.** The Bedrock key can incur spend.

## How the rules are enforced

[`docs/SOP.md`](docs/SOP.md) holds the **52 rules** of the domain, each tagged
**HARD** (the code refuses, however it is asked) or **SOFT** (judgment). §9 maps
every rule to the exact place it is enforced.

Each rule sits at the strongest layer that can hold it:

| Strength | Mechanism |
|---|---|
| Strongest | **the argument does not exist** — payments take no `member_id`, so crediting the wrong person is unsayable |
| Very strong | **DB triggers / CHECKs** — a loan's funder never changes; nothing is ever deleted |
| Strong | **one write chokepoint** — `db.insert`/`db.update`, which also writes the audit row |
| Medium | **tool gates + tokens** — the 6 dangerous tools need a token spendable only in the turn *after* she was shown the summary |
| On the way out | **the output filter** — refuses a dollar amount no tool returned, a tool name, leaked reasoning |
| Weakest | **the prompt** |

Bypassing the tools entirely and writing raw SQL still cannot change a loan's
funder, delete a record, revive a cancelled loan, edit the audit log, or record
negative money.

Two properties hold the rest up: the model **never does arithmetic** — every
figure comes from [`calc.py`](src/nati/tools/calc.py) — and **every write goes
through one function**, so no code path can skip the audit trail. Refusals are
audited too, not just successes.

## The 19 tools

**8 read** · **5 daily write** (one call) · **6 rare write** (two-phase: the
first call returns a summary and a token, and the write lands only when that
token comes back in the turn *after* the summary actually reached her — which is
what makes her confirmation mechanically necessary rather than merely expected).

**Deliberately absent:** no delete, edit, un-void, un-pay, or payout tool.

Inspect the whole world with **no model involved**:

```bash
uv run nati tools                                        # all 19 + arguments
uv run nati tools -r member_status -a '{"member_id": 2}'  # run one by hand
uv run nati fund && uv run nati payout                   # the books, balanced
uv run pytest -q                                         # 414 tests, no network
```

## Traces

Every conversation is recorded locally under `traces/<conv_id>/`:

```
meta.json     model + sha256 of the prompt, tool schemas and world
turns.jsonl   one event per line: user_message · llm_request · llm_response ·
              tool_call · tool_result · egress · note · conv_end
raw/NNNN.*    the exact request and response bodies, verbatim
```

It holds the **full Chain of Thought** (never shown in the UI), **every tool call
including refusals** and their internal reason codes, and the **output filter's
verdict** with any suppressed text. Writes share the `conv_id` via `audit_log`,
so any change traces back to the turn that caused it.

Note when reading one: if the filter suppresses a reply, the model receives one
correction and answers again — so the reply shown in the UI may be the second
attempt. Both are in `turns.jsonl`.

```bash
jq -r 'select(.type=="llm_response").data.reasoning' traces/<id>/turns.jsonl
jq -c 'select(.type=="egress").data|{ok,cause,unverified}' traces/<id>/turns.jsonl
```

Two example conversations ship in [`traces/examples/`](traces/examples/).

## Layout

```
docs/SOP.md   the rules                   docs/GLOSSARY.md  Spanish → English
profiles.toml the only place a model is named
src/nati/agent/  loop.py (tool loop) · egress.py (output filter) · tracer.py
src/nati/tools/  registry.py (19 schemas) · reads · writes · writes_rare
                 gates.py · confirm.py · calc.py
src/nati/world/  schema.sql (10 tables, 21 triggers, 24 CHECKs) · db.py · seed.sql
```

## The world

Built from versioned SQL, identical for everyone, resettable in one command.
At the cut-off (2026-08-04): **29 members**, $15,220 saved, $15,732 cash, $2,121
out on loan, $17,853 equity, $2,633 distributable profits, **11 loans**, 261
raffle tickets.

Deliberate tension: cash would fall short of a full payout today, because money
is out on loan.

**The sharpest rule:** six people lend their *own* money alongside the fund.
Interest on private money belongs to that person alone and must never enter the
fund or the payout. Whose money funded a loan is fixed at creation and **can
never be inferred — only stated.**

## Scope

Single cycle, single administrator. Multi-year behaviour and members leaving
mid-cycle are out of scope and untested. A few domain values are provisional.
`docs/SOP.md` §11 states the limits in full.

---

All data is synthetic. Internal project, not deployed.
