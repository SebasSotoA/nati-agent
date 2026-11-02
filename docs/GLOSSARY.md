# Glossary — Spanish domain → English code

This project models a **natillera**, a Colombian community savings group. The domain is
Spanish; the code, docs and the agent's speech are English so the whole team can read them.

This file is the **single authority for every name**. If a term is not here, it does not
get invented in a source file — it gets added here first. Without that discipline the same
concept ends up as `payment` in one module, `installment` in another, and `abono` in a
third.

## What a natillera is

29 people save together for a year. They lend to each other with interest, run two
mandatory raffles, and split everything before December — the savings exist to be spent on
December shopping. One member, the **administrator**, keeps the books.

The word `natillera` is **kept untranslated** throughout. It is the proper noun for this
specific institution; "savings circle", "ROSCA", "tanda" and "susu" are all different
regional variants, and translating it would make the domain generic.

## Domain terms

| Spanish | English | Notes |
|---|---|---|
| natillera | natillera | kept — no clean equivalent |
| socio / socia | member | 29 of them |
| administradora | administrator | the single user of the agent |
| ahorro | saving | a contribution; no fixed quota |
| préstamo | loan | uncapped by group decision |
| prestamista | lender | either the fund, or a person's own money |
| abono | payment | a payment against a loan |
| interés | interest | on the **original** amount, never the balance |
| boleta | raffle ticket | monthly has 2 numbers, quarterly has 1 |
| rifa | raffle | mandatory, the group's main profit source |
| sorteo | draw | the monthly/quarterly drawing |
| premio | prize | fixed amount |
| reparto | payout | year-end distribution |
| fondo | fund | the common pot |
| caja | cash on hand | what the fund can actually pay today |
| cartera | outstanding loans | capital that is out on loan |
| utilidades | profits | split **equally**, unlike savings |
| ciclo | cycle | months from `cycle_first_month` / `cycle_last_month` (default January–November of one year) |
| saldo | balance | what remains to be paid |
| vigencia | effective date | settings are dated, never overwritten |

## Tables

The six **domain** tables came verbatim from the administrator's real PostgreSQL schema.
They are translated here for readability; this table preserves the provenance.

| Original (her schema) | English | Domain or operational |
|---|---|---|
| `usuarios` | `members` | domain — hers |
| `ahorros` | `savings` | domain — hers |
| `prestamos` | `loans` | domain — hers |
| `pagos_prestamo` | `loan_payments` | domain — hers |
| `intereses_prestamo` | `loan_interest` | domain — hers |
| `boletas_rifa` | `raffle_tickets` | domain — hers |
| `prestamistas` | `lenders` | added — the fund/personal boundary |
| `configuracion` | `settings` | added — dated key/value |
| `sorteos` | `draws` | added — her schema had nowhere for the result |
| `audit_log` | `audit_log` | added — unchanged |

Two columns were added to her tables and one changed shape. All three are documented in
[`OPEN_QUESTIONS.md`](../OPEN_QUESTIONS.md) with the reason:

- `loans.lender_id` — whose money funded the loan. Immutable.
- `raffle_tickets.kind` + nullable `number_2` — she runs **two** raffles, and the original
  table could only hold one ticket per member per month, always with two numbers.
- `raffle_tickets.price` — frozen at sale, so no later settings change can revalue a ticket.

## Columns

| Spanish | English |
|---|---|
| nombre | name |
| telefono | phone |
| fecha_ingreso | joined_on |
| fecha | date |
| monto | amount |
| monto_original | principal |
| tasa_mensual | monthly_rate |
| fecha_inicio | started_on |
| estado | status |
| prestamista_id | lender_id |
| interes_generado | interest_amount |
| tipo | kind |
| mes / anio | month / year |
| fecha_pago | paid_on |
| pagada | paid |
| precio | price |
| numero_1 / numero_2 | number_1 / number_2 |
| numero_ganador | winning_number |
| premio | prize |
| clave / valor | key / value |
| vigente_desde | effective_from |
| nota | note |

## Enumerated values

| Spanish | English |
|---|---|
| `activo` | `active` |
| `cerrado` | `closed` |
| `anulado` | `voided` |
| `mensual` | `monthly` |
| `trimestral` | `quarterly` |
| `fondo` | `fund` |
| `propio` | `personal` |

## Settings keys

| Spanish | English |
|---|---|
| `anio_ciclo` | `cycle_year` |
| `admin_usuario_id` | `admin_member_id` |
| `mes_inicio_ciclo` / `mes_fin_ciclo` | `cycle_first_month` / `cycle_last_month` |
| `precio_boleta_mensual` | `monthly_ticket_price` |
| `precio_boleta_trimestral` | `quarterly_ticket_price` |
| `premio_mensual` | `monthly_prize` |
| `premio_trimestral` | `quarterly_prize` |

## Tools

| Spanish | English | Reads or writes |
|---|---|---|
| `socios` | `members` | read |
| `estado_socio` | `member_status` | read |
| `estado_fondo` | `fund_status` | read |
| `estado_prestamo` | `loan_status` | read |
| `listar_prestamos` | `list_loans` | read |
| `estado_rifa` | `raffle_status` | read |
| `simular_reparto` | `simulate_payout` | read |
| `verificar_integridad` | `check_integrity` | read |
| `registrar_ahorro` | `record_saving` | write |
| `registrar_abono` | `record_payment` | write |
| `registrar_interes_mensual` | `accrue_monthly_interest` | write |
| `registrar_boleta` | `sell_raffle_ticket` | write |
| `marcar_boleta_pagada` | `mark_ticket_paid` | write |
| `registrar_socio` | `add_member` | write (two-phase) |
| `registrar_prestamo` | `record_loan` | write (two-phase) |
| `anular_prestamo` | `void_loan` | write (two-phase) |
| `registrar_reversa` | `record_reversal` | write (two-phase, savings only) |
| `registrar_sorteo` | `record_draw` | write (two-phase) |
| `set_configuracion` | `set_setting` | write (two-phase) |
| `iniciar_ciclo` | `start_cycle` | write (two-phase) |
| `reiniciar_ciclo` | `reset_cycle` | write (two-phase) |

21 tools total: 8 read, 13 write. The last eight writes above take a
`confirm_token`: the first call returns a summary and a token, and the write
happens only on a second call carrying that token (see
[`tools/confirm.py`](../src/nati/tools/confirm.py)). `record_reversal` corrects
**savings only** — a loan payment cannot be reversed, because the interest-first
arithmetic is order-dependent. `reset_cycle` is the only delete: it archives the
current year's money rows first. Cycle months are read from settings
(`cycle_first_month` / `cycle_last_month`), not hardcoded.

## Modules

| Before | After |
|---|---|
| `reloj.py` | `clock.py` |
| `world/consultas.py` | `world/queries.py` |
| `tools/lectura.py` | `tools/reads.py` |
| `tools/escritura.py` | `tools/writes.py` |
| `tools/respuesta.py` | `tools/envelope.py` |
| `scripts/generar_seed.py` | `scripts/generate_seed.py` |

`db.py`, `calc.py`, `gates.py`, `registry.py`, `config.py`, `cli.py` keep their names.

## Currency

**USD, whole dollars only.** The original figures were Colombian pesos; they were divided
by 1,000, which keeps the digits recognizable while producing realistic amounts for a US
savings circle:

| | COP (before) | USD (now) |
|---|---|---|
| monthly ticket | 10,000 | **$10** |
| monthly prize | 60,000 | **$60** |
| quarterly ticket | 20,000 | **$20** |
| quarterly prize | 250,000 | **$250** |
| loans | 150,000–1,000,000 | **$150–$1,000** |
| total saved | 13,170,000 | **$13,170** |

Two things this preserves, both load-bearing:

1. **Whole units.** No cents anywhere. Several HARD rules and all the arithmetic depend on
   money being integers — a real exchange-rate conversion would have introduced fractions
   and broken that.
2. **Exact interest.** Every seeded loan still yields whole-dollar interest at the new
   scale (500×3%=15, 1000×4%=40, 300×3%=9, 400×5%=20, 800×3%=24, 250×2%=5, 600×4%=24), so
   rescaling introduced no new rounding anywhere.

### The formatting trap

Thousands separators are **inverted** between the two locales, and getting it wrong is a
1000× error on a money field:

| Text | Colombia | United States |
|---|---|---|
| `50.000` | fifty thousand | **fifty** |
| `50,000` | fifty | **fifty thousand** |

So the input parser accepts `"50,000"` as 50000 and **rejects** `"50.000"` as fractional,
and the formatter emits `$50,000`. Negative amounts put the sign before the symbol:
`-$12,000`, never `$-12,000`.
