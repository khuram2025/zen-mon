package spool

import (
	"bytes"
	"path/filepath"
	"testing"
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
