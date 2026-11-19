package store

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/zenplus/poller/internal/pinger"
)

var _ pinger.SNMPPollRecorder = (*PostgresStore)(nil)

func TestSNMPPollWriteResult(t *testing.T) {
	t.Cleanup(func() { snmpPollSkipUntil.Store(0) })
	now := time.Unix(1_800_000_000, 0)

	if err := snmpPollWriteResult(nil, now); err != nil {
		t.Fatalf("nil: %v", err)
	}
	// The device was deleted while it was polled.
	if err := snmpPollWriteResult(fmt.Errorf("exec: %w", &pgconn.PgError{Code: "23503"}), now); err != nil {
		t.Fatalf("23503 must be ignored, got %v", err)
	}
	other := errors.New("connection reset")
	if err := snmpPollWriteResult(other, now); !errors.Is(err, other) {
		t.Fatalf("unexpected errors must surface, got %v", err)
	}
	if snmpPollSkipUntil.Load() != 0 {
		t.Fatal("only a missing table pauses the writes")
	}
	// Migration 202 not applied: silent, and paused for the retry window.
	out := captureStdout(t, func() {
		if err := snmpPollWriteResult(&pgconn.PgError{Code: "42P01"}, now); err != nil {
			t.Fatalf("42P01 must be ignored, got %v", err)
		}
	})
	if !strings.Contains(out, "migration 202") {
		t.Fatalf("missing table warning not printed: %q", out)
	}
	if got := snmpPollSkipUntil.Load(); got != now.Add(snmpPollRetry).UnixNano() {
		t.Fatalf("retry window not set: %d", got)
	}
}

func TestRecordSNMPPollSQLKeepsNewestAndStreak(t *testing.T) {
	sql := recordSNMPPollSQL
	for _, want := range []string{
		"ON CONFLICT (device_id) DO UPDATE",
		"COALESCE(s.failing_since, EXCLUDED.failing_since)",
		"WHERE s.last_attempt_at <= EXCLUDED.last_attempt_at",
	} {
		if !strings.Contains(sql, want) {
			t.Fatalf("recordSNMPPollSQL lost %q", want)
		}
	}
}
