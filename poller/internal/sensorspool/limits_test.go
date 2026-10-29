package sensorspool

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
)

func TestEndpointLimitsPreserveInflightAcrossRestart(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "wal")
	s, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 501; i++ {
		ok, err := s.Enqueue(Item{ID: fmt.Sprint(i), Path: "/events", Payload: json.RawMessage(`{"value":1}`)})
		if err != nil || !ok {
			t.Fatalf("enqueue %d: %v %v", i, ok, err)
		}
	}
	// Simulate an old binary which sent 500 events and lost the ACK. New
	// limits must not silently replace that possibly committed request.
	old, err := s.NextBatch(500)
	if err != nil || len(old.Items) != 500 {
		t.Fatalf("old batch: %v", err)
	}
	s, err = Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	retry, err := s.NextBatchWithLimits(500, 1024, map[string]int{"/events": 200})
	if err != nil || retry.Key != old.Key || len(retry.Items) != 500 {
		t.Fatalf("retry changed: %v", err)
	}
	if err := s.Ack(retry); err != nil {
		t.Fatal(err)
	}
	next, err := s.NextBatchWithLimits(500, 1024, map[string]int{"/events": 200})
	if err != nil || len(next.Items) != 1 {
		t.Fatalf("remaining batch: %v", err)
	}
}

func TestNewBatchesRespectEndpointAndEncodedByteLimits(t *testing.T) {
	for _, tc := range []struct {
		name, path            string
		count, bytes, wantMax int
	}{
		{"events", "/events", 501, 0, 200},
		{"results", "/results", 501, 0, 500},
		{"bytes_with_html_escaping", "/events", 20, 2048, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s, err := Open(filepath.Join(t.TempDir(), "wal"), Options{})
			if err != nil {
				t.Fatal(err)
			}
			payload, _ := json.Marshal(map[string]string{"value": strings.Repeat("<>&", 30)})
			for i := 0; i < tc.count; i++ {
				ok, err := s.Enqueue(Item{ID: fmt.Sprint(i), Path: tc.path, Payload: payload})
				if err != nil || !ok {
					t.Fatalf("enqueue: %v %v", ok, err)
				}
			}
			total := 0
			for {
				b, err := s.NextBatchWithLimits(500, tc.bytes, map[string]int{"/events": 200})
				if err != nil {
					t.Fatal(err)
				}
				if b == nil {
					break
				}
				if len(b.Items) > tc.wantMax {
					t.Fatalf("batch has %d items", len(b.Items))
				}
				encoded, _ := json.Marshal(b.Items)
				if tc.bytes > 0 && len(encoded) > tc.bytes {
					t.Fatalf("encoded %d > %d", len(encoded), tc.bytes)
				}
				total += len(b.Items)
				if err := s.Ack(b); err != nil {
					t.Fatal(err)
				}
			}
			if total != tc.count || s.Stats().Dropped != 0 {
				t.Fatalf("lost records: total=%d stats=%+v", total, s.Stats())
			}
		})
	}
}

func TestOversizeSingletonDoesNotStallSpool(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "wal"), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := s.Enqueue(Item{Path: "/events", Payload: json.RawMessage(`{"value":"large"}`)}); !ok || err != nil {
		t.Fatal(err)
	}
	b, err := s.NextBatchWithLimits(500, 1, nil)
	if err != nil || b == nil || len(b.Items) != 1 {
		t.Fatalf("oversize hidden: %v", err)
	}
	if err := s.DropInflight(b); err != nil {
		t.Fatal(err)
	}
	if s.Stats().Dropped != 1 || s.Stats().Depth != 0 {
		t.Fatalf("missing drop accounting: %+v", s.Stats())
	}
}
