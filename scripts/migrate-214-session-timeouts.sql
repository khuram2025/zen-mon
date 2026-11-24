BEGIN;
CREATE TABLE IF NOT EXISTS session_timeout_default (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    minutes integer NOT NULL CHECK (minutes BETWEEN -1 AND 1440)
);
INSERT INTO session_timeout_default (minutes) VALUES (-1) ON CONFLICT DO NOTHING;
GRANT SELECT, UPDATE ON session_timeout_default TO zenplus;
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_timeout_minutes integer
    CHECK (session_timeout_minutes BETWEEN -1 AND 1440);
ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS idle_minutes integer
    CHECK (idle_minutes BETWEEN 1 AND 1440);
ALTER TABLE auth_sessions ALTER COLUMN expires_at DROP NOT NULL;
COMMIT;
