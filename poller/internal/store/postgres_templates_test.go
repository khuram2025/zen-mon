package store

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestIsUndefinedColumn(t *testing.T) {
	if !isUndefinedColumn(fmt.Errorf("query: %w", &pgconn.PgError{Code: "42703", Message: `column p.pilot_device_ids does not exist`})) {
		t.Fatal("wrapped 42703 not recognised")
	}
	for _, err := range []error{nil, fmt.Errorf("plain"), &pgconn.PgError{Code: "42P01"}} {
		if isUndefinedColumn(err) {
			t.Fatalf("%v treated as an undefined column", err)
		}
	}
}

// The current and pre-189 SNMP device statements share everything but the
// template groups column.
func TestLoadSNMPDevicesStatementsDifferOnlyInGroups(t *testing.T) {
	cur := strings.Replace(loadSNMPDevicesSQL, snmpDevicesGroupsSQL, "<groups>", 1)
	old := strings.Replace(loadSNMPDevicesLegacySQL, snmpDevicesGroupsLegacySQL, "<groups>", 1)
	if cur != old || !strings.Contains(cur, "<groups>") {
		t.Fatal("LoadSNMPDevices statements drifted apart")
	}
	if strings.Contains(loadSNMPDevicesLegacySQL, "pilot_") {
		t.Fatal("the legacy statement must not need migration 189")
	}
}

// Once a missing column is seen, the legacy statements are used for
// sysInfoLegacyRetry, then the current ones are tried again.
func TestTemplatesLegacyWindow(t *testing.T) {
	s := &PostgresStore{}
	now := time.Now()
	if s.templatesLegacy(now) {
		t.Fatal("legacy before any failure")
	}
	captureStdout(t, func() { s.noteTemplatesColumnsMissing(now, &pgconn.PgError{Code: "42703"}) })
	if !s.templatesLegacy(now.Add(sysInfoLegacyRetry / 2)) {
		t.Fatal("not legacy right after a missing column")
	}
	if s.templatesLegacy(now.Add(sysInfoLegacyRetry + time.Second)) {
		t.Fatal("still legacy after the retry interval")
	}
	out := captureStdout(t, func() { s.noteTemplatesColumnsPresent(nil) })
	if !strings.Contains(out, "present again") || s.templatesWarnedAt.Load() != 0 {
		t.Fatalf("recovery not announced: %q", out)
	}
}

// RecordProfileMatch / ClearProfileSuggestion against the real schema
// (migration 189 included).
//
//	SYSINFO_TEST_PG=1 go test ./internal/store/ -run ProfileMatchMigratedSchema -v
func TestRecordProfileMatchMigratedSchema(t *testing.T) {
	pool := newMigratedPool(t)
	ctx := context.Background()
	s := &PostgresStore{pool: pool}

	tplA := addTemplate(t, pool, "A", "firewall")
	tplB := addTemplate(t, pool, "B", "")

	type row struct {
		profileID, suggestedID  *uuid.UUID
		source, deviceType      *string
		typeSource              *string
		assignedAt, suggestedAt *time.Time
		xmin                    uint32
	}
	read := func(t *testing.T, id uuid.UUID) row {
		t.Helper()
		var r row
		if err := pool.QueryRow(ctx, `
			SELECT profile_id, profile_suggested_id, profile_source, device_type,
			       device_type_source, profile_assigned_at, profile_suggested_at, xmin::text::bigint
			  FROM devices WHERE id = $1`, id).
			Scan(&r.profileID, &r.suggestedID, &r.source, &r.deviceType,
				&r.typeSource, &r.assignedAt, &r.suggestedAt, &r.xmin); err != nil {
			t.Fatal(err)
		}
		return r
	}
	record := func(t *testing.T, dev, tpl uuid.UUID, deviceType string) {
		t.Helper()
		if err := s.RecordProfileMatch(ctx, dev, tpl, deviceType); err != nil {
			t.Fatalf("RecordProfileMatch: %v", err)
		}
		if s.templatesLegacyUntil.Load() != 0 {
			t.Fatal("fell back to the pre-189 statement on a migrated schema")
		}
	}
	eqID := func(p *uuid.UUID, want uuid.UUID) bool { return p != nil && *p == want }

	t.Run("assigns when unset and takes the type from other", func(t *testing.T) {
		dev, _ := addMigratedDevice(t, pool, "tpl-assign")
		record(t, dev, tplA, "firewall")
		r := read(t, dev)
		if !eqID(r.profileID, tplA) || str(r.source) != "auto" || r.assignedAt == nil {
			t.Fatalf("not assigned: %+v", r)
		}
		if str(r.deviceType) != "firewall" || str(r.typeSource) != "template" {
			t.Fatalf("type = %s (%s), want firewall (template)", str(r.deviceType), str(r.typeSource))
		}
		if r.suggestedID != nil || r.suggestedAt != nil {
			t.Fatalf("suggestion on a fresh assignment: %+v", r)
		}
		// Unchanged on the next poll: no write at all.
		record(t, dev, tplA, "firewall")
		if again := read(t, dev); again.xmin != r.xmin {
			t.Fatal("an unchanged match rewrote the row")
		}
	})

	t.Run("type only replaces other", func(t *testing.T) {
		dev, _ := addMigratedDevice(t, pool, "tpl-typed")
		mustExec(t, pool, `UPDATE devices SET device_type = 'switch' WHERE id = $1`, dev)
		record(t, dev, tplA, "firewall")
		r := read(t, dev)
		if !eqID(r.profileID, tplA) || str(r.deviceType) != "switch" || r.typeSource != nil {
			t.Fatalf("typed device changed: %+v type=%s", r, str(r.deviceType))
		}
	})

	t.Run("unknown or empty type leaves other", func(t *testing.T) {
		dev, _ := addMigratedDevice(t, pool, "tpl-unknown")
		record(t, dev, tplB, "no_such_type")
		r := read(t, dev)
		if !eqID(r.profileID, tplB) || str(r.deviceType) != "other" || r.typeSource != nil {
			t.Fatalf("unknown type: %+v type=%s", r, str(r.deviceType))
		}
	})

	t.Run("never overwrites; suggestion set, kept, replaced, cleared", func(t *testing.T) {
		dev, _ := addMigratedDevice(t, pool, "tpl-suggest")
		mustExec(t, pool, `UPDATE devices SET profile_id = $2, profile_source = 'manual' WHERE id = $1`, dev, tplB)
		record(t, dev, tplA, "firewall")
		r := read(t, dev)
		if !eqID(r.profileID, tplB) || str(r.source) != "manual" || str(r.deviceType) != "other" {
			t.Fatalf("assigned template changed: %+v", r)
		}
		if !eqID(r.suggestedID, tplA) || r.suggestedAt == nil {
			t.Fatalf("suggestion not recorded: %+v", r)
		}
		record(t, dev, tplA, "firewall")
		kept := read(t, dev)
		if kept.xmin != r.xmin || !kept.suggestedAt.Equal(*r.suggestedAt) {
			t.Fatal("an unchanged suggestion was rewritten")
		}
		tplC := addTemplate(t, pool, "C", "")
		record(t, dev, tplC, "")
		if r := read(t, dev); !eqID(r.suggestedID, tplC) || r.suggestedAt == nil {
			t.Fatalf("new suggestion not recorded: %+v", r)
		}
		record(t, dev, tplB, "")
		if r := read(t, dev); !eqID(r.profileID, tplB) || r.suggestedID != nil || r.suggestedAt != nil {
			t.Fatalf("match equal to the assignment did not clear the suggestion: %+v", r)
		}
	})

	t.Run("clear suggestion writes only when one is set", func(t *testing.T) {
		dev, _ := addMigratedDevice(t, pool, "tpl-clear")
		mustExec(t, pool, `UPDATE devices SET profile_id = $2, profile_suggested_id = $3,
		                    profile_suggested_at = now() WHERE id = $1`, dev, tplB, tplA)
		if err := s.ClearProfileSuggestion(ctx, dev); err != nil {
			t.Fatal(err)
		}
		r := read(t, dev)
		if !eqID(r.profileID, tplB) || r.suggestedID != nil || r.suggestedAt != nil {
			t.Fatalf("suggestion not cleared: %+v", r)
		}
		if err := s.ClearProfileSuggestion(ctx, dev); err != nil {
			t.Fatal(err)
		}
		if again := read(t, dev); again.xmin != r.xmin {
			t.Fatal("clearing nothing rewrote the row")
		}
	})

	t.Run("controller-managed children are skipped", func(t *testing.T) {
		parent, _ := addMigratedDevice(t, pool, "tpl-controller")
		child, _ := addMigratedDevice(t, pool, "tpl-child")
		mustExec(t, pool, `UPDATE devices SET managed_by_device_id = $2 WHERE id = $1`, child, parent)
		viaController, _ := addMigratedDevice(t, pool, "tpl-via")
		mustExec(t, pool, `UPDATE devices SET poll_mode = 'via_controller' WHERE id = $1`, viaController)
		for _, dev := range []uuid.UUID{child, viaController} {
			record(t, dev, tplA, "firewall")
			if r := read(t, dev); r.profileID != nil || str(r.deviceType) != "other" {
				t.Fatalf("child classified: %+v", r)
			}
		}
	})

	t.Run("LoadProfiles reads priority and device type", func(t *testing.T) {
		mustExec(t, pool, `UPDATE device_profiles SET match_priority = 250 WHERE id = $1`, tplA)
		profiles, err := s.LoadProfiles(ctx)
		if err != nil {
			t.Fatal(err)
		}
		found := false
		for _, p := range profiles {
			if p.ID == tplA {
				found = true
				if p.MatchPriority != 250 || p.DeviceType != "firewall" {
					t.Fatalf("LoadProfiles = priority %d type %q", p.MatchPriority, p.DeviceType)
				}
			}
		}
		if !found {
			t.Fatal("template not loaded")
		}
		if _, err := s.LoadSNMPDevices(ctx); err != nil {
			t.Fatalf("LoadSNMPDevices: %v", err)
		}
		if s.templatesLegacyUntil.Load() != 0 {
			t.Fatal("LoadProfiles/LoadSNMPDevices fell back on a migrated schema")
		}
	})

	t.Run("pilot devices poll the pilot groups", func(t *testing.T) {
		pilot, _ := addMigratedDevice(t, pool, "tpl-pilot")
		other, _ := addMigratedDevice(t, pool, "tpl-nonpilot")
		mustExec(t, pool, `UPDATE device_profiles SET oid_groups = '[{"name":"published"}]',
		                    pilot_device_ids = ARRAY[$2::uuid], pilot_oid_groups = '[{"name":"pilot"}]'
		                    WHERE id = $1`, tplB, pilot)
		var got []string
		for _, dev := range []uuid.UUID{pilot, other} {
			mustExec(t, pool, `UPDATE devices SET profile_id = $2 WHERE id = $1`, dev, tplB)
			var groups string
			if err := pool.QueryRow(ctx, `SELECT `+strings.TrimSuffix(strings.TrimSpace(snmpDevicesGroupsSQL), ",")+`
				  FROM devices d JOIN device_profiles p ON p.id = d.profile_id WHERE d.id = $1`, dev).
				Scan(&groups); err != nil {
				t.Fatal(err)
			}
			got = append(got, groups)
		}
		if !strings.Contains(got[0], "pilot") || !strings.Contains(got[1], "published") {
			t.Fatalf("groups pilot=%s other=%s", got[0], got[1])
		}
	})
}

func addTemplate(t *testing.T, pool *pgxpool.Pool, name, deviceType string) uuid.UUID {
	t.Helper()
	var id uuid.UUID
	name = "tpl-test-" + name + "-" + uuid.NewString()[:8]
	if err := pool.QueryRow(context.Background(), `
		INSERT INTO device_profiles (name, device_type) VALUES ($1, NULLIF($2, '')) RETURNING id`,
		name, deviceType).Scan(&id); err != nil {
		t.Fatalf("insert template: %v", err)
	}
	t.Cleanup(func() {
		ctx := context.Background()
		_, _ = pool.Exec(ctx, `UPDATE devices SET profile_id = NULL WHERE profile_id = $1`, id)
		if _, err := pool.Exec(ctx, `DELETE FROM device_profiles WHERE id = $1`, id); err != nil {
			t.Logf("cleanup of template %s: %v", name, err)
		}
	})
	return id
}

func mustExec(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(context.Background(), sql, args...); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
}
