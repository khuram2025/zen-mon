-- Migration 128: every cloned agent installation gets its own pending entry.
--
-- VMs cloned from a template that contained the agent report the same
-- agent_uid. The controller used to park the latest conflicting installation
-- in single registration_conflict_* columns on the original agent and answer
-- 409, so each clone overwrote the previous one and approval needed a manual
-- "register as clone" step. It now creates a separate pending agent per
-- installation (bound by reported uid + pending-secret hash through
-- agent_registration_resolutions) that plain approval completes.
--
-- This converts the one candidate each original agent may still hold into
-- such an entry and clears the legacy columns. Other clones re-register on
-- their next poll and get their own entries. Idempotent.

BEGIN;

CREATE TEMP TABLE clone_candidates ON COMMIT DROP AS
SELECT a.id                                              AS source_id,
       gen_random_uuid()                                 AS new_id,
       left(COALESCE(a.platform, 'agent'), 12) || '-clone-' || gen_random_uuid()::text AS new_uid,
       a.agent_uid                                       AS reported_uid,
       a.pending_conflict_secret_hash                    AS pending_hash,
       COALESCE(a.registration_conflict_hostname, a.hostname) AS hostname,
       a.platform,
       COALESCE(a.registration_conflict_version, a.version) AS version,
       a.registration_conflict_install_id                AS install_id,
       a.policy_id,
       a.site_id,
       a.registration_conflict_ip                        AS ip,
       COALESCE(a.update_ring, 'stable')                 AS ring,
       COALESCE(a.registration_conflict_revision, gen_random_uuid()) AS revision,
       COALESCE(a.registration_conflict_at, NOW())       AS seen_at
  FROM agents a
 WHERE a.pending_conflict_secret_hash IS NOT NULL
   AND NOT EXISTS (
         SELECT 1 FROM agent_registration_resolutions r
          WHERE r.reported_agent_uid = a.agent_uid
            AND r.pending_secret_hash = a.pending_conflict_secret_hash);

INSERT INTO agents
       (id, site_id, agent_uid, hostname, platform, version, install_id,
        policy_id, pending_secret_hash, authorization_source, status,
        current_version, last_ip, update_ring)
SELECT c.new_id, c.site_id, c.new_uid, c.hostname, c.platform, c.version, c.install_id,
       c.policy_id, c.pending_hash, 'pending_clone', 'enrolling',
       c.version, c.ip, c.ring
  FROM clone_candidates c
 WHERE NOT EXISTS (SELECT 1 FROM agents x WHERE x.id = c.new_id);

INSERT INTO agent_registration_resolutions
       (reported_agent_uid, pending_secret_hash, action, source_agent_id,
        assigned_agent_id, assigned_agent_uid, conflict_revision, candidate_ip,
        candidate_hostname, candidate_version, candidate_install_id,
        first_claimed_at, last_seen_at)
SELECT c.reported_uid, c.pending_hash, 'register_clone', c.source_id,
       c.new_id, c.new_uid, c.revision, c.ip,
       c.hostname, c.version, c.install_id,
       c.seen_at, c.seen_at
  FROM clone_candidates c
 WHERE NOT EXISTS (
         SELECT 1 FROM agent_registration_resolutions r
          WHERE r.reported_agent_uid = c.reported_uid
            AND r.pending_secret_hash = c.pending_hash);

UPDATE agents SET
       pending_conflict_secret_hash = NULL,
       registration_conflict_revision = NULL,
       registration_conflict_at = NULL,
       registration_conflict_ip = NULL,
       registration_conflict_attempts = 0,
       registration_conflict_install_id = NULL,
       registration_conflict_hostname = NULL,
       registration_conflict_version = NULL
 WHERE pending_conflict_secret_hash IS NOT NULL;

COMMIT;
