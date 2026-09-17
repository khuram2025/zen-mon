package uploader_test

import (
	"encoding/binary"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"

	"zenplus-agent/internal/spool"
)

func TestQuarantineStoresMetadataOnlyAndHonorsConfiguredAge(t *testing.T) {
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	payload := []byte(`{"value":1}`)
	key, err := store.Enqueue("terminal", payload, 1024)
	if err != nil {
		t.Fatal(err)
	}
	quarantinedAt := time.Now().UTC().Add(-2 * time.Hour)
	if err := store.Quarantine(key, quarantinedAt, "terminal validation failure"); err != nil {
		t.Fatal(err)
	}
	records, err := store.PeekQuarantined(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || len(records[0].Payload) != 0 || records[0].PayloadSHA256 == "" ||
		records[0].OriginalSize != int64(len(payload)) {
		t.Fatalf("quarantine did not retain metadata-only diagnostics: %#v", records)
	}
	stats, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Bytes <= 0 || stats.OriginalBytes != int64(len(payload)) {
		t.Fatalf("quarantine byte accounting is ambiguous: %#v", stats)
	}
	if err := store.Prune(time.Hour, 1024); err != nil {
		t.Fatal(err)
	}
	stats, err = store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("configured retention left %d old quarantine record(s)", stats.Depth)
	}
}

func TestSpoolOpenIsolatesCorruptEnvelopeAndKeepsLaterWorkDue(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.db")
	db, err := bolt.Open(path, 0o600, nil)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	validPayload := json.RawMessage(`{"value":2}`)
	validEnvelope, err := json.Marshal(spool.Record{
		Key: 2, BatchID: "valid-later", CreatedAt: now,
		Size: int64(len(validPayload)), Payload: validPayload,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Update(func(tx *bolt.Tx) error {
		batches, createErr := tx.CreateBucketIfNotExists([]byte("batches"))
		if createErr != nil {
			return createErr
		}
		if _, createErr = tx.CreateBucketIfNotExists([]byte("meta")); createErr != nil {
			return createErr
		}
		if _, createErr = tx.CreateBucketIfNotExists([]byte("quarantine")); createErr != nil {
			return createErr
		}
		if putErr := batches.Put(uint64Key(1), []byte(`{"broken"`)); putErr != nil {
			return putErr
		}
		return batches.Put(uint64Key(2), validEnvelope)
	}); err != nil {
		_ = db.Close()
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	store, err := spool.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	due, err := store.PeekDue(10, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 1 || due[0].BatchID != "valid-later" {
		t.Fatalf("later valid work was blocked after repair: %#v", due)
	}
	quarantined, err := store.PeekQuarantined(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(quarantined) != 1 || quarantined[0].PayloadSHA256 == "" || len(quarantined[0].Payload) != 0 {
		t.Fatalf("corrupt envelope was not isolated safely: %#v", quarantined)
	}
}

func uint64Key(value uint64) []byte {
	key := make([]byte, 8)
	binary.BigEndian.PutUint64(key, value)
	return key
}
