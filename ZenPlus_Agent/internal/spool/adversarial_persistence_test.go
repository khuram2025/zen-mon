package spool_test

import (
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"zenplus-agent/internal/spool"
)

func TestDeferredHeadDoesNotBlockDueRecordsAndSurvivesRestart(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "spool.db")
	store, err := spool.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.Close() }()

	head, err := store.Enqueue("head", []byte("{\"batch\":\"head\"}"), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Enqueue("second", []byte("{\"batch\":\"second\"}"), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	third, err := store.Enqueue("third", []byte("{\"batch\":\"third\"}"), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}

	now := time.Now().UTC()
	retryAt := now.Add(time.Hour)
	if err := store.Defer(head, retryAt, "still processing"); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	store, err = spool.Open(path)
	if err != nil {
		t.Fatal(err)
	}

	due, err := store.PeekDue(2, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 2 || due[0].Key != second || due[1].Key != third {
		t.Fatalf("PeekDue() = %#v, want later due records %d and %d", due, second, third)
	}

	records, err := store.Peek(3)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 3 {
		t.Fatalf("Peek() returned %d records, want 3", len(records))
	}
	gotHead := records[0]
	if gotHead.Key != head || gotHead.Attempts != 1 || gotHead.LastError != "still processing" {
		t.Fatalf("persisted deferred record = %#v", gotHead)
	}
	if gotHead.NextAttemptAt == nil || !gotHead.NextAttemptAt.Equal(retryAt) {
		t.Fatalf("persisted retry deadline = %v, want %v", gotHead.NextAttemptAt, retryAt)
	}

	dueNow, next, err := store.NextAttempt(now)
	if err != nil {
		t.Fatal(err)
	}
	if !dueNow || next != nil {
		t.Fatalf("NextAttempt() with later due work = (%v, %v), want (true, nil)", dueNow, next)
	}
	if err := store.Ack(second, third); err != nil {
		t.Fatal(err)
	}
	dueNow, next, err = store.NextAttempt(now)
	if err != nil {
		t.Fatal(err)
	}
	if dueNow || next == nil || !next.Equal(retryAt) {
		t.Fatalf("NextAttempt() with only deferred work = (%v, %v), want (false, %v)", dueNow, next, retryAt)
	}

	due, err = store.PeekDue(1, retryAt.Add(time.Nanosecond))
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 1 || due[0].Key != head {
		t.Fatalf("deferred record did not become due: %#v", due)
	}
}

func TestQuarantineIsBoundedSeparateAndPersistent(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "spool.db")
	store, err := spool.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.Close() }()

	activeKey, err := store.Enqueue("active", []byte("{\"batch\":\"active\"}"), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	for i := 0; i < spool.QuarantineMaxRecords+2; i++ {
		batchID := fmt.Sprintf("collision-%03d", i)
		key, enqueueErr := store.Enqueue(batchID, []byte(fmt.Sprintf("%q", batchID)), 1024*1024)
		if enqueueErr != nil {
			t.Fatal(enqueueErr)
		}
		if quarantineErr := store.Quarantine(key, now, "batch_id collision"); quarantineErr != nil {
			t.Fatal(quarantineErr)
		}
	}

	active, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if active.Depth != 1 {
		t.Fatalf("active Stats().Depth = %d, want 1", active.Depth)
	}
	records, err := store.Peek(2)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].Key != activeKey {
		t.Fatalf("active queue contains quarantined records: %#v", records)
	}

	quarantined, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if quarantined.Depth != spool.QuarantineMaxRecords {
		t.Fatalf("QuarantineStats().Depth = %d, want %d", quarantined.Depth, spool.QuarantineMaxRecords)
	}
	recent, err := store.PeekQuarantined(spool.QuarantineMaxRecords + 10)
	if err != nil {
		t.Fatal(err)
	}
	seen := make(map[string]bool, len(recent))
	for _, rec := range recent {
		seen[rec.BatchID] = true
		if rec.QuarantinedAt == nil || rec.Attempts != 1 || rec.LastError != "batch_id collision" {
			t.Fatalf("quarantined metadata = %#v", rec)
		}
	}
	if seen["collision-000"] || seen["collision-001"] || !seen["collision-002"] {
		t.Fatalf("quarantine did not evict oldest records: seen=%v", seen)
	}

	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	store, err = spool.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	quarantined, err = store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if quarantined.Depth != spool.QuarantineMaxRecords {
		t.Fatalf("reopened quarantine depth = %d, want %d", quarantined.Depth, spool.QuarantineMaxRecords)
	}
}

func TestQuarantinePrunesExpiredDiagnostics(t *testing.T) {
	t.Parallel()

	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	now := time.Now().UTC()
	oldKey, err := store.Enqueue("old", []byte(`"old"`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Quarantine(oldKey, now.Add(-spool.QuarantineMaxAge-time.Second), "old collision"); err != nil {
		t.Fatal(err)
	}
	newKey, err := store.Enqueue("new", []byte(`"new"`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Quarantine(newKey, now, "new collision"); err != nil {
		t.Fatal(err)
	}

	records, err := store.PeekQuarantined(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].BatchID != "new" {
		t.Fatalf("expired quarantine records retained: %#v", records)
	}
}
