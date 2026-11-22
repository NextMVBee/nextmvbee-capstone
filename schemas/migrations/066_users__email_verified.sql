-- migration 066 (UI-2, the owner's decision of 27 October 2026, 21:30; UI2-2 amended): users gains email_verified. The owner role needs
-- a verified email: an AuthKit identity whose email is not verified gets an ordinary row with email_verified 0 and allowance 0 and is
-- never the owner, whatever its email. A local row (workos_user_id NULL: an email and password account, or an invitation's row, which
-- only a verified email links) counts as verified and keeps the column's default, 1. A row linked through AuthKit before this migration
-- (workos_user_id NOT NULL) carries no proof: the earlier callback's new-row path gave an unverified identity a linked row and recorded
-- nothing, so every linked row starts at 0 until a verified callback of its identity records the flag (app/web/pages.py account_for).
-- Corrected in place for B-OWNER-UPGRADE of the Codex re-audit (28 October 2026): no real store has this migration applied (none is
-- deployed; the p6 store is pre-064), so no new migration file is needed. Applied by migrate() when users lacks email_verified.
BEGIN;
ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1;
UPDATE users SET email_verified = 0 WHERE workos_user_id IS NOT NULL;
COMMIT;
