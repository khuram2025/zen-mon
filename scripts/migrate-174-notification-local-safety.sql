BEGIN;
ALTER TABLE notification_site_policies ADD COLUMN mode TEXT NOT NULL DEFAULT 'strict_selected'
    CHECK(mode IN ('strict_selected','local_safety'));
COMMIT;
