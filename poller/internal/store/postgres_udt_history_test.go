package store

import (
	"context"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/zenplus/poller/internal/checker/snmp"
)

func TestIPHistoryPreservesObservationsWithoutChurn(t *testing.T) {
	dsn := os.Getenv("UDT_TEST_DSN")
	if dsn == "" {
		t.Skip("UDT_TEST_DSN not set")
	}
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close(ctx)
	tx, err := conn.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	// Temporary tables shadow production tables; all test writes roll back.
	_, err = tx.Exec(ctx, `
        CREATE TEMP TABLE udt_endpoints (id uuid PRIMARY KEY, ip_address inet, updated_at timestamptz, last_seen timestamptz) ON COMMIT DROP;
        CREATE TEMP TABLE udt_ip_history (id bigserial, endpoint_id uuid, ip inet, source text, reporting_device_id uuid,
            active boolean, first_seen timestamptz, last_seen timestamptz) ON COMMIT DROP;
        CREATE UNIQUE INDEX ON udt_ip_history(endpoint_id, ip) WHERE active;
        CREATE TEMP TABLE udt_ip_evidence(endpoint_id uuid, ip inet, reporting_device_id uuid,
            if_index integer, source text, first_seen timestamptz, last_seen timestamptz,
            observation_count bigint, recent_sightings timestamptz[],
            PRIMARY KEY(endpoint_id,ip,reporting_device_id,if_index,source)) ON COMMIT DROP;
    `)
	if err != nil {
		t.Fatal(err)
	}
	a, b, router1, router2 := uuid.New(), uuid.New(), uuid.New(), uuid.New()
	_, err = tx.Exec(ctx, "INSERT INTO udt_endpoints(id) VALUES ($1), ($2)", a, b)
	if err != nil {
		t.Fatal(err)
	}
	ids := map[string]uuid.UUID{"00:11:22:33:44:55": a, "00:11:22:33:44:66": b}
	first := []snmp.ArpEntry{
		{MAC: "00:11:22:33:44:55", IP: "192.0.2.10", Source: "arp"},
		{MAC: "00:11:22:33:44:55", IP: "2001:db8::1", Source: "nd", IsIPv6: true},
		{MAC: "00:11:22:33:44:55", IP: "2001:0db8:0:0:0:0:0:1", Source: "nd", IsIPv6: true},
		{MAC: "00:11:22:33:44:55", IP: "invalid", Source: "arp"},
	}
	other := []snmp.ArpEntry{{MAC: "00:11:22:33:44:66", IP: "192.0.2.10", Source: "arp"}}
	for i := 0; i < 5; i++ {
		if err := upsertIPHistory(ctx, tx, router1, first, ids); err != nil {
			t.Fatal(err)
		}
		if err := upsertIPHistory(ctx, tx, router2, other, ids); err != nil {
			t.Fatal(err)
		}
	}
	var count, active int
	if err := tx.QueryRow(ctx, "SELECT count(*), count(*) FILTER (WHERE active) FROM udt_ip_history").Scan(&count, &active); err != nil {
		t.Fatal(err)
	}
	if count != 3 || active != 3 {
		t.Fatalf("cross-reporter churn: rows=%d active=%d, want 3/3", count, active)
	}
	var primary string
	if err := tx.QueryRow(ctx, "SELECT host(ip_address) FROM udt_endpoints WHERE id=$1", a).Scan(&primary); err != nil {
		t.Fatal(err)
	}
	if primary != "192.0.2.10" {
		t.Fatalf("IPv4 should be primary: %s", primary)
	}
	// Another IPv4 on a different reporter and an IPv6-only poll must not flip primary.
	extra := []snmp.ArpEntry{{MAC: "00:11:22:33:44:55", IP: "192.0.2.20", Source: "arp"}}
	if err := upsertIPHistory(ctx, tx, router2, extra, ids); err != nil {
		t.Fatal(err)
	}
	if err := upsertIPHistory(ctx, tx, router1, first[1:2], ids); err != nil {
		t.Fatal(err)
	}
	if err := tx.QueryRow(ctx, "SELECT host(ip_address) FROM udt_endpoints WHERE id=$1", a).Scan(&primary); err != nil {
		t.Fatal(err)
	}
	if primary != "192.0.2.10" {
		t.Fatalf("primary flipped: %s", primary)
	}
	// A genuine return after expiration creates a period and preserves the old one.
	_, err = tx.Exec(ctx, "UPDATE udt_ip_history SET active=false, first_seen=NOW()-INTERVAL '4 days', last_seen=NOW()-INTERVAL '2 days' WHERE endpoint_id=$1 AND ip='192.0.2.10'", a)
	if err != nil {
		t.Fatal(err)
	}
	if err := upsertIPHistory(ctx, tx, router1, first[:1], ids); err != nil {
		t.Fatal(err)
	}
	if err := tx.QueryRow(ctx, "SELECT count(*),count(*) FILTER (WHERE active) FROM udt_ip_history WHERE endpoint_id=$1 AND ip='192.0.2.10'", a).Scan(&count, &active); err != nil {
		t.Fatal(err)
	}
	if count != 2 || active != 1 {
		t.Fatalf("lost historical period: %d/%d", count, active)
	}

	// Retries/duplicates in the same poll transaction must count only once.
	var observations int
	if err := tx.QueryRow(ctx, "SELECT observation_count FROM udt_ip_evidence WHERE endpoint_id=$1 AND ip='192.0.2.10' AND reporting_device_id=$2", a, router1).Scan(&observations); err != nil {
		t.Fatal(err)
	}
	if observations != 1 {
		t.Fatalf("duplicate poll counted %d times", observations)
	}
	// Preserve independently reported interfaces and routers for the same binding.
	contexts := []snmp.ArpEntry{
		{MAC: "00:11:22:33:44:55", IP: "192.0.2.10", Source: "arp", IfIndex: 7},
		{MAC: "00:11:22:33:44:55", IP: "192.0.2.10", Source: "arp", IfIndex: 8},
		{MAC: "00:11:22:33:44:55", IP: "192.0.2.10", Source: "arp", IfIndex: 8},
	}
	if err := upsertIPHistory(ctx, tx, router2, contexts, ids); err != nil {
		t.Fatal(err)
	}
	if err := upsertIPHistory(ctx, tx, router2, nil, ids); err != nil {
		t.Fatal(err)
	}
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM udt_ip_evidence WHERE endpoint_id=$1 AND ip='192.0.2.10'", a).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 3 {
		t.Fatalf("reporter/interface evidence collapsed: %d", count)
	}

	// A replacement primary needs repeat evidence; legacy row counts cannot qualify.
	if _, err := tx.Exec(ctx, "UPDATE udt_ip_history SET active=false WHERE endpoint_id=$1 AND ip='192.0.2.10'", a); err != nil {
		t.Fatal(err)
	}
	for poll := 2; poll <= 3; poll++ {
		// NOW() is transaction-stable; age the prior poll to simulate distinct successful snapshots.
		if _, err := tx.Exec(ctx, `UPDATE udt_ip_evidence SET last_seen=NOW()-INTERVAL '1 minute',
		    recent_sightings=ARRAY(SELECT t-INTERVAL '1 minute' FROM unnest(recent_sightings) t)
		    WHERE endpoint_id=$1 AND ip='192.0.2.20'`, a); err != nil {
			t.Fatal(err)
		}
		if err := upsertIPHistory(ctx, tx, router2, extra, ids); err != nil {
			t.Fatal(err)
		}
		if err := tx.QueryRow(ctx, "SELECT host(ip_address) FROM udt_endpoints WHERE id=$1", a).Scan(&primary); err != nil {
			t.Fatal(err)
		}
		want := "192.0.2.10"
		if poll == 3 {
			want = "192.0.2.20"
		}
		if primary != want {
			t.Fatalf("primary after %d polls: %s, want %s", poll, primary, want)
		}
	}
	// Old sightings do not qualify a new change.
	if _, err := tx.Exec(ctx, `UPDATE udt_endpoints SET ip_address='192.0.2.99' WHERE id=$1;
	`, a); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `UPDATE udt_ip_evidence SET last_seen=NOW()-INTERVAL '2 days',
	    recent_sightings=ARRAY[NOW()-INTERVAL '2 days',NOW()-INTERVAL '3 days',NOW()-INTERVAL '4 days'] WHERE endpoint_id=$1`, a); err != nil {
		t.Fatal(err)
	}
	if err := upsertIPHistory(ctx, tx, router2, extra, ids); err != nil {
		t.Fatal(err)
	}
	if err := tx.QueryRow(ctx, "SELECT host(ip_address) FROM udt_endpoints WHERE id=$1", a).Scan(&primary); err != nil {
		t.Fatal(err)
	}
	if primary != "192.0.2.99" {
		t.Fatalf("stale sightings qualified a change: %s", primary)
	}
}
