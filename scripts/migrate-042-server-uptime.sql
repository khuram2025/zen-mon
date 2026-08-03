-- Host uptime / boot time.
--
-- The agent has always reported os.boot_time and os.uptime_seconds in its
-- inventory snapshot, but ingest read only services/filesystems/network/
-- software from that payload and discarded the rest — so uptime, one of the
-- first things an operator looks for on a server, existed nowhere in the
-- product. Stored on the server row (not ClickHouse): it is a slow-moving
-- inventory fact, and deriving current uptime from a boot timestamp needs no
-- time series.

ALTER TABLE servers
    ADD COLUMN IF NOT EXISTS boot_time TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cpu_cores INTEGER,
    ADD COLUMN IF NOT EXISTS memory_total_bytes BIGINT;

COMMENT ON COLUMN servers.boot_time IS
    'Host boot time from the agent inventory snapshot. Uptime is derived as now() - boot_time.';
COMMENT ON COLUMN servers.cpu_cores IS
    'Logical CPU count from the agent inventory snapshot.';
COMMENT ON COLUMN servers.memory_total_bytes IS
    'Total physical memory from the agent inventory snapshot.';
