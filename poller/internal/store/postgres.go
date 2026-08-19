package store

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/checker/snmp"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
)

// PostgresStore handles device data in PostgreSQL.
type PostgresStore struct {
	pool *pgxpool.Pool
}

// NewPostgresStore connects to PostgreSQL.
func NewPostgresStore(ctx context.Context, cfg *config.Config) (*PostgresStore, error) {
	poolCfg, err := pgxpool.ParseConfig(cfg.Postgres.DSN())
	if err != nil {
		return nil, fmt.Errorf("parse postgres config: %w", err)
	}
	poolCfg.MaxConns = 10

	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		return nil, fmt.Errorf("connect to postgres: %w", err)
	}

	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping postgres: %w", err)
	}

	return &PostgresStore{pool: pool}, nil
}

// Close closes the connection pool.
func (s *PostgresStore) Close() {
	s.pool.Close()
}

// LoadDevices returns all ping-enabled devices.
func (s *PostgresStore) LoadDevices(ctx context.Context) ([]*pinger.Device, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, hostname, host(ip_address)::text, ping_interval, ping_enabled, status,
		       COALESCE(last_seen, '1970-01-01'::timestamptz),
		       COALESCE(last_rtt_ms, 0)
		FROM devices
		WHERE ping_enabled = TRUE
		  AND ip_address IS NOT NULL
		ORDER BY hostname
	`)
	if err != nil {
		return nil, fmt.Errorf("query devices: %w", err)
	}
	defer rows.Close()

	var devices []*pinger.Device
	for rows.Next() {
		var d pinger.Device
		var ipStr string
		var intervalSec int

		err := rows.Scan(
			&d.ID, &d.Hostname, &ipStr, &intervalSec,
			&d.PingEnabled, &d.Status, &d.LastSeen, &d.LastRTT,
		)
		if err != nil {
			return nil, fmt.Errorf("scan device: %w", err)
		}

		d.IPAddress = net.ParseIP(ipStr)
		d.PingInterval = time.Duration(intervalSec) * time.Second
		devices = append(devices, &d)
	}

	return devices, rows.Err()
}

// UpdateDeviceStatus updates the device's current status in PostgreSQL.
func (s *PostgresStore) UpdateDeviceStatus(ctx context.Context, deviceID uuid.UUID, status string, lastSeen time.Time, rttMs float64) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE devices
		SET status = $1, last_seen = $2, last_rtt_ms = $3
		WHERE id = $4
	`, status, lastSeen, rttMs, deviceID)
	return err
}

// LoadServiceChecks returns all enabled service checks.
func (s *PostgresStore) LoadServiceChecks(ctx context.Context) ([]*checker.ServiceCheck, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, device_id, name, check_type, enabled,
		       target_host, COALESCE(target_port, 0), COALESCE(target_url, ''),
		       COALESCE(http_method, 'GET'), COALESCE(http_headers::text, '{}'),
		       COALESCE(http_body, ''), COALESCE(http_expected_status, 200),
		       COALESCE(http_expected_statuses, ''),
		       COALESCE(http_content_match, ''), COALESCE(http_follow_redirects, true),
		       COALESCE(tls_warn_days, 30), COALESCE(tls_critical_days, 7),
		       check_interval, timeout, status,
		       COALESCE(level, 1),
		       COALESCE(config::text, '{}'),
		       COALESCE(tags, ARRAY[]::text[]),
		       group_id, parent_check_id,
		       COALESCE(retry_count, 1), COALESCE(retry_delay_s, 30)
		FROM service_checks
		WHERE enabled = TRUE
		ORDER BY name
	`)
	if err != nil {
		return nil, fmt.Errorf("query service checks: %w", err)
	}
	defer rows.Close()

	var checks []*checker.ServiceCheck
	for rows.Next() {
		var sc checker.ServiceCheck
		var deviceID, groupID, parentID *uuid.UUID
		var intervalSec, timeoutSec int
		var retryCount, retryDelaySec int
		var headersJSON string
		var configJSON string
		var tags []string

		err := rows.Scan(
			&sc.ID, &deviceID, &sc.Name, &sc.CheckType, &sc.Enabled,
			&sc.TargetHost, &sc.TargetPort, &sc.TargetURL,
			&sc.HTTPMethod, &headersJSON,
			&sc.HTTPBody, &sc.HTTPExpectedStatus,
			&sc.HTTPExpectedStatuses,
			&sc.HTTPContentMatch, &sc.HTTPFollowRedirects,
			&sc.TLSWarnDays, &sc.TLSCriticalDays,
			&intervalSec, &timeoutSec, &sc.Status,
			&sc.Level, &configJSON, &tags,
			&groupID, &parentID, &retryCount, &retryDelaySec,
		)
		if err != nil {
			return nil, fmt.Errorf("scan service check: %w", err)
		}

		sc.DeviceID = deviceID
		sc.GroupID = groupID
		sc.ParentCheckID = parentID
		sc.CheckInterval = time.Duration(intervalSec) * time.Second
		sc.Timeout = time.Duration(timeoutSec) * time.Second
		sc.RetryCount = retryCount
		sc.RetryDelay = time.Duration(retryDelaySec) * time.Second
		sc.Tags = tags

		// Parse headers JSON
		sc.HTTPHeaders = make(map[string]string)
		if headersJSON != "" && headersJSON != "{}" {
			json.Unmarshal([]byte(headersJSON), &sc.HTTPHeaders)
		}

		// Parse config JSON (type-specific fields)
		sc.Config = make(map[string]any)
		if configJSON != "" && configJSON != "{}" {
			json.Unmarshal([]byte(configJSON), &sc.Config)
		}

		checks = append(checks, &sc)
	}

	return checks, rows.Err()
}

// --- SNMP ---

// LoadSNMPDevices returns all SNMP-enabled devices with credentials
// already decrypted in memory. The SNMP_ENC_KEY env var must be set;
// devices with credentials that fail to decrypt are skipped and logged
// by the caller (we still return a nil error so one bad row does not
// stall the whole sync).
func (s *PostgresStore) LoadSNMPDevices(ctx context.Context) ([]*snmp.Device, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT d.id, d.hostname, COALESCE(d.device_type, 'other'), host(d.ip_address)::text,
		       d.snmp_version, d.snmp_port, COALESCE(d.snmp_community, ''),
		       COALESCE(d.snmp_v3_username, ''), COALESCE(d.snmp_v3_context, ''),
		       COALESCE(d.snmp_auth_protocol, ''), d.snmp_auth_passphrase,
		       COALESCE(d.snmp_priv_protocol, ''), d.snmp_priv_passphrase,
		       COALESCE(d.snmp_timeout_ms, 2000),
		       COALESCE(d.snmp_retries, 2),
		       COALESCE(d.snmp_max_repetitions, 25),
		       COALESCE(d.snmp_poll_interval, 60),
		       d.profile_id,
		       COALESCE(d.sys_object_id, ''),
		       COALESCE(d.vendor, ''),
		       COALESCE(d.model, ''),
		       COALESCE(d.os_version, ''),
		       COALESCE(p.oid_groups::text, ''),
		       COALESCE(us.enabled, TRUE),
		       COALESCE(us.poll_interval_s, 0),
		       sc.id IS NOT NULL,
		       COALESCE(sc.snmp_version, ''), COALESCE(sc.port, 0),
		       COALESCE(sc.community, ''),
		       COALESCE(sc.v3_username, ''), COALESCE(sc.v3_context, ''),
		       COALESCE(sc.v3_auth_protocol, ''), COALESCE(sc.v3_auth_passphrase, ''),
		       COALESCE(sc.v3_priv_protocol, ''), COALESCE(sc.v3_priv_passphrase, ''),
		       COALESCE(sc.timeout_ms, 0), COALESCE(sc.retries, 0)
		FROM devices d
		LEFT JOIN device_profiles p ON p.id = d.profile_id
		LEFT JOIN udt_device_settings us ON us.device_id = d.id
		LEFT JOIN snmp_credentials sc ON sc.id = us.snmp_credential_id
		WHERE d.snmp_enabled = TRUE
		  AND d.ip_address IS NOT NULL
		ORDER BY d.hostname
	`)
	if err != nil {
		return nil, fmt.Errorf("query snmp devices: %w", err)
	}
	defer rows.Close()

	var devices []*snmp.Device
	for rows.Next() {
		var d snmp.Device
		var ipStr string
		var authBlob, privBlob []byte
		var intervalSec int
		var profileID *uuid.UUID
		var oidGroupsJSON string
		var udtIntervalSec int
		var hasUdtCred bool
		var uc snmp.UdtCredential
		err := rows.Scan(
			&d.ID, &d.Hostname, &d.DeviceType, &ipStr,
			&d.Version, &d.Port, &d.Community,
			&d.V3Username, &d.V3Context,
			&d.AuthProtocol, &authBlob,
			&d.PrivProtocol, &privBlob,
			&d.TimeoutMs, &d.Retries, &d.MaxRepetitions, &intervalSec,
			&profileID, &d.SysObjectID, &d.Vendor, &d.Model, &d.OSVersion,
			&oidGroupsJSON,
			&d.UdtEnabled, &udtIntervalSec, &hasUdtCred,
			&uc.Version, &uc.Port, &uc.Community,
			&uc.V3Username, &uc.V3Context,
			&uc.AuthProtocol, &uc.AuthPassphrase,
			&uc.PrivProtocol, &uc.PrivPassphrase,
			&uc.TimeoutMs, &uc.Retries,
		)
		if err != nil {
			return nil, fmt.Errorf("scan snmp device: %w", err)
		}
		d.IPAddress = net.ParseIP(ipStr)
		d.Enabled = true
		d.PollInterval = time.Duration(intervalSec) * time.Second
		d.UdtInterval = time.Duration(udtIntervalSec) * time.Second
		if hasUdtCred {
			d.UdtCredential = &uc
		}
		d.ProfileID = profileID
		if oidGroupsJSON != "" && oidGroupsJSON != "[]" {
			// Parse errors are non-fatal: a bad template entry must not
			// take standard monitoring down with it.
			d.OidGroups, _ = snmp.ParseOidGroups(json.RawMessage(oidGroupsJSON))
		}
		if snmp.IsNetworkDeviceType(d.DeviceType) {
			d.OidGroups = snmp.MergeStandardNetworkOidGroups(d.OidGroups)
		}

		if len(authBlob) > 0 {
			pt, err := snmp.Decrypt(authBlob)
			if err != nil {
				// Skip this device; caller logs via counter.
				continue
			}
			d.AuthPassphrase = pt
		}
		if len(privBlob) > 0 {
			pt, err := snmp.Decrypt(privBlob)
			if err != nil {
				continue
			}
			d.PrivPassphrase = pt
		}

		devices = append(devices, &d)
	}
	return devices, rows.Err()
}

// LoadUdtGlobalInterval returns the operator-configured global UDT
// poll interval in seconds from system_settings (key 'udt'), or 0
// when unset — the engine then falls back to UDT_POLL_INTERVAL / 5m.
func (s *PostgresStore) LoadUdtGlobalInterval(ctx context.Context) (int, error) {
	var n int
	err := s.pool.QueryRow(ctx, `
		SELECT COALESCE((value->>'poll_interval_s')::int, 0)
		FROM system_settings WHERE key = 'udt'
	`).Scan(&n)
	if err == pgx.ErrNoRows {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	if n < 30 {
		return 0, nil
	}
	return n, nil
}

// UpsertSystemInfo writes discovered system-group fields back to
// devices (vendor/model/os_version/sys_object_id). The hostname is
// only overwritten when the device has opted in via
// auto_rename_from_snmp — otherwise the operator's chosen name wins.
// This avoids the surprise where adding SNMP to a device silently
// renames it to whatever its agent reports as sysName.
func (s *PostgresStore) UpsertSystemInfo(
	ctx context.Context, deviceID uuid.UUID,
	sysObjectID, vendor, model, osVersion, sysName string,
) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE devices
		SET sys_object_id = COALESCE(NULLIF($1,''), sys_object_id),
		    vendor        = COALESCE(NULLIF($2,''), vendor),
		    model         = COALESCE(NULLIF($3,''), model),
		    os_version    = COALESCE(NULLIF($4,''), os_version),
		    hostname      = CASE
		                      WHEN auto_rename_from_snmp
		                      THEN COALESCE(NULLIF($6,''), hostname)
		                      ELSE hostname
		                    END
		WHERE id = $5
	`, sysObjectID, vendor, model, osVersion, deviceID, sysName)
	return err
}

// LookupDeviceByIP returns the device ID for a given source IP. Used
// by the SNMP trap listener to enrich incoming traps. Returns
// (uuid.Nil, false) when no device matches.
func (s *PostgresStore) LookupDeviceByIP(ctx context.Context, ip net.IP) (uuid.UUID, bool) {
	if ip == nil {
		return uuid.Nil, false
	}
	var id uuid.UUID
	err := s.pool.QueryRow(ctx,
		`SELECT id FROM devices WHERE ip_address = $1::inet LIMIT 1`,
		ip.String(),
	).Scan(&id)
	if err != nil {
		return uuid.Nil, false
	}
	return id, true
}

// AssignProfileIfUnset sets devices.profile_id for a device, but only
// if it is currently NULL. This honors explicit operator overrides —
// once an admin picks a profile in the UI, auto-classification will
// not overwrite it.
func (s *PostgresStore) AssignProfileIfUnset(ctx context.Context, deviceID, profileID uuid.UUID) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE devices SET profile_id = $1
		WHERE id = $2 AND profile_id IS NULL
	`, profileID, deviceID)
	return err
}

// UpsertProfile inserts a bootstrap Profile into device_profiles by
// (name, version). Existing rows are authoritative: migrations and operator
// edits may have added monitoring groups after an old JSON bootstrap file was
// installed, so startup must never replace their contents.
//
// The no-op conflict update is intentional. Unlike DO NOTHING it reliably
// returns the existing row ID even when another process inserted it
// concurrently, without changing any profile fields.
func (s *PostgresStore) UpsertProfile(ctx context.Context, p *snmp.Profile) error {
	matchJSON, err := json.Marshal(p.Match)
	if err != nil {
		return fmt.Errorf("marshal match_rules: %w", err)
	}
	oidGroups := p.OidGroups
	if len(oidGroups) == 0 {
		oidGroups = json.RawMessage("[]")
	}
	var id uuid.UUID
	err = s.pool.QueryRow(ctx, `
		INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7)
		ON CONFLICT (name, version) DO UPDATE SET
		    name = device_profiles.name
		RETURNING id
	`, p.Name, p.Vendor, string(matchJSON), string(oidGroups), p.Version, p.Builtin, p.Description).Scan(&id)
	if err != nil {
		return fmt.Errorf("upsert profile %s: %w", p.Name, err)
	}
	p.ID = id
	return nil
}

// LoadProfiles returns every row of device_profiles. The DB is the
// source of truth for monitoring templates (builtins are seeded by SQL
// migration; operators create custom ones through the API), so the
// classifier is refreshed from here on the periodic sync tick.
func (s *PostgresStore) LoadProfiles(ctx context.Context) ([]*snmp.Profile, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, name, COALESCE(vendor, ''), match_rules, oid_groups,
		       version, builtin, COALESCE(description, '')
		FROM device_profiles
		ORDER BY name
	`)
	if err != nil {
		return nil, fmt.Errorf("query device_profiles: %w", err)
	}
	defer rows.Close()

	var profiles []*snmp.Profile
	for rows.Next() {
		var p snmp.Profile
		var matchJSON, groupsJSON []byte
		if err := rows.Scan(&p.ID, &p.Name, &p.Vendor, &matchJSON, &groupsJSON,
			&p.Version, &p.Builtin, &p.Description); err != nil {
			return nil, fmt.Errorf("scan device_profile: %w", err)
		}
		if len(matchJSON) > 0 {
			if err := json.Unmarshal(matchJSON, &p.Match); err != nil {
				continue // malformed match rules: skip, don't stall the sync
			}
		}
		p.OidGroups = json.RawMessage(groupsJSON)
		profiles = append(profiles, &p)
	}
	return profiles, rows.Err()
}

// UpsertTemplateValues replaces a device's latest template-metric
// snapshot. Rows are upserted, then rows belonging to a group that was
// polled this cycle but no longer contains the instance are purged (a
// deleted VPN tunnel or a disconnected AP disappears from the UI rather
// than lingering with stale numbers). Groups that were NOT polled this
// cycle (agent timeout mid-poll) are left untouched.
func (s *PostgresStore) UpsertTemplateValues(
	ctx context.Context, deviceID uuid.UUID, vals []snmp.TemplateValue, polledGroups []string,
) error {
	if len(vals) == 0 && len(polledGroups) == 0 {
		return nil
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin template values tx: %w", err)
	}
	defer tx.Rollback(ctx)

	// The purge below removes rows older than this cycle; write the same
	// timestamp we compare against so freshly-upserted rows can never be
	// swept by their own cycle.
	cycleStart := time.Now().UTC()
	batch := &pgx.Batch{}
	for i := range vals {
		v := &vals[i]
		batch.Queue(`
			INSERT INTO device_template_values
			    (device_id, group_key, metric_key, instance, series_key,
			     label, unit, value_num, value_text, updated_at)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
			ON CONFLICT (device_id, group_key, metric_key, instance) DO UPDATE SET
			    series_key = EXCLUDED.series_key,
			    label      = EXCLUDED.label,
			    unit       = EXCLUDED.unit,
			    value_num  = EXCLUDED.value_num,
			    value_text = EXCLUDED.value_text,
			    updated_at = EXCLUDED.updated_at
		`, deviceID, v.GroupKey, v.MetricKey, v.Instance, v.SeriesKey,
			v.Label, v.Unit, v.ValueNum, v.ValueText, cycleStart)
	}
	if batch.Len() > 0 {
		br := tx.SendBatch(ctx, batch)
		for i := 0; i < batch.Len(); i++ {
			if _, err := br.Exec(); err != nil {
				br.Close()
				return fmt.Errorf("upsert template value: %w", err)
			}
		}
		if err := br.Close(); err != nil {
			return fmt.Errorf("close template batch: %w", err)
		}
	}
	if len(polledGroups) > 0 {
		if _, err := tx.Exec(ctx, `
			DELETE FROM device_template_values
			WHERE device_id = $1 AND group_key = ANY($2) AND updated_at < $3
		`, deviceID, polledGroups, cycleStart); err != nil {
			return fmt.Errorf("purge stale template values: %w", err)
		}
	}
	return tx.Commit(ctx)
}

// UpsertInterfaces upserts the discovered interface list for a device.
// Interfaces not seen in the current poll are not deleted — operators
// may want historical per-interface data. A separate cleanup job can
// prune rows with last_seen older than N days.
func (s *PostgresStore) UpsertInterfaces(ctx context.Context, deviceID uuid.UUID, ifs []snmp.Interface) error {
	if len(ifs) == 0 {
		return nil
	}
	batch := &pgx.Batch{}
	for _, i := range ifs {
		var macArg any
		if i.MACAddress != "" {
			macArg = i.MACAddress
		}
		batch.Queue(`
			INSERT INTO device_interfaces (
			    device_id, if_index, if_name, if_descr, if_alias,
			    if_type, if_speed, mac_address, admin_status, oper_status,
			    first_seen, last_seen
			) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW(),NOW())
			ON CONFLICT (device_id, if_index) DO UPDATE SET
			    if_name = EXCLUDED.if_name,
			    if_descr = EXCLUDED.if_descr,
			    if_alias = EXCLUDED.if_alias,
			    if_type = EXCLUDED.if_type,
			    if_speed = EXCLUDED.if_speed,
			    mac_address = EXCLUDED.mac_address,
			    admin_status = EXCLUDED.admin_status,
			    oper_status = EXCLUDED.oper_status,
			    last_seen = NOW()
		`,
			deviceID, i.IfIndex, i.IfName, i.IfDescr, i.IfAlias,
			i.IfType, int64(i.IfSpeed), macArg, i.AdminStatus, i.OperStatus,
		)
	}
	br := s.pool.SendBatch(ctx, batch)
	defer br.Close()
	for i := 0; i < len(ifs); i++ {
		if _, err := br.Exec(); err != nil {
			return fmt.Errorf("upsert interface %d: %w", ifs[i].IfIndex, err)
		}
	}
	return nil
}

// UpsertEntities upserts ENTITY-MIB inventory.
func (s *PostgresStore) UpsertEntities(ctx context.Context, deviceID uuid.UUID, ents []snmp.Entity) error {
	if len(ents) == 0 {
		return nil
	}
	batch := &pgx.Batch{}
	for _, e := range ents {
		batch.Queue(`
			INSERT INTO device_entities (
			    device_id, ent_index, parent_index, class, name,
			    serial_number, model_name, hw_revision, fw_revision,
			    first_seen, last_seen
			) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),NOW())
			ON CONFLICT (device_id, ent_index) DO UPDATE SET
			    parent_index = EXCLUDED.parent_index,
			    class = EXCLUDED.class,
			    name = EXCLUDED.name,
			    serial_number = EXCLUDED.serial_number,
			    model_name = EXCLUDED.model_name,
			    hw_revision = EXCLUDED.hw_revision,
			    fw_revision = EXCLUDED.fw_revision,
			    last_seen = NOW()
		`,
			deviceID, e.EntIndex, e.ParentIndex, e.Class, e.Name,
			e.SerialNumber, e.ModelName, e.HWRevision, e.FWRevision,
		)
	}
	br := s.pool.SendBatch(ctx, batch)
	defer br.Close()
	for i := 0; i < len(ents); i++ {
		if _, err := br.Exec(); err != nil {
			return fmt.Errorf("upsert entity %d: %w", ents[i].EntIndex, err)
		}
	}
	return nil
}

// UpsertSensors upserts ENTITY-SENSOR-MIB rows.
func (s *PostgresStore) UpsertSensors(ctx context.Context, deviceID uuid.UUID, sensors []snmp.Sensor) error {
	if len(sensors) == 0 {
		return nil
	}
	batch := &pgx.Batch{}
	for _, sn := range sensors {
		batch.Queue(`
			INSERT INTO device_sensors (
			    device_id, sensor_index, sensor_type, description, unit,
			    first_seen, last_seen
			) VALUES ($1,$2,$3,$4,$5,NOW(),NOW())
			ON CONFLICT (device_id, sensor_index) DO UPDATE SET
			    sensor_type = EXCLUDED.sensor_type,
			    description = EXCLUDED.description,
			    unit = EXCLUDED.unit,
			    last_seen = NOW()
		`, deviceID, sn.SensorIndex, sn.SensorType, sn.Description, sn.Unit)
	}
	br := s.pool.SendBatch(ctx, batch)
	defer br.Close()
	for i := 0; i < len(sensors); i++ {
		if _, err := br.Exec(); err != nil {
			return fmt.Errorf("upsert sensor %d: %w", sensors[i].SensorIndex, err)
		}
	}
	return nil
}

// LoadActiveMaintenanceCheckIDs returns the set of service-check IDs currently
// inside an active maintenance window (scope: check, group, tag, or all).
// Used by the poller to suppress status-change writes (alerts) while still
// collecting metrics during planned downtime.
func (s *PostgresStore) LoadActiveMaintenanceCheckIDs(ctx context.Context) (map[uuid.UUID]struct{}, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT sc.id
		FROM service_checks sc
		JOIN service_check_maintenance m ON (
		       (m.scope_type = 'check' AND m.scope_check_id = sc.id)
		    OR (m.scope_type = 'group' AND m.scope_group_id = sc.group_id)
		    OR (m.scope_type = 'tag'   AND m.scope_tag       = ANY (sc.tags))
		    OR (m.scope_type = 'all')
		)
		WHERE m.starts_at <= now() AND m.ends_at >= now()
	`)
	if err != nil {
		return nil, fmt.Errorf("load maintenance ids: %w", err)
	}
	defer rows.Close()

	out := map[uuid.UUID]struct{}{}
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = struct{}{}
	}
	return out, rows.Err()
}

// LoadActiveMaintenanceDeviceIDs returns the set of device IDs currently
// inside an active device_maintenance window (scope: device, group, tag, or
// all — devices.tags is a JSONB string array). Mirrors
// LoadActiveMaintenanceCheckIDs: the poller keeps collecting metrics for
// these devices but suppresses status transitions and alerting.
func (s *PostgresStore) LoadActiveMaintenanceDeviceIDs(ctx context.Context) (map[uuid.UUID]struct{}, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT d.id
		FROM devices d
		JOIN device_maintenance m ON (
		       (m.scope_type = 'device' AND m.scope_device_id = d.id)
		    OR (m.scope_type = 'group'  AND m.scope_group_id = d.group_id)
		    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
		    OR (m.scope_type = 'all')
		)
		WHERE m.starts_at <= now() AND m.ends_at >= now()
	`)
	if err != nil {
		return nil, fmt.Errorf("load device maintenance ids: %w", err)
	}
	defer rows.Close()

	out := map[uuid.UUID]struct{}{}
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = struct{}{}
	}
	return out, rows.Err()
}

// UpdateServiceCheckStatus updates the service check's current state in PostgreSQL.
func (s *PostgresStore) UpdateServiceCheckStatus(
	ctx context.Context,
	id uuid.UUID,
	status string,
	lastCheckAt time.Time,
	responseMs float64,
	lastError string,
	tlsExpiry *time.Time,
	tlsDaysRemaining *int,
	tlsIssuer string,
	tlsSubject string,
) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE service_checks
		SET status = $1, last_check_at = $2, last_response_ms = $3, last_error = $4,
		    tls_expiry_date = $5, tls_days_remaining = $6, tls_issuer = $7, tls_subject = $8
		WHERE id = $9
	`, status, lastCheckAt, responseMs, lastError,
		tlsExpiry, tlsDaysRemaining, tlsIssuer, tlsSubject, id)
	return err
}
