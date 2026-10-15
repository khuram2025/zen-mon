package store

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/devicestatus"
	"github.com/zenplus/poller/internal/pinger"
)

func tsPtr(t time.Time) *time.Time { return &t }

// maxPlaceholder returns the highest $n in a statement.
func maxPlaceholder(sql string) int {
	n := 0
	for _, m := range regexp.MustCompile(`\$(\d+)`).FindAllStringSubmatch(sql, -1) {
		if v, _ := strconv.Atoi(m[1]); v > n {
			n = v
		}
	}
	return n
}

func TestSaveDeviceStatusArgsBindValues(t *testing.T) {
	id := uuid.New()
	at := time.Date(2026, 10, 14, 9, 30, 0, 123000, time.FixedZone("x", 3600))
	since := at.Add(-time.Hour)
	flapSince := at.Add(-2 * time.Minute)
	st := devicestatus.State{
		Owner: "central", HardStatus: "down", StatusSince: &since,
		FailStreak: 7, OkStreak: 1, ClearStreak: 2, WindowBits: 0b10110, WindowLen: 5,
		ReachChanges:  []time.Time{at.Add(-10 * time.Minute), at.Add(-5 * time.Minute)},
		ReachFlapping: true, FlappingSince: &flapSince, LastSampleAt: &at,
	}
	save := &pinger.DeviceStatusSave{
		DeviceID: id, At: at, Responded: false, RTTMs: 0, State: st, WriteState: true,
		FlapChanges: 4, FlapWindowMinutes: 30, EndReason: "maintenance", TakeoverOwner: "7f9c",
	}
	args := saveDeviceStatusArgs(save)
	if len(args) != 26 || maxPlaceholder(saveDeviceStatusSQL) != 26 {
		t.Fatalf("%d args for $%d", len(args), maxPlaceholder(saveDeviceStatusSQL))
	}
	want := map[int]any{
		1: id, 2: "down", 3: false, 5: 0.0, 6: true, 8: "reachability", 9: true, 10: "down",
		11: false, 13: 7, 14: 1, 15: 2, 16: 0b10110, 17: 5, 20: true, 21: false,
		23: 4, 24: 30, 25: "maintenance", 26: "7f9c",
	}
	for pos, v := range want {
		if args[pos-1] != v {
			t.Fatalf("$%d = %#v, want %#v", pos, args[pos-1], v)
		}
	}
	for pos, v := range map[int]time.Time{4: at, 7: flapSince, 12: since, 22: at} {
		got, ok := args[pos-1].(time.Time)
		if !ok || !got.Equal(v) || got.Location() != time.UTC {
			t.Fatalf("$%d = %#v, want %v in UTC", pos, args[pos-1], v)
		}
	}
	reach, ok := args[17].([]time.Time)
	if !ok || len(reach) != 2 || !reach[1].Equal(st.ReachChanges[1]) {
		t.Fatalf("$18 = %#v", args[17])
	}
	if perf, ok := args[18].([]time.Time); !ok || perf == nil || len(perf) != 0 {
		t.Fatalf("$19 must be an empty, non-nil list (NOT NULL array), got %#v", args[18])
	}
	if strings.ContainsAny(saveDeviceStatusSQL, "%?") {
		t.Fatal("the statement must bind its values")
	}
}

func TestSaveDeviceStatusArgsNullsAndOverlay(t *testing.T) {
	st := devicestatus.Seed("up", nil, "central")
	st.InMaintenance = true
	args := saveDeviceStatusArgs(&pinger.DeviceStatusSave{DeviceID: uuid.New(), At: time.Now(), Responded: true, RTTMs: 1.5, State: st})
	if args[1] != "maintenance" || args[9] != "up" || args[10] != true {
		t.Fatalf("displayed/hard = %#v/%#v", args[1], args[9])
	}
	for _, pos := range []int{7, 8, 12, 22, 26} {
		if args[pos-1] != nil {
			t.Fatalf("$%d = %#v, want NULL", pos, args[pos-1])
		}
	}
	if args[24] != "stable" || args[8] != false {
		t.Fatalf("default end reason %#v, write_state %#v", args[24], args[8])
	}
	// A status outside the CHECK constraints is never written.
	st.InMaintenance, st.HardStatus = false, ""
	if got := saveDeviceStatusArgs(&pinger.DeviceStatusSave{State: st}); got[1] != "unknown" || got[9] != "unknown" {
		t.Fatalf("invalid hard status bound as %#v/%#v", got[1], got[9])
	}
	legacy := saveDeviceStatusLegacyArgs(&pinger.DeviceStatusSave{State: devicestatus.Seed("down", nil, "central"), Responded: false})
	if len(legacy) != 5 || maxPlaceholder(saveDeviceStatusLegacySQL) != 5 || legacy[1] != "down" || legacy[2] != false {
		t.Fatalf("legacy args %#v", legacy)
	}
}

// last_rtt_ms: a reply's RTT; 0 once confirmed down; an unconfirmed miss
// keeps the last measured RTT (NULL leaves the column as it is).
func TestLastRTTArg(t *testing.T) {
	up := devicestatus.Seed("up", nil, "central")
	down := devicestatus.Seed("down", nil, "central")
	inMaint := down
	inMaint.InMaintenance = true
	for _, tc := range []struct {
		name string
		save pinger.DeviceStatusSave
		want any
	}{
		{"reply", pinger.DeviceStatusSave{Responded: true, RTTMs: 1.5, State: up}, 1.5},
		{"reply while down", pinger.DeviceStatusSave{Responded: true, RTTMs: 2, State: down}, 2.0},
		{"unconfirmed miss", pinger.DeviceStatusSave{State: up}, nil},
		{"confirmed down", pinger.DeviceStatusSave{State: down}, 0.0},
		{"down behind maintenance", pinger.DeviceStatusSave{State: inMaint}, 0.0},
	} {
		save := tc.save
		if got := saveDeviceStatusArgs(&save)[4]; got != tc.want {
			t.Fatalf("%s: $5 = %#v, want %#v", tc.name, got, tc.want)
		}
		if got := saveDeviceStatusLegacyArgs(&save)[4]; got != tc.want {
			t.Fatalf("%s: legacy $5 = %#v, want %#v", tc.name, got, tc.want)
		}
	}
	for _, sql := range []string{saveDeviceStatusSQL, saveDeviceStatusLegacySQL} {
		if !strings.Contains(sql, "COALESCE($5::float8, d.last_rtt_ms)") {
			t.Fatal("last_rtt_ms must keep its value for a NULL $5")
		}
	}
}

// Every central write is limited to what the loader polls: a check still
// queued when ping was switched off (or the address removed) must not
// restore the flap mirror, the state row or an episode the server's release
// just cleared.
func TestCentralWritesUseTheLoaderPredicate(t *testing.T) {
	const polled = "COALESCE(d.ping_enabled, false) AND d.ip_address IS NOT NULL"
	for name, sql := range map[string]string{
		"save": saveDeviceStatusSQL, "legacy save": saveDeviceStatusLegacySQL, "release": releaseDevicesSQL,
	} {
		if !strings.Contains(sql, polled) || !strings.Contains(sql, "owner_kind = 'central'") {
			t.Fatalf("%s statement does not use the central polling predicate", name)
		}
	}
	for _, sql := range []string{loadDevicesSQL, loadDevicesLegacySQL} {
		if !strings.Contains(sql, "d.ping_enabled = TRUE") || !strings.Contains(sql, "d.ip_address IS NOT NULL") {
			t.Fatal("loader predicate changed: keep the writes in step with it")
		}
	}
}

func TestApplyDeviceStateReseedRule(t *testing.T) {
	changed := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	stored := func(owner, hard string) *devicestatus.State {
		s := devicestatus.Seed(hard, nil, owner)
		s.FailStreak = 2
		return &s
	}
	for _, tc := range []struct {
		name       string
		row        *devicestatus.State
		status     string
		loaded     bool
		reason     string
		wantStatus string
	}{
		{"no row: silent seed", nil, "down", false, "reseed", "down"},
		{"another owner: taken over", stored("7f9c", "up"), "up", false, "owner_change", "up"},
		{"another owner, status changed too", stored("7f9c", "up"), "down", false, "owner_change", "down"},
		{"status changed behind the row", stored("central", "up"), "unknown", false, "reseed", "unknown"},
		{"maintenance overlay keeps the row", stored("central", "down"), "maintenance", true, "", "down"},
		{"matching row is used", stored("central", "degraded"), "degraded", true, "", "degraded"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := pinger.Device{SeedOwner: "stale"}
			applyDeviceState(&d, tc.row, tc.status, &changed)
			if d.StateLoaded != tc.loaded || d.SeedEndReason != tc.reason || d.State.HardStatus != tc.wantStatus {
				t.Fatalf("loaded=%v reason=%q hard=%q", d.StateLoaded, d.SeedEndReason, d.State.HardStatus)
			}
			// Only an owner_change seed may take a row over: the one it replaced.
			wantOwner := ""
			if tc.reason == "owner_change" {
				wantOwner = tc.row.Owner
			}
			if d.SeedOwner != wantOwner {
				t.Fatalf("seed owner %q, want %q", d.SeedOwner, wantOwner)
			}
			if tc.loaded && d.State.FailStreak != 2 {
				t.Fatal("persisted counters lost")
			}
			if !tc.loaded && (d.State.FailStreak != 0 || d.State.Owner != "central" ||
				d.State.StatusSince == nil || !d.State.StatusSince.Equal(changed)) {
				t.Fatalf("seed = %+v", d.State)
			}
		})
	}
}

func TestStateRowMapsColumns(t *testing.T) {
	var r stateRow
	if r.state() != nil {
		t.Fatal("a missing row must read as nil")
	}
	owner, hard := "central", "degraded"
	inMaint, rf := true, true
	fail, ok, clear, bits, wlen := 3, 1, 2, 9, 4
	at := time.Date(2026, 10, 14, 9, 0, 0, 0, time.FixedZone("x", 7200))
	r = stateRow{owner: &owner, hardStatus: &hard, inMaintenance: &inMaint, statusSince: &at,
		failStreak: &fail, okStreak: &ok, clearStreak: &clear, windowBits: &bits, windowLen: &wlen,
		reachChanges: []time.Time{at}, reachFlapping: &rf, flappingSince: &at, lastSampleAt: &at}
	s := r.state()
	if s.Owner != owner || s.HardStatus != hard || !s.InMaintenance || s.FailStreak != 3 || s.OkStreak != 1 ||
		s.ClearStreak != 2 || s.WindowBits != 9 || s.WindowLen != 4 || !s.ReachFlapping || s.PerfFlapping {
		t.Fatalf("state = %+v", s)
	}
	if s.StatusSince.Location() != time.UTC || len(s.ReachChanges) != 1 || s.PerfChanges == nil {
		t.Fatalf("times/lists = %+v", s)
	}
	if len(r.targets()) != 15 {
		t.Fatal("scan targets do not match the selected state columns")
	}
}

func TestDecodeMonitoringSettings(t *testing.T) {
	got, err := decodeMonitoringSettings([]byte(`{"degraded_rtt_ms": 1e400, "degraded_confirm_checks": 4, "flap_detection_enabled": false}`))
	if err != nil {
		t.Fatal(err)
	}
	p := devicestatus.PolicyFromSettings(got, devicestatus.Defaults{DegradedRTTMs: 250})
	if p.DegradedRTTMs != 250 || p.DegradedConfirmChecks != 4 || p.FlapDetectionEnabled {
		t.Fatalf("an out-of-range literal must only cost its own field: %+v", p)
	}
	for _, raw := range []string{`null`, `[1, 2]`, `"text"`} {
		got, err := decodeMonitoringSettings([]byte(raw))
		if err != nil || got != nil {
			t.Fatalf("%s: %v %v", raw, got, err)
		}
	}
	if _, err := decodeMonitoringSettings([]byte(`{`)); err == nil {
		t.Fatal("invalid JSON must be an error (the engine keeps its previous policy)")
	}
}

func TestIsStatusSchemaMissing(t *testing.T) {
	for _, tc := range []struct {
		code, msg string
		want      bool
	}{
		{"42P01", `relation "device_status_state" does not exist`, true},
		{"42P01", `relation "device_flap_episodes" does not exist`, true},
		{"42703", `column d.status_changed_at does not exist`, true},
		{"42703", `column "is_flapping" of relation "devices" does not exist`, true},
		{"42703", `column "sys_descr" does not exist`, false},
		{"42P01", `relation "devices" does not exist`, false},
		{"23514", `new row violates check constraint "device_status_state_hard_status_check"`, false},
	} {
		err := fmt.Errorf("wrapped: %w", &pgconn.PgError{Code: tc.code, Message: tc.msg})
		if got := isStatusSchemaMissing(err); got != tc.want {
			t.Fatalf("%s %s: %v", tc.code, tc.msg, got)
		}
	}
	if isStatusSchemaMissing(fmt.Errorf("device_status_state: timeout")) {
		t.Fatal("only PostgreSQL schema errors count")
	}
}

func TestDeviceStatusLogRowBindsValuesVerbatim(t *testing.T) {
	id := uuid.New()
	reason := "Packet loss 33.33% exceeds 10% by 23.33 percentage points; 3 of the last 5 checks over limits or unanswered"
	at := time.Date(2026, 10, 14, 9, 0, 0, 0, time.FixedZone("x", 3600))
	row := deviceStatusLogRow(&pinger.StatusChange{DeviceID: id, OldStatus: "up", NewStatus: "degraded", Reason: reason, Timestamp: at}, 0)
	if len(row) != 6 || row[0] != id || row[2] != "up" || row[3] != "degraded" || row[4] != reason || row[5] != uint64(0) {
		t.Fatalf("row = %#v", row)
	}
	if ts, ok := row[1].(time.Time); !ok || !ts.Equal(at) || ts.Location() != time.UTC {
		t.Fatalf("timestamp = %#v, want the check time in UTC", row[1])
	}
	if strings.Contains(deviceStatusLogInsert, "VALUES") || strings.ContainsAny(deviceStatusLogInsert, "'%?") {
		t.Fatalf("insert statement must not embed values: %s", deviceStatusLogInsert)
	}
}

func TestStatusChangePayloadIncludesIsFlapping(t *testing.T) {
	id := uuid.New()
	at := time.Date(2026, 10, 14, 9, 0, 0, 0, time.UTC)
	data, err := json.Marshal(statusChangePayload(&pinger.StatusChange{
		DeviceID: id, OldStatus: "down", NewStatus: "up", Reason: "Responding again", Timestamp: at, Flapping: true, IsFlapping: true,
	}))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatal(err)
	}
	if len(got) != 6 || got["device_id"] != id.String() || got["is_flapping"] != true ||
		got["timestamp"] != "2026-10-14T09:00:00Z" || got["new_status"] != "up" {
		t.Fatalf("payload = %v", got)
	}
}

// --- Real schema (migration 161), disposable database only ---
//
//	SYSINFO_TEST_PG=1 go test ./internal/store/ -run DeviceStatusMigratedSchema -v

func addPolledDevice(t *testing.T, pool *pgxpool.Pool, status string) uuid.UUID {
	t.Helper()
	return addPolledDeviceAt(t, pool, status, "192.0.2.10")
}

// addPolledDeviceAt adds a centrally polled device at ip (unique per device).
func addPolledDeviceAt(t *testing.T, pool *pgxpool.Pool, status, ip string) uuid.UUID {
	t.Helper()
	id, _ := addMigratedDevice(t, pool, "status")
	if _, err := pool.Exec(context.Background(),
		`UPDATE devices SET ip_address = $3::inet, ping_enabled = true, status = $2 WHERE id = $1`, id, status, ip); err != nil {
		t.Fatal(err)
	}
	return id
}

type episodeRow struct {
	startedAt                time.Time
	endedAt                  *time.Time
	kind                     string
	startChanges, windowMins *int
	statusAtStart, statusEnd *string
	endReason                *string
}

func episodes(t *testing.T, pool *pgxpool.Pool, id uuid.UUID) []episodeRow {
	t.Helper()
	rows, err := pool.Query(context.Background(), `
		SELECT started_at, ended_at, kind, start_changes, window_minutes, status_at_start, status_at_end, end_reason
		  FROM device_flap_episodes WHERE device_id = $1 ORDER BY started_at, created_at`, id)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []episodeRow
	for rows.Next() {
		var e episodeRow
		if err := rows.Scan(&e.startedAt, &e.endedAt, &e.kind, &e.startChanges, &e.windowMins,
			&e.statusAtStart, &e.statusEnd, &e.endReason); err != nil {
			t.Fatal(err)
		}
		out = append(out, e)
	}
	return out
}

func loadedDevice(t *testing.T, s *PostgresStore, id uuid.UUID) *pinger.Device {
	t.Helper()
	devices, err := s.LoadDevices(context.Background())
	if err != nil {
		t.Fatalf("LoadDevices: %v", err)
	}
	for _, d := range devices {
		if d.ID == id {
			return d
		}
	}
	return nil
}

func TestDeviceStatusMigratedSchema(t *testing.T) {
	pool := newMigratedPool(t)
	ctx := context.Background()
	s := &PostgresStore{pool: pool}
	base := time.Date(2026, 10, 14, 9, 0, 0, 0, time.UTC)

	t.Run("seed, save and reload the machine state", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		d := loadedDevice(t, s, id)
		if d == nil || d.StateLoaded || d.SeedEndReason != "reseed" || d.State.HardStatus != "up" {
			t.Fatalf("first load = %+v", d)
		}
		st := d.State
		st.FailStreak, st.WindowBits, st.WindowLen = 1, 1, 1
		st.LastSampleAt = tsPtr(base)
		saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: base, State: st, WriteState: true, FlapWindowMinutes: 30, EndReason: "reseed"})
		if err != nil || !saved.Device || !saved.State {
			t.Fatalf("save: %+v %v", saved, err)
		}
		d = loadedDevice(t, s, id)
		if !d.StateLoaded || d.SeedEndReason != "" || d.State.FailStreak != 1 || d.State.WindowLen != 1 ||
			d.State.Owner != "central" || d.State.LastSampleAt == nil || !d.State.LastSampleAt.Equal(base) {
			t.Fatalf("reloaded = %+v", d.State)
		}
		var lastSeen *time.Time
		var changedAt *time.Time
		var status string
		if err := pool.QueryRow(ctx, `SELECT status, last_seen, status_changed_at FROM devices WHERE id = $1`, id).
			Scan(&status, &lastSeen, &changedAt); err != nil {
			t.Fatal(err)
		}
		if status != "up" || lastSeen != nil || changedAt != nil {
			t.Fatalf("a failed check touched last_seen or status_changed_at: %s %v %v", status, lastSeen, changedAt)
		}

		// Down: displayed change stamps status_changed_at with the check time.
		st.HardStatus, st.FailStreak, st.StatusSince = "down", 3, tsPtr(base.Add(time.Minute))
		st.ReachChanges = []time.Time{base.Add(time.Minute)}
		if _, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: base.Add(time.Minute), State: st, WriteState: true}); err != nil {
			t.Fatal(err)
		}
		// A reply that changes nothing but last_seen (devices row only).
		if _, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: base.Add(90 * time.Second), Responded: true, RTTMs: 2.5, State: st}); err != nil {
			t.Fatal(err)
		}
		var rtt float64
		if err := pool.QueryRow(ctx, `SELECT status, last_seen, status_changed_at, last_rtt_ms FROM devices WHERE id = $1`, id).
			Scan(&status, &lastSeen, &changedAt, &rtt); err != nil {
			t.Fatal(err)
		}
		if status != "down" || changedAt == nil || !changedAt.Equal(base.Add(time.Minute)) ||
			lastSeen == nil || !lastSeen.Equal(base.Add(90*time.Second)) || rtt != 2.5 {
			t.Fatalf("devices row: %s changed=%v seen=%v rtt=%v", status, changedAt, lastSeen, rtt)
		}
		// last_rtt_ms: an unanswered check keeps the measured RTT until Down is
		// confirmed, then reads 0.
		up := st
		up.HardStatus, up.FailStreak = "up", 1
		for _, step := range []struct {
			st   devicestatus.State
			want float64
		}{{up, 2.5}, {st, 0}} {
			if _, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
				DeviceID: id, At: base.Add(2 * time.Minute), State: step.st, WriteState: true}); err != nil {
				t.Fatal(err)
			}
			if err := pool.QueryRow(ctx, `SELECT last_rtt_ms FROM devices WHERE id = $1`, id).Scan(&rtt); err != nil {
				t.Fatal(err)
			}
			if rtt != step.want {
				t.Fatalf("unanswered %s check: last_rtt_ms = %v, want %v", step.st.HardStatus, rtt, step.want)
			}
		}
		var reach []time.Time
		if err := pool.QueryRow(ctx, `SELECT reach_changes FROM device_status_state WHERE device_id = $1`, id).Scan(&reach); err != nil {
			t.Fatal(err)
		}
		if len(reach) != 1 || !reach[0].Equal(base.Add(time.Minute)) {
			t.Fatalf("reach_changes = %v", reach)
		}
	})

	t.Run("flap episode follows the state", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		st := devicestatus.Seed("up", nil, "central")
		start := base.Add(10 * time.Minute)
		st.ReachChanges = []time.Time{base, base.Add(time.Minute), base.Add(2 * time.Minute), start}
		st.ReachFlapping, st.FlappingSince = true, &start
		save := func(at time.Time, st devicestatus.State, reason string) {
			t.Helper()
			saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: at, Responded: true, State: st,
				WriteState: true, FlapChanges: len(st.ReachChanges) + len(st.PerfChanges), FlapWindowMinutes: 30, EndReason: reason})
			if err != nil || !saved.Device {
				t.Fatalf("save: %+v %v", saved, err)
			}
		}
		save(start, st, "stable")
		save(start.Add(time.Minute), st, "stable") // repeated write: still one open episode
		st.PerfFlapping = true                     // kind widens to both
		save(start.Add(2*time.Minute), st, "stable")
		st.PerfFlapping = false // kind stays the union
		save(start.Add(3*time.Minute), st, "stable")

		eps := episodes(t, pool, id)
		if len(eps) != 1 || eps[0].endedAt != nil || eps[0].kind != "both" || !eps[0].startedAt.Equal(start) ||
			eps[0].startChanges == nil || *eps[0].startChanges != 4 || *eps[0].windowMins != 30 || *eps[0].statusAtStart != "up" {
			t.Fatalf("open episode = %+v", eps)
		}
		var flapping bool
		var kind *string
		if err := pool.QueryRow(ctx, `SELECT is_flapping, flap_kind FROM devices WHERE id = $1`, id).Scan(&flapping, &kind); err != nil {
			t.Fatal(err)
		}
		if !flapping || kind == nil || *kind != "reachability" {
			t.Fatalf("devices mirror: %v %v", flapping, kind)
		}

		st.ReachFlapping, st.FlappingSince = false, nil
		st.InMaintenance = true
		end := start.Add(4 * time.Minute)
		save(end, st, "maintenance")
		eps = episodes(t, pool, id)
		if len(eps) != 1 || eps[0].endedAt == nil || !eps[0].endedAt.Equal(end) ||
			*eps[0].endReason != "maintenance" || *eps[0].statusEnd != "maintenance" {
			t.Fatalf("closed episode = %+v", eps)
		}
		if err := pool.QueryRow(ctx, `SELECT is_flapping, flap_kind FROM devices WHERE id = $1`, id).Scan(&flapping, &kind); err != nil {
			t.Fatal(err)
		}
		if flapping || kind != nil {
			t.Fatalf("mirror not cleared: %v %v", flapping, kind)
		}
	})

	t.Run("a device moved to a sensor is not written and its sensor row is taken over on return", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		if _, err := pool.Exec(ctx, `
			INSERT INTO monitoring_policies (target_type, target_id, controller_enabled) VALUES ('device', $1, false)`, id); err != nil {
			t.Fatal(err)
		}
		saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: base, State: devicestatus.Seed("down", nil, "central"), WriteState: true})
		if err != nil || saved.Device || saved.State {
			t.Fatalf("save on a non-central device: %+v %v", saved, err)
		}
		var n int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM device_status_state WHERE device_id = $1`, id).Scan(&n); err != nil || n != 0 {
			t.Fatalf("state row written for a non-central device (%d, %v)", n, err)
		}
		if loadedDevice(t, s, id) != nil {
			t.Fatal("non-central device loaded")
		}

		// Back to central: the sensor's row and open episode are taken over.
		sensorOwner := uuid.New().String()
		if _, err := pool.Exec(ctx,
			`DELETE FROM monitoring_policies WHERE target_type = 'device' AND target_id = $1`, id); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `
			INSERT INTO device_status_state (device_id, owner, hard_status, reach_flapping, fail_streak)
			VALUES ($1, $2, 'up', true, 5)`, id, sensorOwner); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `
			INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, $2, 'reachability')`, id, base); err != nil {
			t.Fatal(err)
		}
		d := loadedDevice(t, s, id)
		if d == nil || d.StateLoaded || d.SeedEndReason != "owner_change" || d.SeedOwner != sensorOwner ||
			d.State.FailStreak != 0 || d.State.IsFlapping() {
			t.Fatalf("takeover load = %+v", d)
		}
		st := d.State
		st.LastSampleAt = tsPtr(base.Add(time.Minute))
		saved, err = s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: base.Add(time.Minute), Responded: true, State: st, WriteState: true,
			EndReason: d.SeedEndReason, TakeoverOwner: d.SeedOwner})
		if err != nil || !saved.Device || !saved.State || saved.Foreign {
			t.Fatalf("takeover save: %+v %v", saved, err)
		}
		var owner string
		if err := pool.QueryRow(ctx, `SELECT owner FROM device_status_state WHERE device_id = $1`, id).Scan(&owner); err != nil || owner != "central" {
			t.Fatalf("owner %q (%v)", owner, err)
		}
		eps := episodes(t, pool, id)
		if len(eps) != 1 || eps[0].endedAt == nil || *eps[0].endReason != "owner_change" || *eps[0].statusEnd != "up" {
			t.Fatalf("sensor episode not closed on takeover: %+v", eps)
		}
	})

	t.Run("release clears devices that left central polling", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		start := base
		st := devicestatus.Seed("up", nil, "central")
		st.ReachChanges = []time.Time{start}
		st.ReachFlapping, st.FlappingSince = true, &start
		if _, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: start, Responded: true, State: st, WriteState: true, FlapChanges: 4, FlapWindowMinutes: 30}); err != nil {
			t.Fatal(err)
		}
		// Still polled: release is a no-op.
		if err := s.ReleaseDevices(ctx, []uuid.UUID{id}); err != nil {
			t.Fatal(err)
		}
		if eps := episodes(t, pool, id); len(eps) != 1 || eps[0].endedAt != nil {
			t.Fatalf("released a device that is still polled: %+v", eps)
		}
		if _, err := pool.Exec(ctx, `UPDATE devices SET ping_enabled = false WHERE id = $1`, id); err != nil {
			t.Fatal(err)
		}
		if err := s.ReleaseDevices(ctx, []uuid.UUID{id, uuid.New()}); err != nil {
			t.Fatal(err)
		}
		eps := episodes(t, pool, id)
		if len(eps) != 1 || eps[0].endedAt == nil || *eps[0].endReason != "not_polled" {
			t.Fatalf("episode = %+v", eps)
		}
		var flapping bool
		var rows int
		if err := pool.QueryRow(ctx, `
			SELECT d.is_flapping, (SELECT count(*) FROM device_status_state s WHERE s.device_id = d.id)
			  FROM devices d WHERE d.id = $1`, id).Scan(&flapping, &rows); err != nil {
			t.Fatal(err)
		}
		if flapping || rows != 0 {
			t.Fatalf("is_flapping=%v state rows=%d after release", flapping, rows)
		}
	})

	// A flapping device whose ping is switched off: the server's release
	// (device_service.release_device_status) runs while checks of the device
	// are still queued in the poller (it only drops the device at its next
	// sync). Those checks must not restore the flag, the state row or an
	// episode, and the poller's own release must leave nothing behind.
	t.Run("checks queued when ping is switched off write nothing", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		start := base
		st := devicestatus.Seed("up", nil, "central")
		st.ReachChanges = []time.Time{start.Add(-3 * time.Minute), start.Add(-2 * time.Minute), start.Add(-time.Minute), start}
		st.ReachFlapping, st.FlappingSince = true, &start
		st.LastSampleAt = &start
		save := func(at time.Time, writeState bool) pinger.DeviceStatusSaved {
			t.Helper()
			saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: at, Responded: true, RTTMs: 1,
				State: st, WriteState: writeState, FlapChanges: 4, FlapWindowMinutes: 30})
			if err != nil {
				t.Fatal(err)
			}
			return saved
		}
		if saved := save(start, true); !saved.Device || !saved.State {
			t.Fatalf("flapping save: %+v", saved)
		}
		serverRelease(t, pool, id)

		// A steady reply (devices row only) and a changed state (full write).
		for _, writeState := range []bool{false, true} {
			if saved := save(start.Add(time.Minute), writeState); saved.Device || saved.Foreign || (writeState && saved.State) {
				t.Fatalf("write_state=%v save after ping was switched off: %+v", writeState, saved)
			}
			assertReleased(t, pool, id, "after a queued check")
		}
		if err := s.ReleaseDevices(ctx, []uuid.UUID{id}); err != nil {
			t.Fatal(err)
		}
		assertReleased(t, pool, id, "after the poller's release")

		// The pre-161 statement is limited the same way.
		tag, err := pool.Exec(ctx, saveDeviceStatusLegacySQL, saveDeviceStatusLegacyArgs(&pinger.DeviceStatusSave{
			DeviceID: id, At: start.Add(2 * time.Minute), Responded: true, State: devicestatus.Seed("down", nil, "central")})...)
		if err != nil || tag.RowsAffected() != 0 {
			t.Fatalf("legacy save on a ping-disabled device: %v %v", tag, err)
		}
	})

	t.Run("release clears a flap mirror and episode without a state row", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		if _, err := pool.Exec(ctx, `
			UPDATE devices SET ping_enabled = false, is_flapping = true, flapping_since = $2, flap_kind = 'reachability'
			 WHERE id = $1`, id, base); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `
			INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, $2, 'reachability')`, id, base); err != nil {
			t.Fatal(err)
		}
		// A sensor's row is left alone (it already took over).
		sensorDev := addPolledDeviceAt(t, pool, "up", "192.0.2.11")
		sensorOwner := uuid.New().String()
		if _, err := pool.Exec(ctx, `
			UPDATE devices SET ping_enabled = false, is_flapping = true, flap_kind = 'reachability' WHERE id = $1`, sensorDev); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `
			INSERT INTO device_status_state (device_id, owner, hard_status, reach_flapping) VALUES ($1, $2, 'up', true)`,
			sensorDev, sensorOwner); err != nil {
			t.Fatal(err)
		}
		if err := s.ReleaseDevices(ctx, []uuid.UUID{sensorDev, id}); err != nil {
			t.Fatal(err)
		}
		assertReleased(t, pool, id, "after the release")
		var flapping bool
		var owner string
		if err := pool.QueryRow(ctx, `
			SELECT d.is_flapping, s.owner FROM devices d JOIN device_status_state s ON s.device_id = d.id
			 WHERE d.id = $1`, sensorDev).Scan(&flapping, &owner); err != nil || !flapping || owner != sensorOwner {
			t.Fatalf("a sensor-owned device was released: flapping=%v owner=%q (%v)", flapping, owner, err)
		}
	})

	// Ownership moved to a sensor and back within one poller sync: the
	// sensor confirmed a change and left its row. The poller's state is from
	// before; its saves must not overwrite the sensor's status silently.
	t.Run("a state row another writer wrote since is foreign", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		d := loadedDevice(t, s, id)
		st := d.State
		st.LastSampleAt = tsPtr(base)
		if saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{
			DeviceID: id, At: base, Responded: true, State: st, WriteState: true, EndReason: "reseed"}); err != nil || !saved.Device {
			t.Fatalf("central save: %+v %v", saved, err)
		}
		sensorOwner := uuid.New().String()
		changed := base.Add(time.Minute)
		for _, stmt := range []struct {
			sql  string
			args []any
		}{
			{`UPDATE devices SET status = 'down', status_changed_at = $2 WHERE id = $1`, []any{id, changed}},
			{`UPDATE device_status_state SET owner = $2, hard_status = 'down', fail_streak = 1 WHERE device_id = $1`, []any{id, sensorOwner}},
			{`INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, $2, 'reachability')`, []any{id, changed}},
		} {
			if _, err := pool.Exec(ctx, stmt.sql, stmt.args...); err != nil {
				t.Fatalf("%s: %v", stmt.sql, err)
			}
		}
		stale := st
		stale.LastSampleAt = tsPtr(base.Add(2 * time.Minute))
		for _, tc := range []struct {
			writeState bool
			takeover   string
		}{{false, ""}, {true, ""}, {true, uuid.New().String()}} {
			saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: base.Add(2 * time.Minute),
				Responded: true, RTTMs: 1, State: stale, WriteState: tc.writeState, TakeoverOwner: tc.takeover})
			if err != nil || !saved.Foreign || saved.Device || saved.State || saved.ForeignOwner != sensorOwner ||
				saved.Status != "down" || saved.StatusChangedAt == nil || !saved.StatusChangedAt.Equal(changed) {
				t.Fatalf("stale save %+v: %+v %v", tc, saved, err)
			}
		}
		var status, owner string
		if err := pool.QueryRow(ctx, `
			SELECT d.status, s.owner FROM devices d JOIN device_status_state s ON s.device_id = d.id WHERE d.id = $1`,
			id).Scan(&status, &owner); err != nil || status != "down" || owner != sensorOwner {
			t.Fatalf("foreign row overwritten: %s %s (%v)", status, owner, err)
		}
		if eps := episodes(t, pool, id); len(eps) != 1 || eps[0].endedAt != nil {
			t.Fatalf("sensor episode touched: %+v", eps)
		}

		// Reseeded from the sensor's down, the next (responding) check
		// confirms up and takes the row over, closing the sensor's episode.
		up := devicestatus.Seed("down", &changed, "central")
		up.HardStatus, up.OkStreak, up.StatusSince = "up", 1, tsPtr(base.Add(3*time.Minute))
		up.LastSampleAt = tsPtr(base.Add(3 * time.Minute))
		saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: base.Add(3 * time.Minute),
			Responded: true, RTTMs: 1, State: up, WriteState: true, EndReason: "owner_change", TakeoverOwner: sensorOwner})
		if err != nil || !saved.Device || !saved.State || saved.Foreign {
			t.Fatalf("takeover save: %+v %v", saved, err)
		}
		if err := pool.QueryRow(ctx, `
			SELECT d.status, s.owner FROM devices d JOIN device_status_state s ON s.device_id = d.id WHERE d.id = $1`,
			id).Scan(&status, &owner); err != nil || status != "up" || owner != "central" {
			t.Fatalf("after takeover: %s %s (%v)", status, owner, err)
		}
		if eps := episodes(t, pool, id); len(eps) != 1 || eps[0].endedAt == nil || *eps[0].endReason != "owner_change" {
			t.Fatalf("sensor episode not closed: %+v", eps)
		}
		// The central row is the engine's own again.
		if saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: base.Add(4 * time.Minute),
			Responded: true, RTTMs: 1, State: up}); err != nil || !saved.Device || saved.Foreign {
			t.Fatalf("central save after takeover: %+v %v", saved, err)
		}
	})

	// The save locks the devices row before its statement takes a snapshot,
	// so a save that waited for another status writer (which locks the
	// devices row first) sees everything that writer committed, also rows of
	// other tables: a sensor's state row, the server's ping-off release.
	t.Run("a save waiting for another writer's lock sees what it committed", func(t *testing.T) {
		id := addPolledDevice(t, pool, "up")
		start := base
		flapping := devicestatus.Seed("up", nil, "central")
		flapping.ReachChanges = []time.Time{start.Add(-3 * time.Minute), start.Add(-2 * time.Minute), start.Add(-time.Minute), start}
		flapping.ReachFlapping, flapping.FlappingSince, flapping.LastSampleAt = true, &start, &start
		saveAsync := func(at time.Time, st devicestatus.State) <-chan pinger.DeviceStatusSaved {
			out := make(chan pinger.DeviceStatusSaved, 1)
			go func() {
				saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: at, Responded: true, RTTMs: 1,
					State: st, WriteState: true, FlapChanges: 4, FlapWindowMinutes: 30})
				if err != nil {
					t.Errorf("save: %v", err)
				}
				out <- saved
			}()
			return out
		}
		if saved := <-saveAsync(start, flapping); !saved.Device || !saved.State {
			t.Fatalf("flapping save: %+v", saved)
		}

		// A sensor that owned the device for a moment writes its row.
		sensorOwner := uuid.New().String()
		changed := start.Add(time.Minute)
		commitBlocked := holdDeviceLock(t, pool, id, []sqlStmt{
			{`UPDATE device_status_state SET owner = $2, hard_status = 'down', reach_flapping = false,
			         flapping_since = NULL WHERE device_id = $1`, []any{id, sensorOwner}},
			{`UPDATE devices SET status = 'down', status_changed_at = $2, is_flapping = false,
			         flapping_since = NULL, flap_kind = NULL WHERE id = $1`, []any{id, changed}},
		})
		pending := saveAsync(start.Add(2*time.Minute), flapping)
		commitBlocked(true)
		if saved := <-pending; !saved.Foreign || saved.ForeignOwner != sensorOwner || saved.Status != "down" || saved.Device {
			t.Fatalf("save that waited for the sensor's write: %+v", saved)
		}
		var owner string
		var isFlapping bool
		if err := pool.QueryRow(ctx, `
			SELECT s.owner, d.is_flapping FROM devices d JOIN device_status_state s ON s.device_id = d.id
			 WHERE d.id = $1`, id).Scan(&owner, &isFlapping); err != nil || owner != sensorOwner || isFlapping {
			t.Fatalf("sensor write overwritten: owner %q flapping %v (%v)", owner, isFlapping, err)
		}

		// The server's ping-off release commits while a check waits.
		if _, err := pool.Exec(ctx, `UPDATE device_status_state SET owner = 'central' WHERE device_id = $1`, id); err != nil {
			t.Fatal(err)
		}
		if saved := <-saveAsync(start.Add(3*time.Minute), flapping); !saved.Device {
			t.Fatalf("flapping save: %+v", saved)
		}
		commitBlocked = holdDeviceLock(t, pool, id, serverReleaseStmts(id))
		pending = saveAsync(start.Add(4*time.Minute), flapping)
		commitBlocked(true)
		if saved := <-pending; saved.Device || saved.Foreign {
			t.Fatalf("save that waited for the release: %+v", saved)
		}
		var flags bool
		var rows, open int
		if err := pool.QueryRow(ctx, `
			SELECT d.is_flapping, (SELECT count(*) FROM device_status_state s WHERE s.device_id = d.id),
			       (SELECT count(*) FROM device_flap_episodes e WHERE e.device_id = d.id AND e.ended_at IS NULL)
			  FROM devices d WHERE d.id = $1`, id).Scan(&flags, &rows, &open); err != nil || flags || rows != 0 || open != 0 {
			t.Fatalf("release undone: is_flapping=%v state rows=%d open episodes=%d (%v)", flags, rows, open, err)
		}
	})

	t.Run("status policy from the monitoring settings", func(t *testing.T) {
		var prior *string
		_ = pool.QueryRow(ctx, `SELECT value::text FROM system_settings WHERE key = 'monitoring'`).Scan(&prior)
		t.Cleanup(func() {
			if prior == nil {
				_, _ = pool.Exec(ctx, `DELETE FROM system_settings WHERE key = 'monitoring'`)
				return
			}
			_, _ = pool.Exec(ctx, `UPDATE system_settings SET value = $1::jsonb WHERE key = 'monitoring'`, *prior)
		})
		if _, err := pool.Exec(ctx, `DELETE FROM system_settings WHERE key = 'monitoring'`); err != nil {
			t.Fatal(err)
		}
		p, err := s.LoadStatusPolicy(ctx, devicestatus.Defaults{DegradedRTTMs: 150, DownConfirmChecks: 4})
		if err != nil || p.DegradedRTTMs != 150 || p.DownConfirmChecks != 4 || p.DegradedConfirmChecks != 3 {
			t.Fatalf("defaults: %+v %v", p, err)
		}
		if _, err := pool.Exec(ctx, `
			INSERT INTO system_settings (key, value) VALUES ('monitoring', '{"degraded_rtt_ms": 250, "degraded_window_checks": 7, "flap_start_changes": 6}')`); err != nil {
			t.Fatal(err)
		}
		p, err = s.LoadStatusPolicy(ctx, devicestatus.Defaults{DegradedRTTMs: 150, DownConfirmChecks: 4})
		if err != nil || p.DegradedRTTMs != 250 || p.DegradedRTTClearMs != 200 || p.DegradedWindowChecks != 7 ||
			p.FlapStartChanges != 6 || p.DownConfirmChecks != 4 {
			t.Fatalf("stored: %+v %v", p, err)
		}
	})
}

type sqlStmt struct {
	sql  string
	args []any
}

// serverReleaseStmts is what device_service.update_device does when ping is
// switched off (release_device_status), after locking the devices row.
func serverReleaseStmts(id uuid.UUID) []sqlStmt {
	return []sqlStmt{
		{`DELETE FROM device_status_state WHERE device_id = $1`, []any{id}},
		{`UPDATE device_flap_episodes
		     SET ended_at = GREATEST(started_at, NOW()), end_reason = 'not_polled',
		         status_at_end = (SELECT status FROM devices WHERE id = $1)
		   WHERE device_id = $1 AND ended_at IS NULL`, []any{id}},
		{`UPDATE devices SET ping_enabled = false, is_flapping = false, flapping_since = NULL, flap_kind = NULL
		   WHERE id = $1`, []any{id}},
	}
}

// serverRelease runs serverReleaseStmts as the server does: one transaction.
func serverRelease(t *testing.T, pool *pgxpool.Pool, id uuid.UUID) {
	t.Helper()
	holdDeviceLock(t, pool, id, serverReleaseStmts(id))(false)
}

// holdDeviceLock starts another status writer's transaction: it locks the
// devices row (as every status writer does first) and runs stmts. The
// returned commit, called with wait, first waits until another backend is
// blocked behind this transaction.
func holdDeviceLock(t *testing.T, pool *pgxpool.Pool, id uuid.UUID, stmts []sqlStmt) (commit func(wait bool)) {
	t.Helper()
	ctx := context.Background()
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = tx.Rollback(ctx) })
	if _, err := tx.Exec(ctx, `SELECT 1 FROM devices WHERE id = $1 FOR NO KEY UPDATE`, id); err != nil {
		t.Fatal(err)
	}
	for _, st := range stmts {
		if _, err := tx.Exec(ctx, st.sql, st.args...); err != nil {
			t.Fatalf("%s: %v", st.sql, err)
		}
	}
	return func(wait bool) {
		t.Helper()
		for deadline := time.Now().Add(10 * time.Second); wait; {
			var blocked int
			if err := tx.QueryRow(ctx, `
				SELECT count(*) FROM pg_locks
				 WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))`).Scan(&blocked); err != nil {
				t.Fatal(err)
			}
			if blocked > 0 {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("no backend is waiting for the devices row lock")
			}
			time.Sleep(10 * time.Millisecond)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
	}
}

// assertReleased checks a device that left central polling: no flap mirror,
// no state row, and every flap episode closed as not_polled.
func assertReleased(t *testing.T, pool *pgxpool.Pool, id uuid.UUID, when string) {
	t.Helper()
	var flapping bool
	var since *time.Time
	var kind *string
	var rows int
	if err := pool.QueryRow(context.Background(), `
		SELECT d.is_flapping, d.flapping_since, d.flap_kind,
		       (SELECT count(*) FROM device_status_state s WHERE s.device_id = d.id)
		  FROM devices d WHERE d.id = $1`, id).Scan(&flapping, &since, &kind, &rows); err != nil {
		t.Fatal(err)
	}
	if flapping || since != nil || kind != nil || rows != 0 {
		t.Fatalf("%s: is_flapping=%v since=%v kind=%v state rows=%d", when, flapping, since, kind, rows)
	}
	eps := episodes(t, pool, id)
	if len(eps) != 1 || eps[0].endedAt == nil || eps[0].endReason == nil || *eps[0].endReason != "not_polled" {
		t.Fatalf("%s: episodes = %+v, want the one closed as not_polled", when, eps)
	}
}

// statusLegacyDDL is the part of the schema the device status statements use,
// as it stood before migration 161, in TEMP tables (they shadow nothing and
// vanish with the session).
const statusLegacyDDL = `
	CREATE TEMP TABLE devices (
		id uuid PRIMARY KEY,
		hostname varchar(255) NOT NULL,
		ip_address inet,
		ping_interval integer DEFAULT 60,
		ping_enabled boolean DEFAULT true,
		status varchar(20) DEFAULT 'unknown',
		last_seen timestamptz,
		last_rtt_ms double precision
	);
	CREATE TEMP TABLE device_polling_owner (
		device_id uuid NOT NULL,
		owner_kind text NOT NULL,
		sensor_id uuid
	)`

// status161DDL adds the migration 161 objects the statements need.
const status161DDL = `
	ALTER TABLE devices
		ADD COLUMN is_flapping boolean NOT NULL DEFAULT false,
		ADD COLUMN flapping_since timestamptz,
		ADD COLUMN flap_kind varchar(16),
		ADD COLUMN status_changed_at timestamptz;
	CREATE TEMP TABLE device_status_state (
		device_id uuid PRIMARY KEY, owner text NOT NULL,
		hard_status varchar(20) NOT NULL DEFAULT 'unknown', in_maintenance boolean NOT NULL DEFAULT false,
		status_since timestamptz, fail_streak smallint NOT NULL DEFAULT 0, ok_streak smallint NOT NULL DEFAULT 0,
		clear_streak smallint NOT NULL DEFAULT 0, window_bits integer NOT NULL DEFAULT 0,
		window_len smallint NOT NULL DEFAULT 0, reach_changes timestamptz[] NOT NULL DEFAULT '{}',
		perf_changes timestamptz[] NOT NULL DEFAULT '{}', reach_flapping boolean NOT NULL DEFAULT false,
		perf_flapping boolean NOT NULL DEFAULT false, flapping_since timestamptz, last_sample_at timestamptz,
		updated_at timestamptz NOT NULL DEFAULT now());
	CREATE TEMP TABLE device_flap_episodes (
		id uuid PRIMARY KEY DEFAULT gen_random_uuid(), device_id uuid NOT NULL, started_at timestamptz NOT NULL,
		ended_at timestamptz, kind varchar(16) NOT NULL, start_changes integer, window_minutes integer,
		status_at_start varchar(20), status_at_end varchar(20), end_reason varchar(20));
	CREATE UNIQUE INDEX ON device_flap_episodes(device_id) WHERE ended_at IS NULL`

// SYSINFO_TEST_PG=1 go test ./internal/store/ -run DeviceStatusSchemaLag -v
func TestDeviceStatusSchemaLagFallback(t *testing.T) {
	dsn := os.Getenv("SYSINFO_TEST_DSN")
	if dsn == "" && os.Getenv("SYSINFO_TEST_PG") == "1" {
		c, err := privateSysInfoCluster()
		if err != nil {
			t.Fatalf("SYSINFO_TEST_PG=1 but no private PostgreSQL cluster: %v", err)
		}
		dsn = c.baseDSN
	}
	if dsn == "" {
		t.Skip("SYSINFO_TEST_DSN not set and SYSINFO_TEST_PG!=1 (disposable PostgreSQL only)")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ConnConfig.Port == 5432 {
		t.Fatal("refusing the default PostgreSQL port; use a disposable cluster")
	}
	cfg.MaxConns = 1
	cfg.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, err := conn.Exec(ctx, statusLegacyDDL)
		return err
	}
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	ctx := context.Background()
	s := &PostgresStore{pool: pool}

	id := uuid.New()
	for _, stmt := range []string{
		`INSERT INTO devices (id, hostname, ip_address, status) VALUES ($1, 'lagging', '192.0.2.20', 'down')`,
		`INSERT INTO device_polling_owner (device_id, owner_kind) VALUES ($1, 'central')`,
	} {
		if _, err := pool.Exec(ctx, stmt, id); err != nil {
			t.Fatal(err)
		}
	}

	d := loadedDevice(t, s, id)
	if d == nil || !s.statusLegacy.Load() || d.StateLoaded || d.SeedEndReason != "" || d.State.HardStatus != "down" {
		t.Fatalf("legacy load = %+v (legacy=%v)", d, s.statusLegacy.Load())
	}
	st := d.State
	st.HardStatus, st.OkStreak = "up", 1
	at := time.Date(2026, 10, 14, 9, 0, 0, 0, time.UTC)
	saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: at, Responded: true, RTTMs: 3, State: st, WriteState: true})
	if err != nil || !saved.Device || saved.State {
		t.Fatalf("legacy save: %+v %v", saved, err)
	}
	if err := s.ReleaseDevices(ctx, []uuid.UUID{id}); err != nil {
		t.Fatalf("release must be a no-op while the schema lags: %v", err)
	}
	// A device that is not centrally polled any more is not written.
	if _, err := pool.Exec(ctx, `UPDATE devices SET ping_enabled = false WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	if saved, err := s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: at, State: d.State}); err != nil || saved.Device {
		t.Fatalf("legacy save on a ping-disabled device: %+v %v", saved, err)
	}
	if _, err := pool.Exec(ctx, `UPDATE devices SET ping_enabled = true WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	var status string
	var seen *time.Time
	if err := pool.QueryRow(ctx, `SELECT status, last_seen FROM devices WHERE id = $1`, id).Scan(&status, &seen); err != nil {
		t.Fatal(err)
	}
	if status != "up" || seen == nil || !seen.Equal(at) {
		t.Fatalf("legacy write: %s %v", status, seen)
	}

	// Migration applied while running: the next sync switches back.
	if _, err := pool.Exec(ctx, status161DDL); err != nil {
		t.Fatal(err)
	}
	d = loadedDevice(t, s, id)
	if d == nil || s.statusLegacy.Load() || d.SeedEndReason != "reseed" {
		t.Fatalf("current load = %+v (legacy=%v)", d, s.statusLegacy.Load())
	}
	saved, err = s.SaveDeviceStatus(ctx, &pinger.DeviceStatusSave{DeviceID: id, At: at.Add(time.Minute), Responded: true, State: d.State, WriteState: true})
	if err != nil || !saved.Device || !saved.State {
		t.Fatalf("current save: %+v %v", saved, err)
	}
}
