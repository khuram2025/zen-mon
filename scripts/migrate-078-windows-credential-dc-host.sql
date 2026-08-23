-- Migration 078: remember which host a Windows credential is meant for
--
-- Windows credentials were stored with no target. The only way to verify one
-- worked was the "test against a host" action, which took a free-text IP typed
-- at test time and authenticated to it with the stored domain password. That
-- made the test action a credential-disclosure primitive: point it at a host
-- you control and capture the NTLM exchange for an account you were never
-- given. It also meant nobody could tell, later, which host a credential was
-- ever supposed to reach.
--
-- `dc_host` pins the intended target — the domain controller name or IP the
-- credential belongs to — so the test action has a trusted default and does
-- not need an operator-supplied host at all. 255 chars matches
-- udt_domain_controllers.host, which holds the same kind of value; the API
-- validates it as a bare hostname or IP (no scheme, port, path or embedded
-- credentials) on the way in.
--
-- Nullable: credentials used only by discovery sweeps have no single target,
-- and existing rows must keep working untouched.
--
-- Idempotent and safe to re-run.

BEGIN;

ALTER TABLE windows_credentials
  ADD COLUMN IF NOT EXISTS dc_host VARCHAR(255);

COMMENT ON COLUMN windows_credentials.dc_host IS
  'Domain controller hostname or IP this credential targets; the default host for the connection test. NULL = no fixed target (discovery-only credential).';

COMMIT;
