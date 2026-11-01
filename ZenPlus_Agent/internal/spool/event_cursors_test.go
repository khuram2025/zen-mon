package spool

import (
	"path/filepath"
	"testing"
)

func TestEventCheckpointAndBatchPersistTogether(t *testing.T) {
	path := filepath.Join(t.TempDir(), "events.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.EnqueueWithEventCursors("events", []byte(`{"event":1}`), 1024, map[string]string{"system": "bookmark-1"}); err != nil {
		t.Fatal(err)
	}
	s.Close()
	s, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	cursors, err := s.EventCursors()
	if err != nil {
		t.Fatal(err)
	}
	records, err := s.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if cursors["system"] != "bookmark-1" || len(records) != 1 {
		t.Fatal("atomic event state lost on restart")
	}
	if _, err = s.EnqueueWithEventCursors("overflow", []byte(`{"event":2}`), 12, map[string]string{"system": "bookmark-2"}); err == nil {
		t.Fatal("overflow should apply backpressure")
	}
	cursors, _ = s.EventCursors()
	records, _ = s.Peek(10)
	if cursors["system"] != "bookmark-1" || len(records) != 1 || records[0].BatchID != "events" {
		t.Fatal("full spool pruned data or advanced checkpoint")
	}
}
