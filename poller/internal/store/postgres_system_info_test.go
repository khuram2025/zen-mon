package store

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestIsIdentityColumnMissing(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"sys_descr undefined", &pgconn.PgError{Code: "42703", Message: `column "sys_descr" of relation "devices" does not exist`}, true},
		{"identity_updated_at undefined", &pgconn.PgError{Code: "42703", Message: `column "identity_updated_at" of relation "devices" does not exist`}, true},
		{"wrapped", fmt.Errorf("exec: %w", &pgconn.PgError{Code: "42703", Message: `column "sys_descr" of relation "devices" does not exist`}), true},
		{"another undefined column is not schema lag we can degrade around", &pgconn.PgError{Code: "42703", Message: `column "auto_rename_from_snmp" does not exist`}, false},
		{"invalid byte sequence", &pgconn.PgError{Code: "22021", Message: "invalid byte sequence for encoding \"UTF8\": 0x00"}, false},
		{"undefined table", &pgconn.PgError{Code: "42P01", Message: `relation "devices" does not exist`}, false},
		{"not a postgres error", errors.New(`column "sys_descr" does not exist`), false},
		{"context canceled", context.Canceled, false},
	}
	for _, c := range cases {
		if got := isIdentityColumnMissing(c.err); got != c.want {
			t.Errorf("%s: isIdentityColumnMissing = %v, want %v", c.name, got, c.want)
		}
	}
}

// sysInfoLegacyDDL is the part of the schema UpsertSystemInfo reads and
// writes, as it stood before migration 157, with the real column widths
// (init-postgres.sql, migrate-004-snmp.sql): an over-long value must fail
// here exactly as it does on the appliance.
const sysInfoLegacyDDL = `
	CREATE TEMP TABLE devices (
		id uuid PRIMARY KEY,
		hostname varchar(255) NOT NULL,
		sys_object_id varchar(255),
		vendor varchar(100),
		model varchar(255),
		os_version varchar(255),
		auto_rename_from_snmp boolean NOT NULL DEFAULT false
	);
	CREATE TEMP TABLE device_entities (
		device_id uuid NOT NULL,
		class text NOT NULL,
		ent_index integer NOT NULL,
		parent_index integer,
		serial_number varchar(255),
		model_name varchar(255),
		fw_revision varchar(64)
	)`

// newSysInfoTestPool returns a one-connection pool whose connection carries
// TEMP tables named like the real ones. Temporary tables shadow the real
// tables for that session and vanish with it, so nothing persists even if
// the DSN were pointed at a real database; the default PostgreSQL port is
// refused anyway.
//
// The database is SYSINFO_TEST_DSN, or, with SYSINFO_TEST_PG=1, a private
// cluster started for the test run (see pgcluster_test.go).
func newSysInfoTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
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
		t.Fatalf("parse SYSINFO_TEST_DSN: %v", err)
	}
	if cfg.ConnConfig.Port == 5432 {
		t.Fatal("refusing the default PostgreSQL port; use a disposable cluster")
	}
	cfg.MaxConns = 1
	cfg.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, err := conn.Exec(ctx, sysInfoLegacyDDL)
		return err
	}
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

type sysInfoRow struct {
	sysObjectID, vendor, model, osVersion *string
	hostname                              string
	sysDescr                              *string
	identityUpdatedAt                     *time.Time
}

func str(p *string) string {
	if p == nil {
		return "<NULL>"
	}
	return *p
}

// readSysInfoRow reads the identity columns; sys_descr and
// identity_updated_at only once the schema has them.
func readSysInfoRow(t *testing.T, pool *pgxpool.Pool, id uuid.UUID, withEvidence bool) sysInfoRow {
	t.Helper()
	var r sysInfoRow
	var err error
	if withEvidence {
		err = pool.QueryRow(context.Background(), `
			SELECT hostname, sys_object_id, vendor, model, os_version, sys_descr, identity_updated_at
			  FROM devices WHERE id = $1`, id).
			Scan(&r.hostname, &r.sysObjectID, &r.vendor, &r.model, &r.osVersion, &r.sysDescr, &r.identityUpdatedAt)
	} else {
		err = pool.QueryRow(context.Background(), `
			SELECT hostname, sys_object_id, vendor, model, os_version
			  FROM devices WHERE id = $1`, id).
			Scan(&r.hostname, &r.sysObjectID, &r.vendor, &r.model, &r.osVersion)
	}
	if err != nil {
		t.Fatalf("read device row: %v", err)
	}
	return r
}

// identityValues are the collected values that devices bounds with a column
// width.
type identityValues struct {
	sysObjectID, vendor, model, osVersion string
}

// overLongIdentity overflows every bounded column: vendor is varchar(100),
// the others varchar(255). The vendor is multi-byte, so a cut by bytes
// instead of characters would be visible.
func overLongIdentity() identityValues {
	return identityValues{
		sysObjectID: strings.Repeat("1.", 200),
		vendor:      strings.Repeat("\u00e9", 150),
		model:       strings.Repeat("m", 300),
		osVersion:   strings.Repeat("o", 1000),
	}
}

func firstRunes(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		r = r[:n]
	}
	return string(r)
}

// requireTruncated checks that each stored identity value is the leading part
// of the input, cut to the column width, and still valid UTF-8.
func requireTruncated(t *testing.T, r sysInfoRow, in identityValues) {
	t.Helper()
	for _, c := range []struct {
		name     string
		got      *string
		in       string
		maxRunes int
	}{
		{"sys_object_id", r.sysObjectID, in.sysObjectID, 255},
		{"vendor", r.vendor, in.vendor, 100},
		{"model", r.model, in.model, 255},
		{"os_version", r.osVersion, in.osVersion, 255},
	} {
		want := firstRunes(c.in, c.maxRunes)
		if c.got == nil || *c.got != want || !utf8.ValidString(*c.got) {
			t.Errorf("%s = %d runes %.20q, want the first %d characters of the input",
				c.name, utf8.RuneCountInString(str(c.got)), str(c.got), c.maxRunes)
		}
	}
}

func insertSysInfoDevice(t *testing.T, pool *pgxpool.Pool, hostname string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO devices (id, hostname) VALUES ($1, $2)`, id, hostname); err != nil {
		t.Fatal(err)
	}
	return id
}

// captureStdout returns what fn printed to os.Stdout.
func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stdout
	os.Stdout = w
	done := make(chan string)
	go func() {
		b, _ := io.ReadAll(r)
		done <- string(b)
	}()
	func() {
		defer func() {
			os.Stdout = orig
			w.Close()
		}()
		fn()
	}()
	return <-done
}

// Run it with a private throwaway cluster (needs the PostgreSQL server
// binaries and a non-root account, see pgcluster_test.go):
//
//	SYSINFO_TEST_PG=1 go test ./internal/store/ -run UpsertSystemInfoPostgres -v
//
// or against a disposable cluster of your own:
//
//	SYSINFO_TEST_DSN="postgres://zen@127.0.0.1:55471/zp_sysinfo_1?sslmode=disable" \
//	    go test ./internal/store/ -run UpsertSystemInfoPostgres -v
func TestUpsertSystemInfoPostgres(t *testing.T) {
	pool := newSysInfoTestPool(t)
	ctx := context.Background()
	s := &PostgresStore{pool: pool}
	id := uuid.New()
	if _, err := pool.Exec(ctx, `
		INSERT INTO devices (id, hostname, vendor, model) VALUES ($1, 'edge-01', 'OldVendor', 'OldModel')`, id); err != nil {
		t.Fatal(err)
	}

	// 1. Poller binary ahead of the schema: the pre-157 identity writeback
	// must keep working instead of failing as a whole.
	t.Run("schema before migration 157 degrades to the legacy statement", func(t *testing.T) {
		err := s.UpsertSystemInfo(ctx, id, "1.3.6.1.4.1.9.1.1", "Cisco", "C9300", "17.3.4", "sw-new", "Cisco IOS XE Software")
		if err != nil {
			t.Fatalf("UpsertSystemInfo on a pre-157 schema: %v", err)
		}
		r := readSysInfoRow(t, pool, id, false)
		if str(r.sysObjectID) != "1.3.6.1.4.1.9.1.1" || str(r.vendor) != "Cisco" ||
			str(r.model) != "C9300" || str(r.osVersion) != "17.3.4" {
			t.Fatalf("identity not written: %+v", r)
		}
		if r.hostname != "edge-01" {
			t.Fatalf("hostname renamed without opt-in: %q", r.hostname)
		}
		if until := s.sysInfoLegacyUntil.Load(); until <= time.Now().UnixNano() {
			t.Fatalf("legacy window not set (until=%d)", until)
		}
		if s.sysInfoWarnedAt.Load() == 0 {
			t.Fatal("no warning recorded for the missing columns")
		}
	})

	// A value longer than its column used to fail the whole statement with
	// SQLSTATE 22001, in the legacy statement as in the current one. It runs
	// on a row of its own so the sticky values below stay as they were.
	t.Run("legacy statement stores over-long values truncated", func(t *testing.T) {
		legacyID := insertSysInfoDevice(t, pool, "edge-legacy-long")
		in := overLongIdentity()
		if err := s.UpsertSystemInfo(ctx, legacyID, in.sysObjectID, in.vendor, in.model, in.osVersion, "", "descr"); err != nil {
			t.Fatalf("over-long values must not fail the legacy identity write: %v", err)
		}
		requireTruncated(t, readSysInfoRow(t, pool, legacyID, false), in)
		if s.sysInfoLegacyUntil.Load() <= time.Now().UnixNano() {
			t.Fatal("legacy window ended by an unrelated value")
		}
	})

	// 2. Migration 157 lands while the poller runs.
	if _, err := pool.Exec(ctx, `
		ALTER TABLE devices ADD COLUMN sys_descr text, ADD COLUMN identity_updated_at timestamptz`); err != nil {
		t.Fatal(err)
	}

	t.Run("legacy window holds until its retry time", func(t *testing.T) {
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", "ignored while degraded"); err != nil {
			t.Fatal(err)
		}
		r := readSysInfoRow(t, pool, id, true)
		if r.sysDescr != nil || r.identityUpdatedAt != nil {
			t.Fatalf("evidence written inside the legacy window: descr=%s at=%v", str(r.sysDescr), r.identityUpdatedAt)
		}
	})

	t.Run("current statement takes over once the retry time passes", func(t *testing.T) {
		s.sysInfoLegacyUntil.Store(time.Now().Add(-time.Second).UnixNano())
		before := time.Now().Add(-time.Second)
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", "Cisco IOS XE Software, Version 17.03.04"); err != nil {
			t.Fatal(err)
		}
		r := readSysInfoRow(t, pool, id, true)
		if str(r.sysDescr) != "Cisco IOS XE Software, Version 17.03.04" {
			t.Fatalf("sys_descr = %s", str(r.sysDescr))
		}
		if r.identityUpdatedAt == nil || r.identityUpdatedAt.Before(before) {
			t.Fatalf("identity_updated_at not stamped: %v", r.identityUpdatedAt)
		}
		if s.sysInfoWarnedAt.Load() != 0 {
			t.Fatal("degraded state not cleared after the columns appeared")
		}
		// And the earlier sticky identity values survived the empty inputs.
		if str(r.vendor) != "Cisco" || str(r.osVersion) != "17.3.4" || str(r.sysObjectID) != "1.3.6.1.4.1.9.1.1" {
			t.Fatalf("sticky values lost: %+v", r)
		}
	})

	t.Run("empty sysDescr never overwrites a stored one but freshness advances", func(t *testing.T) {
		first := readSysInfoRow(t, pool, id, true)
		time.Sleep(20 * time.Millisecond)
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", ""); err != nil {
			t.Fatal(err)
		}
		// Whitespace and NUL only is empty after sanitising.
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", " \x00 \n"); err != nil {
			t.Fatal(err)
		}
		second := readSysInfoRow(t, pool, id, true)
		if str(second.sysDescr) != str(first.sysDescr) {
			t.Fatalf("sys_descr overwritten: %q -> %q", str(first.sysDescr), str(second.sysDescr))
		}
		if !second.identityUpdatedAt.After(*first.identityUpdatedAt) {
			t.Fatalf("identity_updated_at did not advance: %v -> %v", first.identityUpdatedAt, second.identityUpdatedAt)
		}
	})

	t.Run("sysDescr is sanitised before storing", func(t *testing.T) {
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", "\x00  Padded\xff descr \x00"); err != nil {
			t.Fatalf("NUL / invalid UTF-8 must not fail the identity write: %v", err)
		}
		if got := str(readSysInfoRow(t, pool, id, true).sysDescr); got != "Padded\uFFFD descr" {
			t.Fatalf("sys_descr = %q", got)
		}

		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", strings.Repeat("é", 3000)); err != nil {
			t.Fatal(err)
		}
		got := str(readSysInfoRow(t, pool, id, true).sysDescr)
		if n := utf8.RuneCountInString(got); n != maxSysDescrRunes || !utf8.ValidString(got) {
			t.Fatalf("stored %d runes (valid=%v), want %d", n, utf8.ValidString(got), maxSysDescrRunes)
		}
	})

	// SQLSTATE 22001 on one over-long value used to roll the whole UPDATE
	// back: sys_object_id, vendor, model, os_version, the sys_descr evidence
	// and the identity_updated_at stamp were all lost, on every poll.
	t.Run("over-long values are stored truncated and the whole update lands", func(t *testing.T) {
		longID := insertSysInfoDevice(t, pool, "edge-long")
		in := overLongIdentity()
		before := time.Now().Add(-time.Second)
		if err := s.UpsertSystemInfo(ctx, longID, in.sysObjectID, in.vendor, in.model, in.osVersion, "", "descr-long"); err != nil {
			t.Fatalf("over-long values must not fail the identity write: %v", err)
		}
		r := readSysInfoRow(t, pool, longID, true)
		requireTruncated(t, r, in)
		if str(r.sysDescr) != "descr-long" {
			t.Fatalf("sys_descr = %s, lost with the rest of the update", str(r.sysDescr))
		}
		if r.identityUpdatedAt == nil || r.identityUpdatedAt.Before(before) {
			t.Fatalf("identity_updated_at not stamped: %v", r.identityUpdatedAt)
		}
		if s.sysInfoLegacyUntil.Load() > time.Now().UnixNano() || s.sysInfoWarnedAt.Load() != 0 {
			t.Fatal("an over-long value was treated as missing columns")
		}

		// The sticky rules are unchanged: an empty input keeps the stored
		// (truncated) value, a shorter real one replaces it.
		if err := s.UpsertSystemInfo(ctx, longID, "", "", "", "", "", ""); err != nil {
			t.Fatal(err)
		}
		requireTruncated(t, readSysInfoRow(t, pool, longID, true), in)
		if err := s.UpsertSystemInfo(ctx, longID, "", "Cisco", "C9300", "17.3.4", "", ""); err != nil {
			t.Fatal(err)
		}
		r = readSysInfoRow(t, pool, longID, true)
		if str(r.vendor) != "Cisco" || str(r.model) != "C9300" || str(r.osVersion) != "17.3.4" {
			t.Fatalf("a normal value did not replace the truncated one: %+v", r)
		}
	})

	t.Run("one over-long value does not veto the others", func(t *testing.T) {
		oneID := insertSysInfoDevice(t, pool, "edge-one-long")
		long := strings.Repeat("o", 300)
		if err := s.UpsertSystemInfo(ctx, oneID, "1.3.6.1.4.1.9.1.1", "Cisco", "C9300", long, "", "descr-one"); err != nil {
			t.Fatal(err)
		}
		r := readSysInfoRow(t, pool, oneID, true)
		if str(r.vendor) != "Cisco" || str(r.model) != "C9300" || str(r.sysObjectID) != "1.3.6.1.4.1.9.1.1" ||
			str(r.sysDescr) != "descr-one" || len(str(r.osVersion)) != 255 {
			t.Fatalf("update did not land as a whole: %+v", r)
		}
	})

	t.Run("over-long sysName is truncated into hostname only with auto_rename_from_snmp", func(t *testing.T) {
		nameID := insertSysInfoDevice(t, pool, "edge-name")
		long := strings.Repeat("n", 300)
		if err := s.UpsertSystemInfo(ctx, nameID, "", "", "", "", long, "x"); err != nil {
			t.Fatalf("over-long sysName must not fail the identity write: %v", err)
		}
		if got := readSysInfoRow(t, pool, nameID, true).hostname; got != "edge-name" {
			t.Fatalf("hostname = %q, want it untouched without opt-in", got)
		}
		if _, err := pool.Exec(ctx, `UPDATE devices SET auto_rename_from_snmp = true WHERE id = $1`, nameID); err != nil {
			t.Fatal(err)
		}
		if err := s.UpsertSystemInfo(ctx, nameID, "", "", "", "", long, "x"); err != nil {
			t.Fatalf("over-long sysName must not fail the identity write: %v", err)
		}
		if got := readSysInfoRow(t, pool, nameID, true).hostname; got != long[:255] {
			t.Fatalf("hostname = %d chars, want the first 255", len(got))
		}
	})

	t.Run("chassis model keeps precedence over the sysDescr-derived model", func(t *testing.T) {
		if _, err := pool.Exec(ctx, `
			INSERT INTO device_entities (device_id, class, ent_index, parent_index, model_name)
			VALUES ($1, 'chassis', 1, 0, 'C9300-48P')`, id); err != nil {
			t.Fatal(err)
		}
		if err := s.UpsertSystemInfo(ctx, id, "", "", "Profile-Model", "", "", "x"); err != nil {
			t.Fatal(err)
		}
		if got := str(readSysInfoRow(t, pool, id, true).model); got != "C9300-48P" {
			t.Fatalf("model = %q, want the chassis model", got)
		}
	})

	t.Run("hostname follows sysName only with auto_rename_from_snmp", func(t *testing.T) {
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "renamed-by-snmp", "x"); err != nil {
			t.Fatal(err)
		}
		if got := readSysInfoRow(t, pool, id, true).hostname; got != "edge-01" {
			t.Fatalf("hostname = %q, want it untouched", got)
		}
		if _, err := pool.Exec(ctx, `UPDATE devices SET auto_rename_from_snmp = true WHERE id = $1`, id); err != nil {
			t.Fatal(err)
		}
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "renamed-by-snmp", "x"); err != nil {
			t.Fatal(err)
		}
		if got := readSysInfoRow(t, pool, id, true).hostname; got != "renamed-by-snmp" {
			t.Fatalf("hostname = %q, want the sysName", got)
		}
		if err := s.UpsertSystemInfo(ctx, id, "", "", "", "", "", "x"); err != nil {
			t.Fatal(err)
		}
		if got := readSysInfoRow(t, pool, id, true).hostname; got != "renamed-by-snmp" {
			t.Fatalf("empty sysName blanked the hostname: %q", got)
		}
	})

	t.Run("other database errors surface and do not trigger the fallback", func(t *testing.T) {
		before := readSysInfoRow(t, pool, id, true)
		// vendor is not sanitised by the store; PostgreSQL refuses the NUL.
		err := s.UpsertSystemInfo(ctx, id, "", "bad\x00vendor", "", "", "", "y")
		if err == nil {
			t.Fatal("expected the NUL byte in vendor to be rejected")
		}
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "22021" {
			t.Fatalf("error = %v, want SQLSTATE 22021", err)
		}
		if s.sysInfoLegacyUntil.Load() > time.Now().UnixNano() || s.sysInfoWarnedAt.Load() != 0 {
			t.Fatal("an unrelated error was treated as missing columns")
		}
		after := readSysInfoRow(t, pool, id, true)
		if str(after.sysDescr) != str(before.sysDescr) || str(after.vendor) != str(before.vendor) {
			t.Fatalf("failed statement changed the row: %+v -> %+v", before, after)
		}
	})
}

// The statements bound every collected value to its column width before the
// sticky test, and the legacy statement shares the same SET list. This part
// needs no database, so it also runs where the PostgreSQL tests above skip.
func TestSystemInfoSQLBoundsEveryCollectedValue(t *testing.T) {
	for _, c := range []struct {
		param  string
		column string
		width  int
	}{
		{"$2", "sys_object_id", 255},
		{"$3", "vendor", 100},
		{"$4", "model", 255},
		{"$5", "os_version", 255},
		{"$6", "hostname", 255},
	} {
		re := regexp.MustCompile(`NULLIF\(LEFT\(\` + c.param + `,\s*` + fmt.Sprint(c.width) + `\),\s*''\)`)
		if len(re.FindAllString(systemInfoSetSQL, -1)) != 1 {
			t.Errorf("%s (%s) is not bound with NULLIF(LEFT(%s, %d), '') in systemInfoSetSQL",
				c.column, c.param, c.param, c.width)
		}
	}
	if strings.Contains(systemInfoSetSQL, "NULLIF($") {
		t.Error("a collected value reaches the sticky COALESCE without LEFT")
	}
	for name, stmt := range map[string]string{
		"current": upsertSystemInfoSQL,
		"legacy":  upsertSystemInfoLegacySQL,
	} {
		if !strings.Contains(stmt, systemInfoSetSQL) {
			t.Errorf("%s statement does not use the shared bounded SET list", name)
		}
	}
}

// The remedy named by the fallback warning must not be a live schema sync.
// Migration 157 locks devices while it runs, so applying it under the running
// poller stalls the whole pool. The once-per-interval rate limit stays.
func TestIdentityColumnsMissingWarning(t *testing.T) {
	cause := &pgconn.PgError{Code: "42703", Message: `column "sys_descr" of relation "devices" does not exist`}
	s := &PostgresStore{}
	start := time.Now()

	first := captureStdout(t, func() { s.noteIdentityColumnsMissing(start, cause) })
	for _, want := range []string{"WARN", "migration 157", "services stopped", "maintenance window"} {
		if !strings.Contains(first, want) {
			t.Errorf("warning does not mention %q: %s", want, first)
		}
	}
	for _, old := range []string{"or complete the update", "run scripts/sync-schema.py or"} {
		if strings.Contains(first, old) {
			t.Errorf("warning still recommends a live schema sync (%q): %s", old, first)
		}
	}

	// Inside the interval the legacy window is renewed but nothing is printed.
	again := captureStdout(t, func() {
		s.noteIdentityColumnsMissing(start.Add(sysInfoLegacyRetry), cause)
		s.noteIdentityColumnsMissing(start.Add(sysInfoWarnEvery-time.Second), cause)
	})
	if again != "" {
		t.Errorf("warning repeated inside %v: %q", sysInfoWarnEvery, again)
	}
	if want := start.Add(sysInfoWarnEvery - time.Second + sysInfoLegacyRetry).UnixNano(); s.sysInfoLegacyUntil.Load() != want {
		t.Errorf("legacy window not renewed on every detection")
	}

	// After the interval it warns again.
	later := captureStdout(t, func() { s.noteIdentityColumnsMissing(start.Add(sysInfoWarnEvery+time.Second), cause) })
	if !strings.Contains(later, "WARN") {
		t.Errorf("no warning after %v: %q", sysInfoWarnEvery, later)
	}
}
