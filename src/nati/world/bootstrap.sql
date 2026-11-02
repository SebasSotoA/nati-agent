-- =========================================================================
-- PRODUCTION BOOTSTRAP — empty books, one fund, dated settings.
--
-- No members, savings, loans, tickets, or draws. The administrator (and her
-- personal lender) are created by `nati bootstrap`, not here.
--
-- cycle_year is a placeholder: db.create() overlays it from clock.today().year
-- so a January rebuild does not ship last year's cycle. Raffle prices and
-- cycle months are the confirmed defaults; runtime may insert the same keys
-- again with the clock year as effective_from.
-- =========================================================================

-- Exactly one money source at birth: the common fund. id 0 is reserved.
INSERT INTO lenders (id, name, kind, member_id) VALUES
  (0, 'Natillera Fund', 'fund', NULL);

-- Dated settings. Never overwritten; a later insert with a later (or same)
-- effective_from wins (see queries.setting).
INSERT INTO settings (key, value, effective_from, note) VALUES
  ('cycle_year', '2026', '2026-01-01', 'placeholder; overlay at create from clock'),
  ('cycle_first_month', '1', '2026-01-01', 'January'),
  ('cycle_last_month', '11', '2026-01-01', 'November; December is for the shopping'),
  ('monthly_ticket_price', '10', '2026-01-01', 'Monthly raffle fee per member'),
  ('monthly_prize', '60', '2026-01-01', 'Fixed prize for the monthly raffle'),
  ('quarterly_ticket_price', '20', '2026-01-01', 'Quarterly fee per member'),
  ('quarterly_prize', '250', '2026-01-01', 'Prize for the quarterly raffle');
