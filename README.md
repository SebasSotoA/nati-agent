# Natillera Bot ("Nati")

A **natillera** is a Colombian community savings group: people save together
through the year, lend to each other with interest, run raffles, and split
everything before December. One member, the **administrator**, keeps the books.

Nati is her assistant. In production she talks to it on **WhatsApp**. The local
web chat (`nati serve`) is a developer harness only.

The code enforces the books: it will not invent a dollar figure, send the fund
negative, or silently delete history.

## Local quickstart

```bash
uv sync                     # Python 3.12 + deps
cp .env.example .env        # paste your Ollama Cloud API key
uv run nati seed --reset    # empty production books
uv run nati bootstrap --name "Your Name" --phone 3101234567
uv run nati serve           # http://127.0.0.1:8010  (developer chat)
```

The model is named in [`profiles.toml`](profiles.toml). Override it with
`NATI_MODEL`. `uv run nati doctor` diagnoses anything wrong.

`.env` is gitignored — never commit it.

## Production (Fly.io + WhatsApp)

One always-on machine: 512 MB RAM, 5 GB volume at `/data`, internal port **8080**.
Do not enable Managed Postgres. The public host serves `/api/health` and
`/webhook/whatsapp` only (`NATI_PUBLIC_CHAT=0`).

```bash
fly volumes create nati_data --region iad --size 5
fly secrets set OLLAMA_API_KEY=... NATI_MODEL=...
fly secrets set NATI_ADMIN_PHONE=57... WHATSAPP_TOKEN=... \
  WHATSAPP_PHONE_NUMBER_ID=... WHATSAPP_APP_SECRET=... WHATSAPP_VERIFY_TOKEN=...
fly deploy
```

Do not put secrets in the GitHub launch form — they can land in `fly.toml`.
Webhook URL for Meta: `https://<app>.fly.dev/webhook/whatsapp`.
`NATI_ADMIN_PHONE` is Colombian: 10 national digits or `57` plus those 10.
The web chat is off unless you bind localhost; a public bind never serves `/api/chat`.

If the administrator always messages first, WhatsApp **service** replies are free.

## How the rules are enforced

[`docs/SOP.md`](docs/SOP.md) holds the rules of the domain, each tagged
**HARD** (the code refuses) or **SOFT** (judgment). §9 maps every rule to the
exact place it is enforced.

| Strength | Mechanism |
|---|---|
| Strongest | **the argument does not exist** — payments take no `member_id` |
| Very strong | **DB triggers / CHECKs** — a loan's funder never changes; normal tools cannot delete |
| Strong | **one write chokepoint** — `db.insert`/`db.update`, plus the audit row |
| Medium | **tool gates + tokens** — rare writes need a token spendable only in the turn *after* the summary reached her |
| On the way out | **the output filter** — refuses a dollar amount no tool returned, a tool name, leaked reasoning |
| Weakest | **the prompt** |

`reset_cycle` is the only delete path: it archives the current year first, behind
a confirmation token. `start_cycle` opens the next year; members carry forward.

Two properties hold the rest up: the model **never does arithmetic** — every
figure comes from [`calc.py`](src/nati/tools/calc.py) — and **every write goes
through one function**.

## The 21 tools

**8 read** · **5 daily write** (one call) · **8 rare write** (two-phase
confirmation). Rare writes include `start_cycle` and `reset_cycle`.

**Deliberately absent:** no edit, un-void, un-pay, or payout-execution tool.

```bash
uv run nati tools
uv run nati fund && uv run nati payout
uv run pytest -q
```

## Traces

Every conversation is recorded under `traces/<conv_id>/` (on Fly: `/data/traces`).
Writes share `conv_id` with `audit_log`.

## Layout

```
docs/SOP.md   the rules                   docs/GLOSSARY.md  Spanish → English
profiles.toml the only place a model is named
fly.toml      Fly.io: 512 MB, port 8080, volume /data
src/nati/agent/    loop · egress · tracer · prompt
src/nati/channels/ WhatsApp adapter + session store
src/nati/tools/    registry (21 schemas) · reads · writes · writes_rare
src/nati/world/    schema.sql · bootstrap.sql · db.py
```

## The books

A production seed is **empty**: one fund lender, dated settings, no members.
Add the administrator with `nati bootstrap`. The 29-member 2026 world is a
**test fixture** only (`tests/fixtures/challenge_world.sql`).

**The sharpest rule:** there are two money sources — the common fund, and the
administrator's *own* money. Interest on her own money is hers alone and must
never enter the fund or the payout. Whose money funded a loan is fixed at
creation and **can never be changed.**
