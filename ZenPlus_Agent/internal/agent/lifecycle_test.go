package agent

import (
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/spool"
)

func TestShutdownEvidenceIsSpooledAsItsOwnBatch(t *testing.T) {
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.EnqueueWithEventCursors("collection", []byte(`{"metrics":[]}`), 0, map[string]string{"q": "cursor"}); err != nil {
		t.Fatal(err)
	}
	enrollment := enroll.Result{Identity: identity.Identity{AgentID: "agent-1", ServerID: "server-1"}}
	metric := model.Metric{Kind: "event_detail", Timestamp: time.Now().UTC(), Data: map[string]any{"event_id": 13}}
	if err := enqueueMetrics(config.Default(), store, enrollment, []model.Metric{metric}); err != nil {
		t.Fatal(err)
	}
	records, err := store.Peek(10)
	if err != nil || len(records) != 2 {
		t.Fatalf("records=%v err=%v", records, err)
	}
	var batch model.Batch
	if err := json.Unmarshal(records[1].Payload, &batch); err != nil {
		t.Fatal(err)
	}
	if batch.AgentID != "agent-1" || batch.ServerID != "server-1" || len(batch.Metrics) != 1 ||
		batch.Metrics[0].Kind != "event_detail" || batch.SequenceEnd != batch.SequenceStart || batch.Health.Status != "ok" {
		t.Fatalf("batch = %+v", batch)
	}
	// Spooling shutdown evidence must not move the event-log checkpoints.
	cursors, err := store.EventCursors()
	if err != nil || cursors["q"] != "cursor" {
		t.Fatalf("cursors=%v err=%v", cursors, err)
	}
}

func TestOnlyLinuxAdvertisesSystemdServices(t *testing.T) {
	if got := platformCapabilities("linux"); len(got) != 1 || got[0] != systemdServicesCapability {
		t.Fatalf("linux capabilities = %v", got)
	}
	for _, goos := range []string{"windows", "darwin"} {
		if got := platformCapabilities(goos); len(got) != 0 {
			t.Fatalf("%s capabilities = %v", goos, got)
		}
	}
}
