package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/zenplus/poller/internal/devicestatus"
	"github.com/zenplus/poller/internal/pinger"
)

// Device status persistence for the central poller (migration 161,
// docs/device-status-policy.md). devices.status is the confirmed status the
// machine displays; device_status_state holds the machine's counters so a
// restart neither resets confirmation nor re-notifies; device_flap_episodes
// is derived from the state on every state write, so a lost or repeated
// write converges. Lock order, as in every writer: devices ->
// device_status_state -> device_flap_episodes.
//
// Schema lag: while migration 161 is missing (a binary started ahead of its
// migrations), the pre-161 statements are used with in-memory state only and
// a warning is printed at most every statusWarnEvery. LoadDevices retries the
// current statement on every sync and switches back as soon as it works.

const ownerCentral = "central"

// statusWarnEvery is the minimum gap between repeated schema-lag warnings.
const statusWarnEvery = 10 * time.Minute

// loadDevicesSQL selects the centrally polled devices with their persisted
// machine state (NULL columns when the device has no state row yet).
const loadDevicesSQL = `
	SELECT d.id, d.hostname, host(d.ip_address)::text, d.ping_interval, d.ping_enabled, d.status,
	       COALESCE(d.last_seen, '1970-01-01'::timestamptz),
	       COALESCE(d.last_rtt_ms, 0),
	       d.status_changed_at,
	       s.owner, s.hard_status, s.in_maintenance, s.status_since,
	       s.fail_streak, s.ok_streak, s.clear_streak, s.window_bits, s.window_len,
	       s.reach_changes, s.perf_changes, s.reach_flapping, s.perf_flapping,
	       s.flapping_since, s.last_sample_at
	FROM devices d
	JOIN device_polling_owner owner
	  ON owner.device_id = d.id AND owner.owner_kind = 'central'
	LEFT JOIN device_status_state s ON s.device_id = d.id
	WHERE d.ping_enabled = TRUE
	  AND d.ip_address IS NOT NULL
	ORDER BY d.hostname
`

// loadDevicesLegacySQL is the pre-161 statement (schema-lag fallback).
const loadDevicesLegacySQL = `
	SELECT d.id, d.hostname, host(d.ip_address)::text, d.ping_interval, d.ping_enabled, d.status,
	       COALESCE(d.last_seen, '1970-01-01'::timestamptz),
	       COALESCE(d.last_rtt_ms, 0)
	FROM devices d
	JOIN device_polling_owner owner
	  ON owner.device_id = d.id AND owner.owner_kind = 'central'
	WHERE d.ping_enabled = TRUE
	  AND d.ip_address IS NOT NULL
	ORDER BY d.hostname
`

// stateRow holds the nullable device_status_state columns of loadDevicesSQL.
type stateRow struct {
	owner         *string
	hardStatus    *string
	inMaintenance *bool
	statusSince   *time.Time
	failStreak    *int
	okStreak      *int
	clearStreak   *int
	windowBits    *int
	windowLen     *int
	reachChanges  []time.Time
	perfChanges   []time.Time
	reachFlapping *bool
	perfFlapping  *bool
	flappingSince *time.Time
	lastSampleAt  *time.Time
}

func (r *stateRow) targets() []any {
	return []any{
		&r.owner, &r.hardStatus, &r.inMaintenance, &r.statusSince,
		&r.failStreak, &r.okStreak, &r.clearStreak, &r.windowBits, &r.windowLen,
		&r.reachChanges, &r.perfChanges, &r.reachFlapping, &r.perfFlapping,
		&r.flappingSince, &r.lastSampleAt,
	}
}

// state returns the persisted state, or nil when the device has no row.
func (r *stateRow) state() *devicestatus.State {
	if r.owner == nil {
		return nil
	}
	return &devicestatus.State{
		Owner:         *r.owner,
		HardStatus:    deref(r.hardStatus),
		InMaintenance: deref(r.inMaintenance),
		StatusSince:   utcPtr(r.statusSince),
		FailStreak:    deref(r.failStreak),
		OkStreak:      deref(r.okStreak),
		ClearStreak:   deref(r.clearStreak),
		WindowBits:    deref(r.windowBits),
		WindowLen:     deref(r.windowLen),
		ReachChanges:  utcTimes(r.reachChanges),
		PerfChanges:   utcTimes(r.perfChanges),
		ReachFlapping: deref(r.reachFlapping),
		PerfFlapping:  deref(r.perfFlapping),
		FlappingSince: utcPtr(r.flappingSince),
		LastSampleAt:  utcPtr(r.lastSampleAt),
	}
}

// applyDeviceState gives a loaded device its machine state: the persisted
// row, or — when the row is missing, belongs to another owner or another
// writer changed devices.status behind it — a silent seed from
// devices.status whose end reason closes any episode still open.
func applyDeviceState(d *pinger.Device, row *devicestatus.State, devicesStatus string, statusChangedAt *time.Time) {
	if reseed, reason := devicestatus.NeedsReseed(row, devicesStatus, ownerCentral); reseed {
		d.State = devicestatus.Seed(devicesStatus, utcPtr(statusChangedAt), ownerCentral)
		d.StateLoaded = false
		d.SeedEndReason = reason
		return
	}
	d.State = *row
	d.StateLoaded = true
	d.SeedEndReason = ""
}

// LoadDevices returns all ping-enabled, centrally polled devices with their
// machine state.
func (s *PostgresStore) LoadDevices(ctx context.Context) ([]*pinger.Device, error) {
	devices, err := s.loadDevices(ctx)
	if err == nil {
		if s.statusLegacy.Swap(false) {
			fmt.Printf("INFO: device status tables of migration 161 are present again; " +
				"confirmed status state and flap episodes are persisted again\n")
		}
		return devices, nil
	}
	if !isStatusSchemaMissing(err) {
		return nil, err
	}
	s.noteStatusSchemaMissing(time.Now(), err)
	return s.loadDevicesLegacy(ctx)
}

func (s *PostgresStore) loadDevices(ctx context.Context) ([]*pinger.Device, error) {
	rows, err := s.pool.Query(ctx, loadDevicesSQL)
	if err != nil {
		return nil, fmt.Errorf("query devices: %w", err)
	}
	defer rows.Close()

	var devices []*pinger.Device
	for rows.Next() {
		var d pinger.Device
		var ipStr string
		var intervalSec int
		var statusChangedAt *time.Time
		var st stateRow
		targets := append([]any{
			&d.ID, &d.Hostname, &ipStr, &intervalSec,
			&d.PingEnabled, &d.Status, &d.LastSeen, &d.LastRTT,
			&statusChangedAt,
		}, st.targets()...)
		if err := rows.Scan(targets...); err != nil {
			return nil, fmt.Errorf("scan device: %w", err)
		}
		d.IPAddress = net.ParseIP(ipStr)
		d.PingInterval = time.Duration(intervalSec) * time.Second
		applyDeviceState(&d, st.state(), d.Status, statusChangedAt)
		devices = append(devices, &d)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read devices: %w", err)
	}
	return devices, nil
}

// loadDevicesLegacy seeds every device silently from devices.status; there is
// no episode table to close anything in.
func (s *PostgresStore) loadDevicesLegacy(ctx context.Context) ([]*pinger.Device, error) {
	rows, err := s.pool.Query(ctx, loadDevicesLegacySQL)
	if err != nil {
		return nil, fmt.Errorf("query devices: %w", err)
	}
	defer rows.Close()

	var devices []*pinger.Device
	for rows.Next() {
		var d pinger.Device
		var ipStr string
		var intervalSec int
		if err := rows.Scan(
			&d.ID, &d.Hostname, &ipStr, &intervalSec,
			&d.PingEnabled, &d.Status, &d.LastSeen, &d.LastRTT,
		); err != nil {
			return nil, fmt.Errorf("scan device: %w", err)
		}
		d.IPAddress = net.ParseIP(ipStr)
		d.PingInterval = time.Duration(intervalSec) * time.Second
		d.State = devicestatus.Seed(d.Status, nil, ownerCentral)
		devices = append(devices, &d)
	}
	return devices, rows.Err()
}

// saveDeviceStatusSQL persists one check in one statement:
//
//   - dev: the devices row (displayed status, last_seen only for a reply,
//     the check's RTT, the flap mirror, status_changed_at when the displayed
//     status changes), only while the device is centrally owned;
//   - state: the machine state upsert ($9), taking over a row of another
//     owner (the central writer is single and ordered, so no age guard);
//   - opened: while flapping, the open episode (one per device, partial
//     unique index): inserted, or its kind widened to the union;
//   - closed: while not flapping, any open episode is closed.
//
// The episode follows the state on every state write, so a lost or repeated
// write converges. Only rows of dev are touched, so a device that moved to a
// sensor is left alone and the caller drops the check's events. The final
// SELECT reads dev and then state, so their rows are locked before the
// episode CTEs run: devices -> device_status_state -> device_flap_episodes.
const saveDeviceStatusSQL = `
	WITH dev AS (
	    UPDATE devices d
	       SET status            = $2::varchar,
	           last_seen         = CASE WHEN $3::boolean THEN $4::timestamptz ELSE d.last_seen END,
	           last_rtt_ms       = $5::float8,
	           is_flapping       = $6::boolean,
	           flapping_since    = $7::timestamptz,
	           flap_kind         = $8::varchar,
	           status_changed_at = CASE WHEN d.status IS DISTINCT FROM $2::varchar
	                                    THEN $4::timestamptz ELSE d.status_changed_at END
	     WHERE d.id = $1::uuid
	       AND EXISTS (SELECT 1 FROM device_polling_owner owner
	                    WHERE owner.device_id = d.id AND owner.owner_kind = 'central')
	    RETURNING d.id
	),
	state AS (
	    INSERT INTO device_status_state AS s (
	        device_id, owner, hard_status, in_maintenance, status_since,
	        fail_streak, ok_streak, clear_streak, window_bits, window_len,
	        reach_changes, perf_changes, reach_flapping, perf_flapping,
	        flapping_since, last_sample_at, updated_at)
	    SELECT dev.id, 'central', $10::varchar, $11::boolean, $12::timestamptz,
	           $13::smallint, $14::smallint, $15::smallint, $16::integer, $17::smallint,
	           $18::timestamptz[], $19::timestamptz[], $20::boolean, $21::boolean,
	           $7::timestamptz, $22::timestamptz, NOW()
	      FROM dev
	     WHERE $9::boolean
	    ON CONFLICT (device_id) DO UPDATE SET
	        owner          = EXCLUDED.owner,
	        hard_status    = EXCLUDED.hard_status,
	        in_maintenance = EXCLUDED.in_maintenance,
	        status_since   = EXCLUDED.status_since,
	        fail_streak    = EXCLUDED.fail_streak,
	        ok_streak      = EXCLUDED.ok_streak,
	        clear_streak   = EXCLUDED.clear_streak,
	        window_bits    = EXCLUDED.window_bits,
	        window_len     = EXCLUDED.window_len,
	        reach_changes  = EXCLUDED.reach_changes,
	        perf_changes   = EXCLUDED.perf_changes,
	        reach_flapping = EXCLUDED.reach_flapping,
	        perf_flapping  = EXCLUDED.perf_flapping,
	        flapping_since = EXCLUDED.flapping_since,
	        last_sample_at = EXCLUDED.last_sample_at,
	        updated_at     = EXCLUDED.updated_at
	    RETURNING s.device_id
	),
	opened AS (
	    INSERT INTO device_flap_episodes AS e (
	        device_id, started_at, kind, start_changes, window_minutes, status_at_start)
	    SELECT dev.id, COALESCE($7::timestamptz, $4::timestamptz), $8::varchar,
	           $23::integer, $24::integer, $2::varchar
	      FROM dev
	     WHERE $9::boolean AND $6::boolean
	    ON CONFLICT (device_id) WHERE ended_at IS NULL DO UPDATE
	       SET kind = 'both'
	     WHERE e.kind <> EXCLUDED.kind AND e.kind <> 'both'
	),
	closed AS (
	    UPDATE device_flap_episodes e
	       SET ended_at = $4::timestamptz, end_reason = $25::varchar, status_at_end = $2::varchar
	      FROM dev
	     WHERE $9::boolean AND NOT $6::boolean
	       AND e.device_id = dev.id AND e.ended_at IS NULL
	)
	SELECT (SELECT count(*) FROM dev), (SELECT count(*) FROM state)
`

// saveDeviceStatusLegacySQL is the pre-161 devices write (schema-lag
// fallback): displayed status, last_seen for a reply, the check's RTT.
const saveDeviceStatusLegacySQL = `
	UPDATE devices d
	   SET status      = $2::varchar,
	       last_seen   = CASE WHEN $3::boolean THEN $4::timestamptz ELSE d.last_seen END,
	       last_rtt_ms = $5::float8
	 WHERE d.id = $1::uuid
	   AND EXISTS (SELECT 1 FROM device_polling_owner owner
	                WHERE owner.device_id = d.id AND owner.owner_kind = 'central')
`

// saveDeviceStatusArgs binds a save to saveDeviceStatusSQL ($1..$25). Values
// are always bound, never formatted into the statement.
func saveDeviceStatusArgs(save *pinger.DeviceStatusSave) []any {
	st := save.State
	endReason := save.EndReason
	if endReason == "" {
		endReason = devicestatus.EndStable
	}
	return []any{
		save.DeviceID,             // $1
		displayedStatus(st),       // $2 devices.status, status_at_start/end
		save.Responded,            // $3
		save.At.UTC(),             // $4 last_seen, status_changed_at, episode times
		save.RTTMs,                // $5
		st.IsFlapping(),           // $6
		optTime(st.FlappingSince), // $7
		optString(st.FlapKind()),  // $8
		save.WriteState,           // $9
		hardStatus(st.HardStatus), // $10
		st.InMaintenance,          // $11
		optTime(st.StatusSince),   // $12
		st.FailStreak,             // $13
		st.OkStreak,               // $14
		st.ClearStreak,            // $15
		st.WindowBits,             // $16
		st.WindowLen,              // $17
		utcTimes(st.ReachChanges), // $18
		utcTimes(st.PerfChanges),  // $19
		st.ReachFlapping,          // $20
		st.PerfFlapping,           // $21
		optTime(st.LastSampleAt),  // $22
		save.FlapChanges,          // $23 start_changes
		save.FlapWindowMinutes,    // $24 window_minutes
		endReason,                 // $25
	}
}

// saveDeviceStatusLegacyArgs binds a save to saveDeviceStatusLegacySQL.
func saveDeviceStatusLegacyArgs(save *pinger.DeviceStatusSave) []any {
	return []any{save.DeviceID, displayedStatus(save.State), save.Responded, save.At.UTC(), save.RTTMs}
}

// SaveDeviceStatus persists one central check (see saveDeviceStatusSQL).
func (s *PostgresStore) SaveDeviceStatus(ctx context.Context, save *pinger.DeviceStatusSave) (pinger.DeviceStatusSaved, error) {
	if !s.statusLegacy.Load() {
		var devRows, stateRows int64
		err := s.pool.QueryRow(ctx, saveDeviceStatusSQL, saveDeviceStatusArgs(save)...).Scan(&devRows, &stateRows)
		if err == nil {
			return pinger.DeviceStatusSaved{Device: devRows > 0, State: stateRows > 0 || !save.WriteState}, nil
		}
		if !isStatusSchemaMissing(err) {
			return pinger.DeviceStatusSaved{}, err
		}
		s.noteStatusSchemaMissing(time.Now(), err)
	}
	tag, err := s.pool.Exec(ctx, saveDeviceStatusLegacySQL, saveDeviceStatusLegacyArgs(save)...)
	if err != nil {
		return pinger.DeviceStatusSaved{}, err
	}
	return pinger.DeviceStatusSaved{Device: tag.RowsAffected() > 0, State: !save.WriteState}, nil
}

// releaseDevicesSQL cleans up after devices that left central polling ($1:
// their ids): only while the central writer still owns their state row (a
// sensor that already took over is left alone) and the device is really not
// centrally polled any more. It clears the devices flap mirror, drops the
// state row (a later return to central polling seeds silently) and closes
// the open episode: owner_change when a sensor owns the device now,
// not_polled otherwise (ping disabled, no address, no vantage).
//
// Lock order is devices -> device_status_state -> device_flap_episodes, as in
// every writer: gone locks the devices and state rows (in id order) before
// any write. The un-referenced write CTEs run after it (episodes first), on
// rows that are already locked, so no writer can deadlock with this one.
const releaseDevicesSQL = `
	WITH gone AS (
	    SELECT d.id AS device_id, d.status,
	           CASE WHEN EXISTS (SELECT 1 FROM device_polling_owner o
	                              WHERE o.device_id = d.id
	                                AND o.owner_kind = 'sensor' AND o.sensor_id IS NOT NULL)
	                THEN 'owner_change' ELSE 'not_polled' END AS end_reason
	      FROM devices d
	      JOIN device_status_state s ON s.device_id = d.id
	     WHERE d.id = ANY($1::uuid[])
	       AND s.owner = 'central'
	       AND NOT (COALESCE(d.ping_enabled, false) AND d.ip_address IS NOT NULL
	                AND EXISTS (SELECT 1 FROM device_polling_owner o
	                             WHERE o.device_id = d.id AND o.owner_kind = 'central'))
	     ORDER BY d.id
	       FOR UPDATE OF d, s
	),
	flags AS (
	    UPDATE devices d
	       SET is_flapping = false, flapping_since = NULL, flap_kind = NULL
	      FROM gone g
	     WHERE d.id = g.device_id
	       AND (d.is_flapping OR d.flapping_since IS NOT NULL OR d.flap_kind IS NOT NULL)
	),
	dropped AS (
	    DELETE FROM device_status_state s
	     USING gone g
	     WHERE s.device_id = g.device_id
	),
	closed AS (
	    UPDATE device_flap_episodes e
	       SET ended_at = NOW(), end_reason = g.end_reason, status_at_end = g.status
	      FROM gone g
	     WHERE e.device_id = g.device_id AND e.ended_at IS NULL
	)
	SELECT count(*) FROM gone
`

// ReleaseDevices is called (through the ordered status writer) for devices
// that dropped out of LoadDevices.
func (s *PostgresStore) ReleaseDevices(ctx context.Context, ids []uuid.UUID) error {
	if len(ids) == 0 || s.statusLegacy.Load() {
		return nil
	}
	var released int64
	err := s.pool.QueryRow(ctx, releaseDevicesSQL, ids).Scan(&released)
	if err != nil && isStatusSchemaMissing(err) {
		s.noteStatusSchemaMissing(time.Now(), err)
		return nil
	}
	return err
}

// LoadStatusPolicy reads the 'monitoring' settings document and returns the
// effective policy over defaults (a missing row means all defaults).
func (s *PostgresStore) LoadStatusPolicy(ctx context.Context, defaults devicestatus.Defaults) (devicestatus.Policy, error) {
	var raw string
	err := s.pool.QueryRow(ctx,
		`SELECT COALESCE(value::text, 'null') FROM system_settings WHERE key = 'monitoring'`).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return devicestatus.PolicyFromSettings(nil, defaults), nil
	}
	if err != nil {
		return devicestatus.Policy{}, fmt.Errorf("load monitoring settings: %w", err)
	}
	settings, err := decodeMonitoringSettings([]byte(raw))
	if err != nil {
		return devicestatus.Policy{}, err
	}
	return devicestatus.PolicyFromSettings(settings, defaults), nil
}

// decodeMonitoringSettings parses the 'monitoring' document with UseNumber,
// so a literal outside the float64 range only costs its own field (it takes
// the default, as in Python) instead of failing the whole document. A value
// that is not a JSON object means "all defaults".
func decodeMonitoringSettings(raw []byte) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var doc any
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("parse monitoring settings: %w", err)
	}
	settings, _ := doc.(map[string]any)
	return settings, nil
}

// statusSchemaObjects are the migration 161 objects the status statements
// use; a 42P01/42703 naming one of them means the schema lags the binary.
var statusSchemaObjects = []string{
	"device_status_state", "device_flap_episodes",
	"is_flapping", "flapping_since", "flap_kind", "status_changed_at",
}

// isStatusSchemaMissing reports whether err is PostgreSQL's undefined_table
// (42P01) or undefined_column (42703) for an object of migration 161. Any
// other failure is not a schema-lag problem and must surface unchanged.
func isStatusSchemaMissing(err error) bool {
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) || (pgErr.Code != "42P01" && pgErr.Code != "42703") {
		return false
	}
	for _, name := range statusSchemaObjects {
		if strings.Contains(pgErr.Message, name) {
			return true
		}
	}
	return false
}

// noteStatusSchemaMissing switches the status writes to the pre-161
// statements until the next successful LoadDevices and warns, at most once
// per statusWarnEvery.
func (s *PostgresStore) noteStatusSchemaMissing(now time.Time, cause error) {
	s.statusLegacy.Store(true)
	last := s.statusWarnedAt.Load()
	if last != 0 && now.UnixNano()-last < int64(statusWarnEvery) {
		return
	}
	if s.statusWarnedAt.CompareAndSwap(last, now.UnixNano()) {
		fmt.Printf("WARN: device status tables/columns of migration 161 are missing (%v): "+
			"migration 161 (migrate-161-device-status-confirmation.sql) has not been applied, so "+
			"device status confirmation runs in memory only (counters reset on restart, no flap "+
			"episodes, devices.is_flapping not maintained). Falling back to the pre-161 statements; "+
			"they are retried on every device sync. Apply the migration with services stopped.\n", cause)
	}
}

func deref[T any](p *T) T {
	var zero T
	if p == nil {
		return zero
	}
	return *p
}

func utcPtr(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	u := t.UTC()
	return &u
}

// utcTimes copies a change list in UTC; never nil (NOT NULL array columns).
func utcTimes(in []time.Time) []time.Time {
	out := make([]time.Time, len(in))
	for i, t := range in {
		out[i] = t.UTC()
	}
	return out
}

// optTime binds a nullable timestamp (untyped nil for NULL).
func optTime(t *time.Time) any {
	if t == nil {
		return nil
	}
	return t.UTC()
}

// optString binds "" as NULL.
func optString(v string) any {
	if v == "" {
		return nil
	}
	return v
}

// displayedStatus is the devices.status written for a state: the
// maintenance overlay or the hard status (kept inside the CHECK constraint).
func displayedStatus(st devicestatus.State) string {
	if st.InMaintenance {
		return devicestatus.StatusMaintenance
	}
	return hardStatus(st.HardStatus)
}

// hardStatus keeps the state row inside its CHECK constraint: anything but a
// contract hard status is stored as unknown (Step reads it the same way).
func hardStatus(v string) string {
	switch v {
	case devicestatus.StatusUp, devicestatus.StatusDegraded, devicestatus.StatusDown, devicestatus.StatusUnknown:
		return v
	default:
		return devicestatus.StatusUnknown
	}
}
