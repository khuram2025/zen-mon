package spool

import (
	"bytes"
	"path/filepath"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
)

func TestEnqueueRejectsOversizedPayloadWithoutPruningExistingData(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	if _, err := store.Enqueue("existing", []byte(`"ok"`), 16); err != nil {
		t.Fatal(err)
	}
	oversized := append([]byte{'"'}, bytes.Repeat([]byte("x"), 16)...)
	oversized = append(oversized, '"')
	if _, err := store.Enqueue("oversized", oversized, 16); err == nil {
		t.Fatal("oversized payload was reported as enqueued")
	}

	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].BatchID != "existing" {
		t.Fatalf("oversized enqueue pruned existing data: %#v", records)
	}
}

func TestSpoolPersistsFIFOAndAcknowledgesSelectedRecords(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.db")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	first, err := store.Enqueue("one", []byte(`{"value":1}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Enqueue("two", []byte(`{"value":2}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	store, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 2 || records[0].Key != first || records[1].Key != second {
		t.Fatalf("reopened spool order = %#v", records)
	}
	if err := store.Ack(first); err != nil {
		t.Fatal(err)
	}
	records, err = store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].Key != second {
		t.Fatalf("selective acknowledgement left %#v", records)
	}
}

func TestPeekDueScansPastDeferredFIFOHeadAndPersistsRetryState(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	first, err := store.Enqueue("first", []byte(`{"value":1}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Enqueue("second", []byte(`{"value":2}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	third, err := store.Enqueue("third", []byte(`{"value":3}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	if err := store.Defer(first, now.Add(2*time.Minute), "first is busy"); err != nil {
		t.Fatal(err)
	}
	if err := store.Defer(second, now.Add(time.Minute), "second is busy"); err != nil {
		t.Fatal(err)
	}

	due, err := store.PeekDue(1, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 1 || due[0].Key != third {
		t.Fatalf("PeekDue() = %#v, want third record", due)
	}
	all, err := store.Peek(3)
	if err != nil {
		t.Fatal(err)
	}
	if all[0].Attempts != 1 || all[0].NextAttemptAt == nil || all[0].LastError != "first is busy" {
		t.Fatalf("first retry state was not persisted: %#v", all[0])
	}

	if err := store.Defer(third, now.Add(3*time.Minute), "third is busy"); err != nil {
		t.Fatal(err)
	}
	hasDue, next, err := store.NextAttempt(now)
	if err != nil {
		t.Fatal(err)
	}
	if hasDue || next == nil || !next.Equal(now.Add(time.Minute)) {
		t.Fatalf("NextAttempt() = due %v, next %v", hasDue, next)
	}
	hasDue, next, err = store.NextAttempt(now.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if !hasDue || next != nil {
		t.Fatalf("NextAttempt(at deadline) = due %v, next %v", hasDue, next)
	}
}

func TestQuarantineRemovesActiveDepthAndIsBounded(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	for i := 0; i < QuarantineMaxRecords+1; i++ {
		key, err := store.Enqueue("collision", []byte(`{"value":1}`), 1024*1024)
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Quarantine(key, now, "batch ID collision"); err != nil {
			t.Fatal(err)
		}
	}
	active, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if active.Depth != 0 || active.Bytes != 0 {
		t.Fatalf("active stats include quarantine: %#v", active)
	}
	quarantined, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if quarantined.Depth != QuarantineMaxRecords {
		t.Fatalf("quarantine depth = %d, want bounded %d", quarantined.Depth, QuarantineMaxRecords)
	}
	recent, err := store.PeekQuarantined(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(recent) != 1 || recent[0].QuarantinedAt == nil || recent[0].Attempts != 1 || recent[0].LastError != "batch ID collision" {
		t.Fatalf("quarantine metadata = %#v", recent)
	}
	if len(recent[0].Payload) != 0 || recent[0].PayloadSHA256 == "" || recent[0].OriginalSize == 0 {
		t.Fatalf("quarantine retained payload or omitted safe diagnostics: %#v", recent[0])
	}
}

func TestOpenRepairsCorruptEnvelopeWithoutBlockingLaterBatch(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.db")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	corruptKey, err := store.Enqueue("will-corrupt", []byte(`{"value":1}`), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	validKey, err := store.Enqueue("valid-later", []byte(`{"value":2}`), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketBatches).Put(itob(corruptKey), []byte(`{"broken"`))
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	store, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	due, err := store.PeekDue(10, time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 1 || due[0].Key != validKey || due[0].BatchID != "valid-later" {
		t.Fatalf("due records after repair = %#v", due)
	}
	quarantined, err := store.PeekQuarantined(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(quarantined) != 1 || quarantined[0].Key != corruptKey ||
		quarantined[0].QuarantinedAt == nil || quarantined[0].LastError == "" ||
		len(quarantined[0].Payload) != 0 || quarantined[0].PayloadSHA256 == "" {
		t.Fatalf("corrupt envelope was not retained safely: %#v", quarantined)
	}
}

func TestPruneAppliesConfiguredAgeToQuarantineMetadata(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	key, err := store.Enqueue("old-terminal", []byte(`{"value":1}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Quarantine(key, time.Now().UTC().Add(-2*time.Hour), "terminal"); err != nil {
		t.Fatal(err)
	}
	if err := store.Prune(time.Hour, 1024); err != nil {
		t.Fatal(err)
	}
	stats, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("configured one-hour retention left %d quarantine record(s)", stats.Depth)
	}
}

func TestPostponeExtendsDeadlineWithoutCountingAnotherAttempt(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	key, err := store.Enqueue("retrying", []byte(`{"value":1}`), 1024)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	if err := store.Defer(key, now.Add(time.Minute), "request failed"); err != nil {
		t.Fatal(err)
	}
	if err := store.Postpone(key, now.Add(10*time.Minute)); err != nil {
		t.Fatal(err)
	}
	// A later circuit with a shorter deadline must not shorten the persisted one.
	if err := store.Postpone(key, now.Add(5*time.Minute)); err != nil {
		t.Fatal(err)
	}
	records, err := store.Peek(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].Attempts != 1 || records[0].NextAttemptAt == nil ||
		!records[0].NextAttemptAt.Equal(now.Add(10*time.Minute)) {
		t.Fatalf("postponed record = %#v", records)
	}
}
