package store

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/checker/snmp"
)

// Uses a private Unix-socket cluster; never accepts a production DSN.
func TestPostgresInterfaceReindexPreservesPolicy(t *testing.T) {
	if os.Getenv("ZENPLUS_NETWORK_INTEGRATION") != "1" {
		t.Skip("requires disposable PostgreSQL fixture")
	}
	socket := filepath.Clean(os.Getenv("ZENPLUS_NETWORK_PG_SOCKET"))
	parent := filepath.Dir(socket)
	if filepath.Dir(parent) != "/tmp" || !strings.HasPrefix(filepath.Base(parent), "zenplus-network-integration-") || filepath.Base(socket) != "pgsocket" {
		t.Fatal("integration database must use the private fixture socket")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	cfg, err := pgxpool.ParseConfig("postgres://network_fixture@localhost:15432/zenplus?sslmode=disable")
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.Host = socket
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	id := uuid.New()
	if _, err = pool.Exec(ctx, "INSERT INTO devices (id,hostname) VALUES ($1,'interface-fixture')", id); err != nil {
		t.Fatal(err)
	}
	defer pool.Exec(context.Background(), "DELETE FROM devices WHERE id=$1", id)
	store := &PostgresStore{pool: pool}
	old := []snmp.Interface{{IfIndex: 1, IfName: "uplink", AdminStatus: "up", OperStatus: "up"},
		{IfIndex: 2, IfName: "access", AdminStatus: "up", OperStatus: "up"}}
	if err = store.UpsertInterfaces(ctx, id, old); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, "UPDATE device_interfaces SET monitored=false,configured_speed_bps=100000000 WHERE device_id=$1 AND if_index=1", id); err != nil {
		t.Fatal(err)
	}
	current := []snmp.Interface{{IfIndex: 2, IfName: "uplink", AdminStatus: "up", OperStatus: "up"},
		{IfIndex: 1, IfName: "access", AdminStatus: "up", OperStatus: "up"},
		{IfIndex: 3, IfName: "new", AdminStatus: "up", OperStatus: "up"}}
	var wg sync.WaitGroup
	errors := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); errors <- store.UpsertInterfaces(ctx, id, current) }()
	}
	wg.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	rows, err := pool.Query(ctx, "SELECT if_index,monitored,configured_speed_bps FROM device_interfaces WHERE device_id=$1 ORDER BY if_index", id)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var idx int
		var monitored bool
		var speed *int64
		if err = rows.Scan(&idx, &monitored, &speed); err != nil {
			t.Fatal(err)
		}
		count++
		if idx == 2 {
			if monitored || speed == nil || *speed != 100000000 {
				t.Fatal("uplink policy lost after reindex")
			}
		} else if !monitored || speed != nil {
			t.Fatal("uplink policy leaked to a different port")
		}
	}
	if rows.Err() != nil {
		t.Fatal(rows.Err())
	}
	if count != 3 {
		t.Fatalf("expected 3 interfaces, got %d", count)
	}
}
