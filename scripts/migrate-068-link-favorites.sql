-- Migration 068: per-user favourite links (Link Utilization page)
--
-- Operators watch a handful of uplinks out of thousands of interfaces.
-- Starring one pins it to the top of /link-utilization regardless of the
-- active sort, and the "Favourites" filter narrows the table to just those.
--
-- Scoped per user rather than appliance-wide: the NOC lead's uplinks are not
-- the campus team's. Absence of a row simply means "not favourited", so
-- existing installs keep today's behaviour.
--
-- The interface is referenced by (device_id, if_index) against devices, NOT
-- against device_interfaces: interface rows are re-written by discovery, and a
-- favourite must survive that. A favourite whose if_index no longer exists just
-- never matches a row; deleting the device cascades it away.
--
-- Idempotent and safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS link_favorites (
    user_id    UUID    NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    device_id  UUID    NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    if_index   INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, device_id, if_index)
);

-- The list endpoint reads every favourite for one user on each page load.
-- The primary key already leads with user_id, so that lookup is covered; this
-- index serves the ordered "my favourites" listing without a sort.
CREATE INDEX IF NOT EXISTS idx_link_favorites_user_created
    ON link_favorites (user_id, created_at DESC);

-- ---------------------------------------------------------------- grants
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON link_favorites TO zenplus;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO zenplus;
  END IF;
END $$;

COMMIT;
