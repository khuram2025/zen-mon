package store

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

// The newest statement asks the shared SQL function (migration 207), and a
// binary that runs ahead of the migrations falls back on a missing function
// or view only, never on another error.
func TestMaintenanceDeviceIDQueriesFallBackOnMissingSchemaOnly(t *testing.T) {
	if !strings.Contains(maintenanceDeviceIDQueries[0], "device_maintenance_active(m, now())") {
		t.Fatal("the current statement must decide 'in force' with device_maintenance_active")
	}
	for _, q := range maintenanceDeviceIDQueries[1:] {
		if strings.Contains(q, "device_maintenance_active") {
			t.Fatal("fallback statements must not need migration 207")
		}
	}
	cases := []struct {
		code string
		want bool
	}{{"42883", true}, {"42P01", true}, {"42703", false}, {"57014", false}}
	for _, c := range cases {
		err := fmt.Errorf("wrapped: %w", &pgconn.PgError{Code: c.code})
		if got := maintenanceFallback(err); got != c.want {
			t.Errorf("code %s: fallback = %v, want %v", c.code, got, c.want)
		}
	}
	if maintenanceFallback(fmt.Errorf("connection refused")) {
		t.Error("a non-PostgreSQL error must not fall back")
	}
}

// Against the real schema: a recurring window whose occurrence covers now puts
// the device in the set, one whose occurrences are elsewhere in the day does
// not, and a one-off window behaves as before.
//
//	SYSINFO_TEST_PG=1 go test ./internal/store/ -run MaintenanceDeviceIDsMigrated -v
func TestMaintenanceDeviceIDsMigratedSchema(t *testing.T) {
	pool := newMigratedPool(t)
	ctx := context.Background()
	s := &PostgresStore{pool: pool}
	inNow, _ := addMigratedDevice(t, pool, "maint-recurring-now")
	later, _ := addMigratedDevice(t, pool, "maint-recurring-later")
	oneOff, _ := addMigratedDevice(t, pool, "maint-one-off")

	insert := func(sql string, args ...any) {
		t.Helper()
		if _, err := pool.Exec(ctx, sql, args...); err != nil {
			t.Fatalf("insert window: %v", err)
		}
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(ctx, `DELETE FROM device_maintenance WHERE scope_device_id = ANY($1)`,
			[]any{inNow, later, oneOff})
	})
	// Every day, started 10 minutes ago (UTC), 60 minutes long.
	insert(`INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at,
	            recur_days, recur_start_time, recur_duration_minutes, recur_timezone)
	        VALUES ('device', $1, now() - interval '1 day', now(),
	            '{1,2,3,4,5,6,7}', (now() AT TIME ZONE 'UTC' - interval '10 minutes')::time, 60, 'UTC')`, inNow)
	// Every day, starting in 3 hours, 30 minutes long.
	insert(`INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at,
	            recur_days, recur_start_time, recur_duration_minutes, recur_timezone)
	        VALUES ('device', $1, now() - interval '2 days', now() - interval '2 days' + interval '30 minutes',
	            '{1,2,3,4,5,6,7}', (now() AT TIME ZONE 'UTC' + interval '3 hours')::time, 30, 'UTC')`, later)
	insert(`INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at)
	        VALUES ('device', $1, now() - interval '5 minutes', now() + interval '5 minutes')`, oneOff)

	got, err := s.LoadActiveMaintenanceDeviceIDs(ctx)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if _, ok := got[inNow]; !ok {
		t.Error("a recurring window in force now must cover its device")
	}
	if _, ok := got[later]; ok {
		t.Error("a recurring window between occurrences must not cover its device")
	}
	if _, ok := got[oneOff]; !ok {
		t.Error("a one-off window in force must cover its device")
	}
}
