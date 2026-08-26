package uploader

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/spool"
)

func TestSpoolRejectsOversizedPayloadWithoutPruningExistingData(t *testing.T) {
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
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

func TestDrainAcknowledgesFinalPartialRejection(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/results/host" {
			t.Fatalf("unexpected request path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"accepted":0,"rejected":1,"errors":["invalid sample"]}`))
	}))
	defer server.Close()

	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	batch := model.Batch{
		AgentID: "agent-1", ServerID: "server-1", BatchID: "batch-1",
		Metrics: []model.Metric{{Kind: "cpu", Timestamp: time.Now().UTC(), Data: map[string]any{"usage": 10}}},
	}
	payload, _ := json.Marshal(batch)
	if _, err := store.Enqueue(batch.BatchID, payload, 1024*1024); err != nil {
		t.Fatal(err)
	}

	acked, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
	if err == nil || !strings.Contains(err.Error(), "rejected 1") {
		t.Fatalf("Drain() error = %v", err)
	}
	if acked != 1 {
		t.Fatalf("Drain() acked = %d, want 1", acked)
	}
	stats, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("rejected batch still blocks spool; depth=%d", stats.Depth)
	}
}

func TestDrainDropsMalformedPayloadAndContinuesFIFOReplay(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.Enqueue("malformed", []byte(`{"agent_id":42}`), 1024*1024); err != nil {
		t.Fatal(err)
	}
	batch := model.Batch{
		AgentID: "agent-1", ServerID: "server-1", BatchID: "valid",
		Metrics: []model.Metric{{Kind: "cpu", Timestamp: time.Now().UTC(), Data: map[string]any{"usage": 10}}},
	}
	payload, err := json.Marshal(batch)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Enqueue(batch.BatchID, payload, 1024*1024); err != nil {
		t.Fatal(err)
	}

	acked, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
	if err != nil {
		t.Fatalf("malformed local payload blocked later telemetry: %v", err)
	}
	if acked != 2 || requests != 1 {
		t.Fatalf("drain acked=%d requests=%d; want 2, 1", acked, requests)
	}
	stats, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("spool depth = %d after replay", stats.Depth)
	}
}
