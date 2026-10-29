package store

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/checker"
)

func TestServiceTransitionAtomic(t *testing.T) {
	dsn := os.Getenv("ZENPLUS_SERVICE_INBOX_TEST_DSN")
	if dsn == "" {
		t.Skip("requires private Unix-socket PostgreSQL fixture")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.ConnConfig.Host, "/tmp/") || !strings.HasPrefix(cfg.ConnConfig.Database, "zp_t_") || cfg.ConnConfig.Port == 5432 || cfg.ConnConfig.Password != "" {
		t.Fatal("only the disposable PostgreSQL fixture is allowed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	id, err := uuid.Parse(os.Getenv("ZENPLUS_SERVICE_INBOX_TEST_ID"))
	if err != nil {
		t.Fatal(err)
	}
	s := &PostgresStore{pool: pool}
	now := time.Now().UTC()
	sc := &checker.ServiceCheck{ID: id, Name: "fixture", CheckType: "http", Status: "down"}
	r := &checker.ServiceCheckResult{ServiceCheckID: id, IsUp: true, Timestamp: now}
	body := []byte(fmt.Sprintf(`{"service_check_id":%q,"check_name":"fixture","check_type":"http","old_status":"down","new_status":"up"}`, id.String()))
	committed, err := s.RecordServiceTransition(ctx, sc, r, "down", "up", body)
	if os.Getenv("ZENPLUS_SERVICE_INBOX_EXPECT_FAILURE") == "1" {
		if err == nil || committed {
			t.Fatal("queue failure did not abort state update")
		}
		return
	}
	if err != nil || !committed {
		t.Fatalf("transition failed: %v", err)
	}
	committed, err = s.RecordServiceTransition(ctx, sc, r, "down", "up", body)
	if err != nil || committed {
		t.Fatal("repeated old state must not enqueue twice")
	}
}
