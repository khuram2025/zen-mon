package store

import (
	"bytes"
	"context"
	"errors"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/checker/snmp"
	"github.com/zenplus/poller/internal/pinger"
	"net"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type stagedBlock struct {
	token   string
	payload []byte
}

func TestTrapJournalPrivatePostgres(t *testing.T) {
	dsn := os.Getenv("ZENPLUS_TRAP_JOURNAL_TEST_DSN")
	if dsn == "" {
		t.Skip("private PostgreSQL fixture required")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.ConnConfig.Host, "/tmp/") || !strings.HasPrefix(cfg.ConnConfig.Database, "zp_t_") || cfg.ConnConfig.Port == 5432 || cfg.ConnConfig.Password != "" {
		t.Fatal("disposable fixture required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	store := &ClickHouseStore{journal: &PostgresStore{pool: pool}}
	if err = store.EnableDurableTrapAlerts(); err != nil {
		t.Fatal(err)
	}
	trap := snmp.TrapRecord{EventID: uuid.New(), Timestamp: time.Now(), SourceIP: net.ParseIP("2001:db8::5"), TrapOID: "1.3.6.1.6.3.1.1.5.3"}
	counts := func(want int) {
		t.Helper()
		var analytics, incidents, receipts int
		err := pool.QueryRow(ctx, `SELECT
			(SELECT count(*) FROM host_ingest_batches WHERE table_name='snmp_traps'),
			(SELECT count(*) FROM snmp_trap_incident_inbox WHERE event_id=$1),
			(SELECT count(*) FROM host_ingest_receipts WHERE table_name='snmp_traps')`, trap.EventID).Scan(&analytics, &incidents, &receipts)
		if err != nil || analytics != want || incidents != want || receipts != want {
			t.Fatalf("unexpected trap admission counts %d/%d/%d: %v", analytics, incidents, receipts, err)
		}
	}
	if _, err = pool.Exec(ctx, `UPDATE host_ingest_capacity SET trap_bytes=33554432`); err != nil {
		t.Fatal(err)
	}
	if err = store.insertTrapBatch(ctx, []snmp.TrapRecord{trap}); err != journalRefusal("trap incident inbox capacity exhausted") {
		t.Fatalf("expected trap capacity refusal, got %v", err)
	}
	counts(0)
	if _, err = pool.Exec(ctx, `UPDATE host_ingest_capacity SET trap_bytes=0`); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err = store.insertTrapBatch(ctx, []snmp.TrapRecord{trap}); err != nil {
			t.Fatal(err)
		}
	}
	counts(1)
}

type failingStager struct {
	mu       sync.Mutex
	calls    []stagedBlock
	accepted chan stagedBlock
}

func (f *failingStager) StageAnalytics(_ context.Context, _ string, token string, payload []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	b := stagedBlock{token, append([]byte(nil), payload...)}
	f.calls = append(f.calls, b)
	if len(f.calls) <= 2 {
		return errors.New("private fixture unavailable")
	}
	f.accepted <- b
	return nil
}
func TestJournalRetryDoesNotRegroupOrDiscard(t *testing.T) {
	f := &failingStager{accepted: make(chan stagedBlock, 5)}
	s := &ClickHouseStore{journal: f, batchSize: 1, flushInterval: 10 * time.Millisecond, buffer: make(chan *pinger.PingResult, 2), done: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	finished := make(chan struct{})
	go func() { s.RunBatchWriter(ctx); close(finished) }()
	stamp := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	s.WriteResult(&pinger.PingResult{DeviceID: uuid.New(), Timestamp: stamp, IPAddress: net.IPv4(192, 0, 2, 1)})
	s.WriteResult(&pinger.PingResult{DeviceID: uuid.New(), Timestamp: stamp, IPAddress: net.IPv4(192, 0, 2, 2)})
	for i := 0; i < 2; i++ {
		select {
		case <-f.accepted:
		case <-time.After(3 * time.Second):
			t.Fatal("pending work was discarded")
		}
	}
	cancel()
	<-finished
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.calls) != 4 {
		t.Fatalf("expected retries and two accepted blocks, got %d", len(f.calls))
	}
	for _, b := range f.calls[1:3] {
		if b.token != f.calls[0].token || !bytes.Equal(b.payload, f.calls[0].payload) {
			t.Fatal("failed batch changed identity or membership")
		}
	}
	if f.calls[3].token == f.calls[0].token {
		t.Fatal("new sample reused old batch identity")
	}
}

func TestAnalyticsJournalPrivatePostgres(t *testing.T) {
	dsn := os.Getenv("ZENPLUS_ANALYTICS_JOURNAL_TEST_DSN")
	if dsn == "" {
		t.Skip("private PostgreSQL fixture required")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.ConnConfig.Host, "/tmp/") || !strings.HasPrefix(cfg.ConnConfig.Database, "zp_t_") || cfg.ConnConfig.Port == 5432 || cfg.ConnConfig.Password != "" {
		t.Fatal("disposable fixture required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	s := &ClickHouseStore{journal: &PostgresStore{pool: pool}}
	b, err := s.prepareBatch(ctx, "INSERT INTO ping_metrics (device_id,timestamp,u,ip,optional,text)")
	if err != nil {
		t.Fatal(err)
	}
	var optional *uint16
	id := uuid.MustParse("00000000-0000-0000-0000-000000000123")
	if err = b.Append(&id, time.Date(2026, 11, 1, 0, 0, 0, 123000000, time.UTC), uint64(18446744073709551615), net.IPv4(192, 0, 2, 9), optional, "quotes '\" and unicode \u0627"); err != nil {
		t.Fatal(err)
	}
	err = b.Send()
	if os.Getenv("ZENPLUS_ANALYTICS_JOURNAL_EXPECT_FAILURE") == "1" {
		if err == nil {
			t.Fatal("expected transactional failure")
		}
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	if err = b.Send(); err != nil {
		t.Fatal(err)
	}
}
