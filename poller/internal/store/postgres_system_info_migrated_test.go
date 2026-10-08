package store

import (
	"context"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/checker/snmp"
)

const migratedTestDatabase = "zp_sysinfo_real"

var (
	migratedOnce sync.Once
	migratedErr  error
)

// newMigratedPool returns a pool on a database that carries the repository's
// real schema (init-postgres.sql plus every migration, 157 included), so real
// column widths and the migration 157 triggers are exercised. The database is
// one of:
//
//   - SYSINFO_TEST_MIGRATED_DSN, a disposable database you migrated yourself
//     (scripts/run-migrations.py --include-init). Its name must start with
//     "zp_" and its port must not be the default 5432;
//   - with SYSINFO_TEST_PG=1, a database of the private cluster that this
//     helper creates and migrates (needs python3 and psql, several seconds).
//
// Rows the tests add are deleted afterwards, but the database is written to,
// so never point this at the appliance.
func newMigratedPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("SYSINFO_TEST_MIGRATED_DSN")
	if dsn == "" && os.Getenv("SYSINFO_TEST_PG") == "1" {
		c, err := privateSysInfoCluster()
		if err != nil {
			t.Fatalf("SYSINFO_TEST_PG=1 but no private PostgreSQL cluster: %v", err)
		}
		migratedOnce.Do(func() { migratedErr = c.createMigratedDatabase(migratedTestDatabase) })
		if errors.Is(migratedErr, errMigrationPrereq) {
			t.Skipf("cannot build a migrated database: %v", migratedErr)
		}
		if migratedErr != nil {
			t.Fatalf("build a migrated database: %v", migratedErr)
		}
		dsn = c.dsn(migratedTestDatabase)
	}
	if dsn == "" {
		t.Skip("SYSINFO_TEST_MIGRATED_DSN not set and SYSINFO_TEST_PG!=1 (disposable migrated PostgreSQL only)")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatalf("parse DSN: %v", err)
	}
	if cfg.ConnConfig.Port == 5432 {
		t.Fatal("refusing the default PostgreSQL port; use a disposable cluster")
	}
	if !strings.HasPrefix(cfg.ConnConfig.Database, "zp_") {
		t.Fatalf("refusing database %q: a disposable fixture database is named zp_*", cfg.ConnConfig.Database)
	}
	cfg.MaxConns = 2
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

// addMigratedDevice inserts a device row and removes it when the test ends.
// The hostname gets a random suffix so a database that is reused after a
// killed run cannot collide with a leftover row.
func addMigratedDevice(t *testing.T, pool *pgxpool.Pool, hostname string) (uuid.UUID, string) {
	t.Helper()
	id := uuid.New()
	hostname += "-" + id.String()[:8]
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO devices (id, hostname) VALUES ($1, $2)`, id, hostname); err != nil {
		t.Fatalf("insert device: %v", err)
	}
	t.Cleanup(func() {
		if _, err := pool.Exec(context.Background(), `DELETE FROM devices WHERE id = $1`, id); err != nil {
			t.Logf("cleanup of device %s: %v", hostname, err)
		}
	})
	return id, hostname
}

// The same statements against the real schema. This is what the TEMP-table
// test cannot show: the real column types and widths, and that the
// migration 157 triggers on devices never turn an identity write into an error.
//
//	SYSINFO_TEST_PG=1 go test ./internal/store/ -run UpsertSystemInfoMigratedSchema -v
func TestUpsertSystemInfoMigratedSchema(t *testing.T) {
	pool := newMigratedPool(t)
	ctx := context.Background()
	s := &PostgresStore{pool: pool}

	t.Run("current statement runs without falling back and stamps the evidence", func(t *testing.T) {
		id, host := addMigratedDevice(t, pool, "mig-current")
		before := time.Now().Add(-time.Second)
		if err := s.UpsertSystemInfo(ctx, id, "1.3.6.1.4.1.9.1.1", "Cisco", "C9300", "17.3.4", host, "Cisco IOS XE Software"); err != nil {
			t.Fatalf("UpsertSystemInfo on the migrated schema: %v", err)
		}
		if s.sysInfoLegacyUntil.Load() != 0 || s.sysInfoWarnedAt.Load() != 0 {
			t.Fatal("fell back to the pre-157 statement on a schema that has migration 157")
		}
		var descr *string
		var stamped *time.Time
		var vendor, model, osVersion, oid *string
		if err := pool.QueryRow(ctx, `
			SELECT sys_descr, identity_updated_at, vendor, model, os_version, sys_object_id
			  FROM devices WHERE id = $1`, id).
			Scan(&descr, &stamped, &vendor, &model, &osVersion, &oid); err != nil {
			t.Fatal(err)
		}
		if str(descr) != "Cisco IOS XE Software" || stamped == nil || stamped.Before(before) {
			t.Fatalf("evidence not stored: sys_descr=%s identity_updated_at=%v", str(descr), stamped)
		}
		if str(vendor) != "Cisco" || str(model) != "C9300" || str(osVersion) != "17.3.4" || str(oid) != "1.3.6.1.4.1.9.1.1" {
			t.Fatalf("identity not stored: %s %s %s %s", str(vendor), str(model), str(osVersion), str(oid))
		}
	})

	// SQLSTATE 22001 on one over-long value used to roll the whole UPDATE back.
	t.Run("over-long values are cut to the real column widths and the update lands", func(t *testing.T) {
		id, host := addMigratedDevice(t, pool, "mig-long")
		in := overLongIdentity()
		before := time.Now().Add(-time.Second)
		if err := s.UpsertSystemInfo(ctx, id, in.sysObjectID, in.vendor, in.model, in.osVersion, strings.Repeat("n", 300), "descr-long"); err != nil {
			t.Fatalf("over-long values must not fail the identity write: %v", err)
		}
		var oidLen, vendorLen, modelLen, osLen, nameLen int
		var descr *string
		var stamped *time.Time
		if err := pool.QueryRow(ctx, `
			SELECT char_length(sys_object_id), char_length(vendor), char_length(model),
			       char_length(os_version), char_length(hostname), sys_descr, identity_updated_at
			  FROM devices WHERE id = $1`, id).
			Scan(&oidLen, &vendorLen, &modelLen, &osLen, &nameLen, &descr, &stamped); err != nil {
			t.Fatal(err)
		}
		if oidLen != 255 || vendorLen != 100 || modelLen != 255 || osLen != 255 {
			t.Fatalf("stored widths oid=%d vendor=%d model=%d os_version=%d, want 255/100/255/255", oidLen, vendorLen, modelLen, osLen)
		}
		if nameLen != len(host) {
			t.Fatalf("hostname changed without auto_rename_from_snmp: %d chars", nameLen)
		}
		if str(descr) != "descr-long" || stamped == nil || stamped.Before(before) {
			t.Fatalf("evidence lost with the rest of the update: sys_descr=%s identity_updated_at=%v", str(descr), stamped)
		}
	})

	// First discovery, in the order handleSNMPResult now writes: entities,
	// then system info. devices.model takes the chassis model once and the
	// profile-derived model never appears, so a normaliser tick between the
	// two statements cannot record a hardware change that never happened.
	t.Run("first discovery stores the chassis model once", func(t *testing.T) {
		id, _ := addMigratedDevice(t, pool, "mig-first")
		serial := "SYSINFO-" + id.String()[:12]
		modelNow := func() string {
			var m *string
			if err := pool.QueryRow(ctx, `SELECT model FROM devices WHERE id = $1`, id).Scan(&m); err != nil {
				t.Fatal(err)
			}
			return str(m)
		}
		if err := s.UpsertEntities(ctx, id, []snmp.Entity{
			{EntIndex: 1, ParentIndex: 0, Class: "chassis", Name: "Switch 1", SerialNumber: serial, ModelName: "C9300-48P"},
		}); err != nil {
			t.Fatalf("UpsertEntities: %v", err)
		}
		afterEntities := modelNow()
		if err := s.UpsertSystemInfo(ctx, id, "1.3.6.1.4.1.9.1.1", "Cisco", "C9300", "17.3.4", "", "Cisco IOS XE Software"); err != nil {
			t.Fatalf("UpsertSystemInfo: %v", err)
		}
		afterSystemInfo := modelNow()
		if afterEntities != "C9300-48P" || afterSystemInfo != "C9300-48P" {
			t.Fatalf("devices.model after entities=%q, after system info=%q, want the chassis model both times",
				afterEntities, afterSystemInfo)
		}
	})
}
