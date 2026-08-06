-- migrate-064: Retire the legacy classification-only template stubs
--
-- Background
-- ----------
-- The original builtin "templates" seeded at install (Apr 2026) were
-- classification shells: match_rules only, oid_groups = '[]'. They exist
-- purely to stamp vendor/model/os_version onto a discovered device. The
-- real vendor monitoring packs arrived in migrate-062 / migrate-063 with
-- populated oid_groups.
--
-- Two defects fall out of the two generations coexisting:
--
--  1. Devices discovered before 062/063 stayed pinned to the empty stubs.
--     The poller only calls AssignProfileIfUnset (postgres.go:308) — it
--     fills a NULL profile_id but never corrects an existing one — so
--     those devices poll no vendor metrics at all. The detail page renders
--     the "Vendor Insights" heading and then sticks on the placeholder
--     "Template attached — collecting vendor metrics…" forever, because
--     /devices/{id}/template-insights returns groups = [].
--
--  2. The Cisco stub carries sys_object_id_prefixes ['1.3.6.1.4.1.9'] —
--     the entire Cisco enterprise tree — while both Cisco packs match on
--     sysDescr only. Classifier priority is "longest sysObjectID prefix
--     wins, sysDescr is a fallback" (profile.go:151-190), so the stub wins
--     step 1 for every Cisco device and the packs are unreachable by
--     auto-classification. Dropping just the prefix is not enough: the
--     poller's sysDescr fallback walks profiles in `ORDER BY name`, and
--     'Cisco IOS Router' sorts before 'Cisco IOS/IOS-XE Switch & Router'
--     (space < '/'), so the stub would still shadow the pack.
--
-- Fix
-- ---
-- Fold each stub's classification coverage into its pack so the pack is a
-- strict superset, repoint the devices, then delete the stub. Idempotent:
-- every statement is guarded on the stub still existing with oid_groups = '[]'.
--
-- Deliberately NOT retired: 'Linux Server', 'Windows Server' and
-- 'MikroTik RouterOS'. No monitoring pack supersedes them yet, so they stay
-- as classification-only profiles.
--
-- NOTE: poller/internal/pinger/engine.go seedSNMPProfiles() re-upserts any
-- JSON in $SNMP_PROFILES_DIR (default /opt/zenplus/data/profiles) at
-- startup. The stale stub files must be removed from that directory too, or
-- a poller restart will recreate the stub rows. Devices repointed below stay
-- correct either way (AssignProfileIfUnset never overwrites a set profile_id),
-- but a resurrected Cisco stub would shadow the pack for *future* discoveries.

BEGIN;

-- ============================ 1. widen the packs ============================
-- Absorb the stubs' sysDescr regexes, extractors and broad OID prefixes so
-- retiring a stub cannot narrow classification coverage. `||` merges at the
-- top level only, so each statement restates the full prefix array.

-- Fortinet: stub covered all of 12356; the pack only 12356.101.1 / 12356.15.
UPDATE device_profiles SET
  match_rules = match_rules || jsonb_build_object(
    'sys_object_id_prefixes', jsonb_build_array(
      '1.3.6.1.4.1.12356.101.1', '1.3.6.1.4.1.12356.15',
      '1.3.6.1.4.1.12356.101', '1.3.6.1.4.1.12356'),
    'sys_descr_regex', 'FortiGate|FortiOS',
    'default_model', 'FortiGate'),
  updated_at = NOW()
WHERE name = 'Fortinet FortiGate' AND jsonb_array_length(oid_groups) > 0;

-- Palo Alto: stub covered all of 25461; the pack only 25461.2.3. The pack
-- also had no os_version extractor.
UPDATE device_profiles SET
  match_rules = match_rules || jsonb_build_object(
    'sys_object_id_prefixes', jsonb_build_array(
      '1.3.6.1.4.1.25461.2.3', '1.3.6.1.4.1.25461'),
    'sys_descr_regex', 'Palo Alto|PAN-OS',
    'extract_os_version', 'PAN-OS\s+(\S+)'),
  updated_at = NOW()
WHERE name = 'Palo Alto PAN-OS Firewall' AND jsonb_array_length(oid_groups) > 0;

-- Juniper: stub covered all of 2636; the pack only the 2636.1.1.1.2 subtree.
UPDATE device_profiles SET
  match_rules = match_rules || jsonb_build_object(
    'sys_object_id_prefixes', jsonb_build_array(
      '1.3.6.1.4.1.2636.1.1.1.2', '1.3.6.1.4.1.2636'),
    'sys_descr_regex', 'Juniper|JUNOS'),
  updated_at = NOW()
WHERE name = 'Juniper JunOS' AND jsonb_array_length(oid_groups) > 0;

-- Cisco: deliberately NO sys_object_id_prefixes. ASA and IOS/IOS-XE share
-- the 1.3.6.1.4.1.9.1.x space with no clean subtree split, so sysDescr is
-- the only reliable discriminator between the two packs — granting either
-- one an OID prefix would make it swallow the other. The regex additionally
-- picks up NX-OS, which the stub used to classify via the OID-9 prefix and
-- would otherwise lose vendor stamping entirely.
UPDATE device_profiles SET
  match_rules = match_rules || jsonb_build_object(
    'sys_descr_regex', '(?i)(IOS Software|IOS-XE Software|IOS \(tm\)|Nexus Operating System|NX-OS)',
    'extract_model', '(C\d{4}|ISR\d{4}|ASR\d{4}|Nexus\d{4}|Catalyst\s?\d{4})'),
  updated_at = NOW()
WHERE name = 'Cisco IOS/IOS-XE Switch & Router' AND jsonb_array_length(oid_groups) > 0;

-- ========================= 2. repoint stub devices ==========================
-- Only ever moves a device off a 0-group stub and onto a >0-group pack, so
-- operator-chosen templates and already-correct devices are untouched.

CREATE TEMP TABLE _stub_migration (stub text, pack text) ON COMMIT DROP;
INSERT INTO _stub_migration (stub, pack) VALUES
  ('FortiGate Firewall', 'Fortinet FortiGate'),
  ('Palo Alto Firewall', 'Palo Alto PAN-OS Firewall'),
  ('Juniper Device',     'Juniper JunOS'),
  ('Cisco IOS Router',   'Cisco IOS/IOS-XE Switch & Router');

CREATE TEMP TABLE _repointed (device_id uuid) ON COMMIT DROP;

WITH mapping AS (
  SELECT s.id AS stub_id, p.id AS pack_id
  FROM _stub_migration m
  JOIN device_profiles s ON s.name = m.stub AND jsonb_array_length(s.oid_groups) = 0
  JOIN device_profiles p ON p.name = m.pack AND jsonb_array_length(p.oid_groups) > 0
), upd AS (
  UPDATE devices d SET profile_id = mapping.pack_id, updated_at = NOW()
  FROM mapping WHERE d.profile_id = mapping.stub_id
  RETURNING d.id
)
INSERT INTO _repointed (device_id) SELECT id FROM upd;

-- ===================== 3. drop values from the old template =================
-- device_template_values is keyed by (device_id, group_key, metric_key,
-- instance) and the poller only purges group_keys it polled this cycle, so
-- rows belonging to the previous template would linger indefinitely. Mirrors
-- what POST /snmp/profiles/unassign does on a manual detach.

DELETE FROM device_template_values
WHERE device_id IN (SELECT device_id FROM _repointed);

-- ========================== 4. delete the stubs =============================
-- Safe now that nothing points at them: devices.profile_id and every other
-- FK to device_profiles is ON DELETE SET NULL, and the coverage each stub
-- provided was folded into its pack in step 1.

DELETE FROM device_profiles s
USING _stub_migration m
WHERE s.name = m.stub
  AND jsonb_array_length(s.oid_groups) = 0
  AND EXISTS (
    SELECT 1 FROM device_profiles p
    WHERE p.name = m.pack AND jsonb_array_length(p.oid_groups) > 0
  );

COMMIT;
