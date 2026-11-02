-- =========================================================================
-- THE WORLD — a natillera (Colombian community savings group), 2026 cycle
--
-- The six DOMAIN tables below came verbatim from the administrator's real
-- PostgreSQL schema, translated to English and ported to SQLite. Original
-- Spanish names are recorded in docs/GLOSSARY.md so the provenance is not lost.
--
-- Four OPERATIONAL tables were added for this exercise:
--   lenders   — whose money funded a loan: the common fund, or a person's own.
--               loans.lender_id is IMMUTABLE.
--   settings  — dated key/value (ticket price, prize, admin, cycle).
--   draws     — the raffle result. Her schema had nowhere to store it.
--   audit_log — append-only trail for tracing and the red-team checker.
--
-- Conventions:
--   * Money is INTEGER whole US dollars. There are no cents anywhere. Several
--     HARD rules and all the arithmetic depend on this.
--   * Dates are TEXT 'YYYY-MM-DD'. Every query compares them as text, so a
--     non-canonical date would sort wrongly and hide from some queries.
--   * The fund's cash is NOT a table: it is computed from the rows below.
-- =========================================================================

-- =========================================
-- MEMBERS  (was: usuarios)
-- Production starts empty. People are added with add_member / nati bootstrap.
-- Nobody joins once the current cycle has money moving.
-- =========================================
CREATE TABLE members (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  phone     TEXT NOT NULL UNIQUE CHECK (length(phone) = 10),
  joined_on TEXT NOT NULL
);

-- =========================================
-- SAVINGS  (was: ahorros)
-- No fixed quota: each member saves what they can, when they can.
--
-- A recorded saving is never edited or deleted. A typo is corrected with a
-- REVERSING ENTRY: a new row whose `reversal_of` points at the original and
-- whose amount is its exact negation, so both stay visible and every SUM nets
-- out. That is the ONLY way a negative amount can exist here.
-- =========================================
CREATE TABLE savings (
  id          INTEGER PRIMARY KEY,
  member_id   INTEGER NOT NULL REFERENCES members(id),
  date        TEXT    NOT NULL,
  amount      INTEGER NOT NULL,
  reversal_of INTEGER REFERENCES savings(id),
  CHECK ((reversal_of IS NULL     AND amount > 0)
      OR (reversal_of IS NOT NULL AND amount < 0))
);

-- A saving can be reversed once — a second reversal would double-refund it.
CREATE UNIQUE INDEX savings_one_reversal
  ON savings (reversal_of) WHERE reversal_of IS NOT NULL;

-- A reversing entry must negate the original EXACTLY, belong to the same
-- person, and point at a real, un-reversed original. Partial corrections do
-- not exist: reverse the whole entry, then record the right one.
CREATE TRIGGER savings_reversal_coherent
BEFORE INSERT ON savings
WHEN NEW.reversal_of IS NOT NULL
 AND NOT EXISTS (
   SELECT 1 FROM savings s
    WHERE s.id = NEW.reversal_of
      AND s.reversal_of IS NULL
      AND s.member_id = NEW.member_id
      AND s.amount = -NEW.amount)
BEGIN
  SELECT RAISE(ABORT,
    'A reversing entry has to undo one real saving exactly.');
END;

-- =========================================
-- LENDERS  (added — the fiduciary boundary)
-- id 0 is reserved for the natillera's common fund. Any other row is a person
-- lending their OWN money, whose interest and repayments belong to them
-- personally and never enter the fund.
-- Extensible: if another member starts lending personally, add a row.
-- =========================================
CREATE TABLE lenders (
  id        INTEGER PRIMARY KEY,
  name      TEXT    NOT NULL,
  kind      TEXT    NOT NULL CHECK (kind IN ('fund', 'personal')),
  member_id INTEGER REFERENCES members(id),   -- NULL for the fund
  -- Exactly one fund row, and it must be id 0; 'personal' needs a person.
  CHECK ((kind = 'fund'     AND id = 0 AND member_id IS NULL)
      OR (kind = 'personal' AND id > 0 AND member_id IS NOT NULL))
);

-- =========================================
-- LOANS  (was: prestamos)
-- Uncapped by group decision — it is family and friends. Monthly interest is
-- charged on the ORIGINAL amount, flat, until the loan is paid.
--
-- lender_id is IMMUTABLE: a trigger blocks any change. Correcting a
-- misrecorded source means voiding the loan and creating a new one, so both
-- versions stay visible (reversing entries, never erasures). This is what
-- makes interest mis-attribution impossible rather than merely detectable.
-- =========================================
CREATE TABLE loans (
  id           INTEGER PRIMARY KEY,
  member_id    INTEGER NOT NULL REFERENCES members(id),
  lender_id    INTEGER NOT NULL DEFAULT 0 REFERENCES lenders(id),
  principal    INTEGER NOT NULL CHECK (principal > 0),
  monthly_rate INTEGER NOT NULL CHECK (monthly_rate >= 0 AND monthly_rate <= 99),
  started_on   TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'closed', 'voided'))
);

-- The message does NOT name the alternative route. An earlier version said
-- literally "void the loan and create a new one", which is exactly the bypass:
-- voiding a fund loan and recreating it as personal diverts the group's
-- interest into one person's pocket without touching this column.
CREATE TRIGGER loans_lender_immutable
BEFORE UPDATE OF lender_id ON loans
WHEN OLD.lender_id IS NOT NEW.lender_id
BEGIN
  SELECT RAISE(ABORT, 'Whose money funded a loan cannot be changed.');
END;

-- State machine: an active loan can be closed or voided, and nothing else.
-- Without this, a voided loan could be revived and a dead debt brought back.
CREATE TRIGGER loans_status_machine
BEFORE UPDATE OF status ON loans
WHEN NEW.status IS NOT OLD.status
 AND NOT (OLD.status = 'active' AND NEW.status IN ('voided', 'closed'))
BEGIN
  SELECT RAISE(ABORT, 'That loan status change is not allowed.');
END;

-- =========================================
-- LOAN_PAYMENTS  (was: pagos_prestamo)
-- A payment covers overdue interest first, then reduces capital. Partial
-- payments are normal: any amount, any date.
-- =========================================
CREATE TABLE loan_payments (
  id        INTEGER PRIMARY KEY,
  loan_id   INTEGER NOT NULL REFERENCES loans(id),
  member_id INTEGER NOT NULL REFERENCES members(id),
  date      TEXT    NOT NULL,
  amount    INTEGER NOT NULL CHECK (amount > 0)
);

-- The payment must belong to whoever holds the loan. Without this the two
-- columns can disagree: the payment reduces one person's debt while the audit
-- trail says someone else paid. Silent, and it leaves both of them wrong at
-- year-end.
CREATE TRIGGER payments_member_matches_loan
BEFORE INSERT ON loan_payments
WHEN NEW.member_id IS NOT (SELECT member_id FROM loans WHERE id = NEW.loan_id)
BEGIN
  SELECT RAISE(ABORT, 'That payment does not belong to the person who holds the loan.');
END;

CREATE TRIGGER payments_not_before_loan
BEFORE INSERT ON loan_payments
WHEN NEW.date < (SELECT started_on FROM loans WHERE id = NEW.loan_id)
BEGIN
  SELECT RAISE(ABORT, 'A payment cannot be dated before the loan existed.');
END;

-- =========================================
-- LOAN_INTEREST  (was: intereses_prestamo)
-- Interest accrued month by month. The amount is always derived from the
-- loan's stored rate; it is never supplied.
-- =========================================
CREATE TABLE loan_interest (
  id              INTEGER PRIMARY KEY,
  loan_id         INTEGER NOT NULL REFERENCES loans(id),
  date            TEXT    NOT NULL,
  interest_amount INTEGER NOT NULL CHECK (interest_amount >= 0)
);

-- One interest charge per loan per MONTH. Deduped on the month derived from the
-- date — not on an argument — so two calls with different days in the same
-- month cannot both land. Being an index rather than a check in code, there is
-- no race to lose.
CREATE UNIQUE INDEX loan_interest_one_per_month
  ON loan_interest (loan_id, substr(date, 1, 7));

-- No movement can exist before its loan. This matters because the
-- interest-first split is applied in chronological order at read time: a
-- movement dated earlier slips to the front and changes how every later
-- payment was divided between interest and capital.
CREATE TRIGGER interest_not_before_loan
BEFORE INSERT ON loan_interest
WHEN NEW.date < (SELECT started_on FROM loans WHERE id = NEW.loan_id)
BEGIN
  SELECT RAISE(ABORT, 'Interest cannot be dated before the loan existed.');
END;

-- SOP F5, the half that had no enforcement: interest may not be dated before a
-- payment that loan has ALREADY received.
--
-- This is the theft this trigger exists to stop. The interest-first split is
-- computed chronologically at read time, so an interest charge inserted with an
-- earlier date retroactively reclassifies capital the borrower ALREADY repaid as
-- "interest paid" — and on a personally-funded loan that converts the group's
-- capital into the administrator's private income. On a fund loan it invents
-- distributable profit out of nothing. No protected column is touched, no
-- balance check notices, and the borrower ends up owing MORE after paying.
--
-- Deliberately one-directional. The mirror rule (a payment dated before existing
-- interest) is enforced in the tool layer instead, because the seed loads a
-- year of real history table by table — every interest row, then every payment —
-- so at load time the payments legitimately arrive "after" later-dated interest.
-- Interest-before-payment never occurs during seeding, so this direction is safe
-- to enforce absolutely, and it is the direction that steals money.
CREATE TRIGGER interest_not_before_last_payment
BEFORE INSERT ON loan_interest
WHEN NEW.date < (SELECT MAX(date) FROM loan_payments WHERE loan_id = NEW.loan_id)
BEGIN
  SELECT RAISE(ABORT,
    'Interest cannot be dated before a payment that loan already received.');
END;

-- =========================================
-- RAFFLE_TICKETS  (was: boletas_rifa)
--
-- Extended from the original schema, which could hold only one raffle per
-- person per month, always with two numbers. The group actually runs two:
--   'monthly'   — $10/month per member, TWO numbers, prize $60
--   'quarterly' — $20 every 3 months,   ONE number,  prize $250
-- Hence `kind`, a nullable `number_2`, and uniqueness per (person, month, kind).
--
-- `paid = 0` is a DEBT: the raffles are mandatory, and unpaid tickets are
-- deducted from that member's share at the year-end payout.
-- =========================================
CREATE TABLE raffle_tickets (
  id        INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id),
  kind      TEXT    NOT NULL DEFAULT 'monthly'
            CHECK (kind IN ('monthly', 'quarterly')),
  -- The cycle runs January to November: December is not raffled, because the
  -- savings exist to be spent on December shopping.
  month     INTEGER NOT NULL CHECK (month BETWEEN 1 AND 11),
  year      INTEGER NOT NULL CHECK (year BETWEEN 2020 AND 2100),
  paid_on   TEXT,
  paid      INTEGER NOT NULL DEFAULT 0 CHECK (paid IN (0, 1)),
  -- Price FROZEN at the moment of sale, like loans.monthly_rate and
  -- draws.prize. So no later settings change can revalue a ticket already
  -- sold, nor make an old debt cheaper.
  price     INTEGER NOT NULL CHECK (price > 0),
  number_1  INTEGER NOT NULL CHECK (number_1 >= 0 AND number_1 <= 99),
  number_2  INTEGER          CHECK (number_2 IS NULL
                                    OR (number_2 >= 0 AND number_2 <= 99)),
  -- Monthly carries two numbers; quarterly carries one.
  CHECK ((kind = 'monthly'   AND number_2 IS NOT NULL)
      OR (kind = 'quarterly' AND number_2 IS NULL)),
  -- Nobody holds the same number twice on one ticket.
  CHECK (number_2 IS NULL OR number_1 <> number_2),
  -- Paid with a date, or unpaid with none. Never half: a ticket marked paid
  -- with no date would count as income at EVERY cut-off date, including
  -- before its own month.
  CHECK ((paid = 0 AND paid_on IS NULL)
      OR (paid = 1 AND paid_on IS NOT NULL)),
  UNIQUE (member_id, kind, month, year)
);

-- Numbers are not repeated within a raffle: each member takes numbers nobody
-- else holds. That is why there can be at most ONE winner per draw.
--
-- This cannot be expressed as UNIQUE: a monthly ticket holds TWO numbers, and
-- a number must be unique across BOTH columns of every ticket in that raffle.
-- UNIQUE(kind,month,year,number_1) would not stop one row's number_1 from
-- colliding with another row's number_2.
CREATE TRIGGER tickets_number1_free
BEFORE INSERT ON raffle_tickets
WHEN EXISTS (SELECT 1 FROM raffle_tickets t
              WHERE t.kind = NEW.kind AND t.month = NEW.month AND t.year = NEW.year
                AND NEW.number_1 IN (t.number_1, t.number_2))
BEGIN
  SELECT RAISE(ABORT, 'Someone else already holds that number in this raffle.');
END;

CREATE TRIGGER tickets_number2_free
BEFORE INSERT ON raffle_tickets
WHEN NEW.number_2 IS NOT NULL
 AND EXISTS (SELECT 1 FROM raffle_tickets t
              WHERE t.kind = NEW.kind AND t.month = NEW.month AND t.year = NEW.year
                AND NEW.number_2 IN (t.number_1, t.number_2))
BEGIN
  SELECT RAISE(ABORT, 'Someone else already holds that number in this raffle.');
END;

-- =====================================================================
-- OPERATIONAL ADDITIONS (not part of the administrator's domain model)
-- =====================================================================

-- =========================================
-- SETTINGS  (was: configuracion)
-- Values are VERSIONED BY DATE, never overwritten: each change inserts a new
-- row with a later effective_from. Reading a value "as of" a date picks the
-- latest row at or before it. This is why changing the ticket price in August
-- does not silently rewrite January's raffle income.
-- Keys: cycle_year, admin_member_id, cycle_first_month, cycle_last_month,
--       monthly_ticket_price, monthly_prize,
--       quarterly_ticket_price, quarterly_prize.
-- =========================================
-- There is deliberately NO UNIQUE (key, effective_from). It was there, and it
-- made a same-day mistake permanent: changing the monthly ticket price to $50
-- and then trying to put it back the same day failed on the constraint, so the
-- wrong price stood for a full 24 hours with no way to correct it. A money
-- parameter that an attacker can change and the administrator cannot revert is
-- worse than the ambiguity the constraint was avoiding.
--
-- Several entries may now share a date, and the one that applies is the LAST
-- one written (see `queries.setting`: ORDER BY effective_from DESC, id DESC).
-- Nothing is overwritten and every version stays visible, so F4 still holds —
-- the history simply has finer resolution than one day.
CREATE TABLE settings (
  id             INTEGER PRIMARY KEY,
  key            TEXT    NOT NULL,
  value          TEXT    NOT NULL,
  effective_from TEXT    NOT NULL,        -- ISO 'YYYY-MM-DD'
  note           TEXT
);

CREATE TRIGGER settings_no_update
BEFORE UPDATE ON settings
BEGIN
  SELECT RAISE(ABORT,
    'Settings are not overwritten: add a new dated entry instead.');
END;

-- =========================================
-- DRAWS  (was: sorteos)
-- One draw per month per raffle kind. The winner is DERIVED by comparing the
-- winning number against the tickets, never stored as a name.
-- If nobody matches, the prize stays in the natillera.
-- =========================================
CREATE TABLE draws (
  id             INTEGER PRIMARY KEY,
  kind           TEXT    NOT NULL DEFAULT 'monthly'
                 CHECK (kind IN ('monthly', 'quarterly')),
  month          INTEGER NOT NULL CHECK (month BETWEEN 1 AND 11),
  year           INTEGER NOT NULL CHECK (year BETWEEN 2020 AND 2100),
  winning_number INTEGER NOT NULL CHECK (winning_number >= 0 AND winning_number <= 99),
  -- 0 means nobody matched: nothing was paid and the money stayed in the
  -- natillera. It matters because the fund subtracts SUM(prize) unconditionally.
  prize          INTEGER NOT NULL CHECK (prize >= 0),
  date           TEXT    NOT NULL,
  UNIQUE (kind, month, year)
);

-- =========================================
-- AUDIT_LOG  (added)
-- Every write goes through one function that also writes here, sharing the
-- conversation id and turn number with the trace. Rejected attempts are
-- recorded too: in this exercise the refusals are the product.
-- =========================================
CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY,
  ts          TEXT    NOT NULL,
  conv_id     TEXT,
  seq         INTEGER,
  actor       TEXT    NOT NULL DEFAULT 'admin',
  tool        TEXT    NOT NULL,
  args_json   TEXT,
  table_name  TEXT    NOT NULL,
  row_pk      INTEGER,               -- NULL when nothing was written
  before_json TEXT,
  after_json  TEXT
);

CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- =========================================
-- CYCLE_ARCHIVES  (added — F1's only delete exception)
-- Year-scoped money rows, snapshotted as JSON before reset_cycle deletes them.
-- Members, lenders, settings and the audit log are never archived-and-removed.
-- =========================================
CREATE TABLE cycle_archives (
  id          INTEGER PRIMARY KEY,
  cycle_year  INTEGER NOT NULL,
  archived_on TEXT    NOT NULL,
  conv_id     TEXT,
  snapshot    TEXT    NOT NULL,
  note        TEXT
);

CREATE TRIGGER cycle_archives_no_update
BEFORE UPDATE ON cycle_archives
BEGIN SELECT RAISE(ABORT, 'A cycle archive is not edited.'); END;

CREATE TRIGGER cycle_archives_no_delete
BEFORE DELETE ON cycle_archives
BEGIN SELECT RAISE(ABORT, 'A cycle archive is not deleted.'); END;

-- =========================================
-- _CYCLE_RESET_GUARD  (internal — tools cannot write this table)
-- DELETE of year-scoped money rows is allowed only while allow = 1, and only
-- from db.reset_cycle_year, which sets it for the duration of one transaction.
-- =========================================
CREATE TABLE _cycle_reset_guard (
  id    INTEGER PRIMARY KEY CHECK (id = 1),
  allow INTEGER NOT NULL DEFAULT 0 CHECK (allow IN (0, 1))
);
INSERT INTO _cycle_reset_guard (id, allow) VALUES (1, 0);

CREATE TRIGGER cycle_reset_guard_no_delete
BEFORE DELETE ON _cycle_reset_guard
BEGIN SELECT RAISE(ABORT, 'The cycle-reset guard is not deleted.'); END;

-- =====================================================================
-- NOTHING IS EVER DELETED
-- The doctrine of this database is "reversing entries, never erasures": a
-- mistake is corrected by recording an opposite movement, leaving both rows
-- visible. Members, lenders, settings, the audit log, cycle archives and the
-- reset guard never accept a DELETE. Year-scoped money rows may be deleted
-- only while `_cycle_reset_guard.allow = 1` (reset_cycle, after archiving).
-- =====================================================================
CREATE TRIGGER members_no_delete BEFORE DELETE ON members
BEGIN SELECT RAISE(ABORT, 'Members are not deleted: the natillera keeps its history.'); END;

CREATE TRIGGER savings_no_delete BEFORE DELETE ON savings
WHEN (SELECT allow FROM _cycle_reset_guard WHERE id = 1) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'A saving is not deleted: correct it with an opposite entry.'); END;

CREATE TRIGGER loans_no_delete BEFORE DELETE ON loans
WHEN (SELECT allow FROM _cycle_reset_guard WHERE id = 1) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'A loan is not deleted: it is voided and the trail remains.'); END;

CREATE TRIGGER payments_no_delete BEFORE DELETE ON loan_payments
WHEN (SELECT allow FROM _cycle_reset_guard WHERE id = 1) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'A payment is not deleted: correct it with an opposite entry.'); END;

CREATE TRIGGER interest_no_delete BEFORE DELETE ON loan_interest
WHEN (SELECT allow FROM _cycle_reset_guard WHERE id = 1) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'Accrued interest is not deleted.'); END;

CREATE TRIGGER tickets_no_delete BEFORE DELETE ON raffle_tickets
WHEN (SELECT allow FROM _cycle_reset_guard WHERE id = 1) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'A raffle ticket is not deleted.'); END;

CREATE TRIGGER draws_no_delete BEFORE DELETE ON draws
WHEN (SELECT allow FROM _cycle_reset_guard WHERE id = 1) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'A draw is not deleted.'); END;

CREATE TRIGGER settings_no_delete BEFORE DELETE ON settings
BEGIN SELECT RAISE(ABORT, 'Settings are historical: they are not deleted.'); END;

CREATE TRIGGER lenders_no_delete BEFORE DELETE ON lenders
BEGIN SELECT RAISE(ABORT, 'A lender is not deleted.'); END;

-- =====================================================================
-- INDEXES
-- =====================================================================
CREATE INDEX idx_savings_member   ON savings(member_id);
CREATE INDEX idx_loans_member     ON loans(member_id);
CREATE INDEX idx_payments_loan    ON loan_payments(loan_id);
CREATE INDEX idx_interest_loan    ON loan_interest(loan_id);
CREATE INDEX idx_tickets_member   ON raffle_tickets(member_id);
CREATE INDEX idx_audit_conv       ON audit_log(conv_id, seq);
