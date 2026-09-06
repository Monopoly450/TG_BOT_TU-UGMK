-- Idempotent cleanup for installations upgraded from the previous schema.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'ai_expires_at') THEN
        UPDATE users SET custom_ai_key = NULL WHERE ai_expires_at IS NOT NULL;
    END IF;
END $$;
ALTER TABLE users
    DROP COLUMN IF EXISTS vpn_enabled,
    DROP COLUMN IF EXISTS vpn_key,
    DROP COLUMN IF EXISTS vpn_expires_at,
    DROP COLUMN IF EXISTS vpn_purchased_at,
    DROP COLUMN IF EXISTS ai_balance,
    DROP COLUMN IF EXISTS ai_expires_at,
    DROP COLUMN IF EXISTS ai_purchased_at;
DROP TABLE IF EXISTS ai_keys;
DELETE FROM settings WHERE key = 'openrouter_management_key';
