-- Agent packages are published in two flavours per platform and version:
-- core (server monitoring only, zenplus-agent-core-<version>.*) and full
-- (server monitoring and APM, zenplus-agent-<version>.*). Every package
-- registered before this migration is a full package.
ALTER TABLE agent_packages ADD COLUMN IF NOT EXISTS flavor varchar(16) NOT NULL DEFAULT 'full';

DO $$
DECLARE
    old_key record;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'agent_packages'::regclass
          AND conname = 'agent_packages_flavor_check'
    ) THEN
        ALTER TABLE agent_packages
            ADD CONSTRAINT agent_packages_flavor_check CHECK (flavor IN ('core', 'full'));
    END IF;

    -- The pre-flavour key (platform, arch, version, channel) would make the
    -- core and full packages of one version collide. Replace it.
    FOR old_key IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'agent_packages'::regclass
          AND contype = 'u'
          AND conname <> 'agent_packages_platform_arch_version_channel_flavor_key'
    LOOP
        EXECUTE format('ALTER TABLE agent_packages DROP CONSTRAINT %I', old_key.conname);
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'agent_packages'::regclass
          AND conname = 'agent_packages_platform_arch_version_channel_flavor_key'
    ) THEN
        ALTER TABLE agent_packages
            ADD CONSTRAINT agent_packages_platform_arch_version_channel_flavor_key
            UNIQUE (platform, arch, version, channel, flavor);
    END IF;
END $$;

DROP INDEX IF EXISTS idx_agent_pkg_latest;
CREATE INDEX IF NOT EXISTS idx_agent_pkg_latest
    ON agent_packages (platform, channel, flavor, is_latest);
