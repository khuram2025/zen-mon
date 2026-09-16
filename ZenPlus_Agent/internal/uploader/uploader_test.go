package uploader

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
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

func TestDrainQuarantinesFinalPartialRejection(t *testing.T) {
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
	var quarantineErr *QuarantinedBatchError
	if !errors.As(err, &quarantineErr) || !strings.Contains(err.Error(), "rejected 1") {
		t.Fatalf("Drain() error = %T %v, want terminal quarantine", err, err)
	}
	if acked != 0 {
		t.Fatalf("Drain() acked = %d, want 0", acked)
	}
	stats, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("rejected batch still blocks spool; depth=%d", stats.Depth)
	}
	quarantine, err := store.PeekQuarantined(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(quarantine) != 1 || !strings.Contains(quarantine[0].LastError, "rejected 1") {
		t.Fatalf("rejected batch was not retained for diagnostics: %#v", quarantine)
	}
}

func TestDrainQuarantinesMalformedPayloadAndContinuesFIFOReplay(t *testing.T) {
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
	var quarantineErr *QuarantinedBatchError
	if !errors.As(err, &quarantineErr) {
		t.Fatalf("malformed local payload outcome = %T %v, want quarantine", err, err)
	}
	if acked != 1 || requests != 1 {
		t.Fatalf("drain acked=%d requests=%d; want 1, 1", acked, requests)
	}
	stats, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("spool depth = %d after replay", stats.Depth)
	}
	quarantine, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if quarantine.Depth != 1 {
		t.Fatalf("quarantine depth = %d, want 1", quarantine.Depth)
	}
}

func TestDrainDefersInProgressConflictAndContinuesLaterDueBatch(t *testing.T) {
	var requested []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		requested = append(requested, batch.BatchID)
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID == "busy" {
			w.Header().Set("Retry-After", "7")
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"host-results batch is still being processed; retry later"}`))
			return
		}
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
	enqueueBatch(t, store, "busy")
	enqueueBatch(t, store, "later")
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	up := New(api, store, "agent-1", "server-1")
	up.now = func() time.Time { return now }

	removed, err := up.Drain(context.Background(), 10)
	if err != nil {
		t.Fatalf("Drain() error = %v", err)
	}
	if removed != 1 || strings.Join(requested, ",") != "busy,later" {
		t.Fatalf("Drain() removed=%d requests=%v", removed, requested)
	}
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].BatchID != "busy" || records[0].Attempts != 1 ||
		records[0].NextAttemptAt == nil || !records[0].NextAttemptAt.Equal(now.Add(7*time.Second)) {
		t.Fatalf("deferred record = %#v", records)
	}
}

func TestDrainQuarantinesExactPayloadCollisionAndContinues(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID == "collision" {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"batch_id was already used for a different payload"}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "collision")
	enqueueBatch(t, store, "later")

	removed, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
	var quarantineErr *QuarantinedBatchError
	if !errors.As(err, &quarantineErr) {
		t.Fatalf("Drain() error = %T %v, want QuarantinedBatchError", err, err)
	}
	if removed != 1 || requests != 2 {
		t.Fatalf("Drain() removed=%d requests=%d", removed, requests)
	}
	active, _ := store.Stats()
	quarantined, _ := store.QuarantineStats()
	if active.Depth != 0 || quarantined.Depth != 1 {
		t.Fatalf("active=%#v quarantine=%#v", active, quarantined)
	}
}

func TestDrainHonorsRetryAfterAndStopsControllerWideFailure(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", "11")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"detail":"storage unavailable"}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "first")
	enqueueBatch(t, store, "second")
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	up := New(api, store, "agent-1", "server-1")
	up.now = func() time.Time { return now }

	removed, err := up.Drain(context.Background(), 10)
	var retryErr *RetryableError
	if !errors.As(err, &retryErr) {
		t.Fatalf("Drain() error = %T %v, want RetryableError", err, err)
	}
	if removed != 0 || requests != 1 || retryErr.RetryAfter != 11*time.Second {
		t.Fatalf("Drain() removed=%d requests=%d retry=%s", removed, requests, retryErr.RetryAfter)
	}
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 2 || records[0].Attempts != 1 || records[0].NextAttemptAt == nil ||
		!records[0].NextAttemptAt.Equal(now.Add(11*time.Second)) || records[1].Attempts != 0 {
		t.Fatalf("retry records = %#v", records)
	}
}

func TestDrainDefersPerBatchServiceTimeoutAndContinues(t *testing.T) {
	var requested []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		requested = append(requested, batch.BatchID)
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID == "slow" {
			w.Header().Set("Retry-After", "4")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"detail":"Host telemetry processing timed out; retry the same batch"}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "slow")
	enqueueBatch(t, store, "later")
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	up := New(api, store, "agent-1", "server-1")
	up.now = func() time.Time { return now }

	removed, err := up.Drain(context.Background(), 10)
	if err != nil || removed != 1 || strings.Join(requested, ",") != "slow,later" {
		t.Fatalf("Drain() removed=%d requested=%v err=%v", removed, requested, err)
	}
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].BatchID != "slow" || records[0].NextAttemptAt == nil ||
		!records[0].NextAttemptAt.Equal(now.Add(4*time.Second)) {
		t.Fatalf("deferred slow record = %#v", records)
	}
}

func TestDrainTreatsAcceptedAsNonFinal(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID == "accepted" {
			w.Header().Set("Retry-After", "3")
			w.WriteHeader(http.StatusAccepted)
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "accepted")
	enqueueBatch(t, store, "later")

	removed, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
	if err != nil || removed != 1 || requests != 2 {
		t.Fatalf("Drain() removed=%d requests=%d error=%v", removed, requests, err)
	}
	records, _ := store.Peek(10)
	if len(records) != 1 || records[0].BatchID != "accepted" || records[0].Attempts != 1 {
		t.Fatalf("accepted record was acknowledged: %#v", records)
	}
}

func TestDrainSerializesConcurrentCallers(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requests.Add(1) == 1 {
			close(started)
			<-release
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "first")
	enqueueBatch(t, store, "second")
	up := New(api, store, "agent-1", "server-1")
	done := make(chan error, 2)
	go func() {
		_, err := up.Drain(context.Background(), 1)
		done <- err
	}()
	<-started
	go func() {
		_, err := up.Drain(context.Background(), 1)
		done <- err
	}()
	time.Sleep(50 * time.Millisecond)
	if got := requests.Load(); got != 1 {
		t.Fatalf("concurrent Drain opened %d requests while first was alive", got)
	}
	close(release)
	for i := 0; i < 2; i++ {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	if got := requests.Load(); got != 2 {
		t.Fatalf("serialized requests = %d, want 2", got)
	}
}

func TestDrainSerializesAcrossUploaderReplacement(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requests.Add(1) == 1 {
			close(started)
			<-release
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "single-flight-across-refresh")
	first := New(api, store, "agent-1", "server-1")
	second := New(api, store, "agent-1", "server-1")
	done := make(chan error, 2)
	go func() {
		_, drainErr := first.Drain(context.Background(), 1)
		done <- drainErr
	}()
	<-started
	go func() {
		_, drainErr := second.Drain(context.Background(), 1)
		done <- drainErr
	}()
	time.Sleep(50 * time.Millisecond)
	if got := requests.Load(); got != 1 {
		t.Fatalf("replacement uploader opened %d concurrent requests", got)
	}
	close(release)
	for i := 0; i < 2; i++ {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	if got := requests.Load(); got != 1 {
		t.Fatalf("same durable batch was posted %d times", got)
	}
}

func TestDrainGenerationFencePreventsStaleDurableMutation(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
	}{
		{name: "success ack", status: http.StatusOK, body: `{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`},
		{name: "busy defer", status: http.StatusConflict, body: `{"detail":"host-results batch is still being processed; retry later"}`},
		{name: "terminal quarantine", status: http.StatusUnprocessableEntity, body: `{"code":"invalid_host_results_batch","detail":"invalid"}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			started := make(chan struct{})
			release := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				close(started)
				<-release
				w.Header().Set("Content-Type", "application/json")
				if test.status == http.StatusConflict {
					w.Header().Set("Retry-After", "20")
				}
				w.WriteHeader(test.status)
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()

			store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer store.Close()
			enqueueBatch(t, store, "generation-fenced")
			api, _ := client.New(server.URL, "", true, "agent-1", "secret")
			generation := store.AdvanceReplayGeneration()
			done := make(chan error, 1)
			go func() {
				_, drainErr := New(api, store, "agent-1", "server-1").DrainGeneration(context.Background(), 1, generation)
				done <- drainErr
			}()
			<-started
			store.AdvanceReplayGeneration()
			close(release)
			if err := <-done; !errors.Is(err, spool.ErrStaleReplayGeneration) {
				t.Fatalf("stale DrainGeneration() error = %T %v", err, err)
			}
			records, err := store.Peek(1)
			if err != nil {
				t.Fatal(err)
			}
			quarantined, err := store.QuarantineStats()
			if err != nil {
				t.Fatal(err)
			}
			if len(records) != 1 || records[0].Attempts != 0 || records[0].NextAttemptAt != nil || quarantined.Depth != 0 {
				t.Fatalf("stale generation mutated spool: active=%#v quarantine=%#v", records, quarantined)
			}
		})
	}
}

func TestDrainCancellationDoesNotPersistControllerFailure(t *testing.T) {
	started := make(chan struct{})
	handlerRelease := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-handlerRelease
	}))
	defer server.Close()
	defer close(handlerRelease)
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "shutdown")
	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, drainErr := New(api, store, "agent-1", "server-1").Drain(ctx, 1)
		done <- drainErr
	}()
	<-started
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled Drain() error = %T %v", err, err)
	}
	records, err := store.Peek(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].Attempts != 0 || records[0].NextAttemptAt != nil {
		t.Fatalf("shutdown cancellation mutated retry state: %#v", records)
	}
}

func TestHostResultsRequestTimeoutIsBelowProxyDeadline(t *testing.T) {
	if hostResultsRequestTimeout <= 0 || hostResultsRequestTimeout >= 30*time.Second {
		t.Fatalf("host-results request timeout = %s, want >0 and <30s", hostResultsRequestTimeout)
	}
}

func TestDrainQuarantinesTerminalValidationAndContinues(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID == "invalid" {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"code":"invalid_host_results_batch","detail":"invalid metric"}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "invalid")
	enqueueBatch(t, store, "later")

	removed, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
	var quarantineErr *QuarantinedBatchError
	if !errors.As(err, &quarantineErr) {
		t.Fatalf("Drain() error = %T %v, want quarantine", err, err)
	}
	if removed != 1 || requests != 2 {
		t.Fatalf("Drain() removed=%d requests=%d, want 1 and 2", removed, requests)
	}
	active, _ := store.Stats()
	quarantined, _ := store.QuarantineStats()
	if active.Depth != 0 || quarantined.Depth != 1 {
		t.Fatalf("active=%#v quarantine=%#v", active, quarantined)
	}
}

func TestDrainPreservesBindingErrorAsControllerWideFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"detail":"Agent has no server binding"}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "binding-error")

	removed, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 1)
	if err == nil || removed != 0 {
		t.Fatalf("Drain() = (%d, %v), want preserved controller failure", removed, err)
	}
	active, _ := store.Stats()
	quarantined, _ := store.QuarantineStats()
	if active.Depth != 1 || quarantined.Depth != 0 {
		t.Fatalf("binding error was lost or quarantined: active=%#v quarantine=%#v", active, quarantined)
	}
}

func TestDrainPreservesProtocolWideClientErrors(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
	}{
		{name: "unsupported media type", status: http.StatusUnsupportedMediaType, body: `{"detail":"unsupported content type"}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(test.status)
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()
			api, _ := client.New(server.URL, "", true, "agent-1", "secret")
			store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer store.Close()
			enqueueBatch(t, store, "protocol-wide")

			removed, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 1)
			if err == nil || removed != 0 {
				t.Fatalf("Drain() = (%d, %v), want preserved controller failure", removed, err)
			}
			active, _ := store.Stats()
			quarantined, _ := store.QuarantineStats()
			if active.Depth != 1 || quarantined.Depth != 0 {
				t.Fatalf("protocol error was lost or quarantined: active=%#v quarantine=%#v", active, quarantined)
			}
		})
	}
}

func TestDrainDefersGeneric422AndContinuesLaterBatches(t *testing.T) {
	fixedNow := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	posted := make([]string, 0, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		posted = append(posted, batch.BatchID)
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID == "schema-drift" {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"detail":[{"loc":["body"],"msg":"schema mismatch"}]}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	enqueueBatch(t, store, "schema-drift")
	enqueueBatch(t, store, "current-schema")

	u := New(api, store, "agent-1", "server-1")
	u.now = func() time.Time { return fixedNow }
	removed, err := u.Drain(context.Background(), 2)
	if err != nil || removed != 1 {
		t.Fatalf("Drain() = (%d, %v), want one later batch uploaded", removed, err)
	}
	if !reflect.DeepEqual(posted, []string{"schema-drift", "current-schema"}) {
		t.Fatalf("posted batches = %v", posted)
	}
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].BatchID != "schema-drift" || records[0].Attempts != 1 ||
		records[0].NextAttemptAt == nil || !records[0].NextAttemptAt.After(fixedNow) {
		t.Fatalf("schema-drift batch was not durably deferred: %#v", records)
	}
	quarantined, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if quarantined.Depth != 0 {
		t.Fatalf("generic 422 was quarantined: %#v", quarantined)
	}
}

func TestDrainCircuitBreaksControllerWideGeneric422(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":[{"loc":["body"],"msg":"schema mismatch"}]}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	for i := 0; i < 6; i++ {
		enqueueBatch(t, store, fmt.Sprintf("schema-drift-%d", i))
	}

	removed, err := New(api, store, "agent-1", "server-1").Drain(context.Background(), 6)
	var retryable *RetryableError
	if removed != 0 || !errors.As(err, &retryable) {
		t.Fatalf("Drain() = (%d, %v), want controller-wide retry cooldown", removed, err)
	}
	if retryable.RetryAfter != validationFailureCircuitCooldown {
		t.Fatalf("RetryAfter = %s, want %s", retryable.RetryAfter, validationFailureCircuitCooldown)
	}
	if got := requests.Load(); got != maxDeferredValidationFailuresPerDrain {
		t.Fatalf("request count = %d, want circuit at %d", got, maxDeferredValidationFailuresPerDrain)
	}
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 6 {
		t.Fatalf("active records = %d, want 6 preserved", len(records))
	}
	for i, record := range records {
		wantAttempts := 0
		if i < maxDeferredValidationFailuresPerDrain {
			wantAttempts = 1
		}
		if record.Attempts != wantAttempts {
			t.Fatalf("record %d attempts = %d, want %d", i, record.Attempts, wantAttempts)
		}
	}
}

func TestDrainCircuitCooldownAdvancesPastDeferredHeads(t *testing.T) {
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	posted := make([]string, 0, 4)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		posted = append(posted, batch.BatchID)
		w.Header().Set("Content-Type", "application/json")
		if batch.BatchID != "valid-tail" {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"detail":[{"loc":["body"],"msg":"schema mismatch"}]}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"accepted":1,"rejected":0,"duplicates":0}`))
	}))
	defer server.Close()

	api, _ := client.New(server.URL, "", true, "agent-1", "secret")
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	for i := 0; i < maxDeferredValidationFailuresPerDrain; i++ {
		enqueueBatch(t, store, fmt.Sprintf("schema-drift-%d", i))
	}
	enqueueBatch(t, store, "valid-tail")
	u := New(api, store, "agent-1", "server-1")
	u.now = func() time.Time { return now }

	removed, err := u.Drain(context.Background(), 4)
	var retryable *RetryableError
	if removed != 0 || !errors.As(err, &retryable) || retryable.RetryAfter != validationFailureCircuitCooldown {
		t.Fatalf("first Drain() = (%d, %v), want five-minute circuit cooldown", removed, err)
	}
	now = now.Add(validationFailureCircuitCooldown)
	removed, err = u.Drain(context.Background(), 4)
	if err != nil || removed != 1 {
		t.Fatalf("second Drain() = (%d, %v), want valid tail uploaded", removed, err)
	}
	wantPosted := []string{"schema-drift-0", "schema-drift-1", "schema-drift-2", "valid-tail"}
	if !reflect.DeepEqual(posted, wantPosted) {
		t.Fatalf("posted batches = %v, want %v", posted, wantPosted)
	}
	records, err := store.Peek(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != maxDeferredValidationFailuresPerDrain {
		t.Fatalf("active records after tail progress = %d", len(records))
	}
}

func TestParseRetryAfterIsBoundedWithoutOverflowOrHotLoop(t *testing.T) {
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	if delay, ok := parseRetryAfter("0", now); !ok || delay != time.Second {
		t.Fatalf("Retry-After 0 = %s, %v", delay, ok)
	}
	if delay, ok := parseRetryAfter("999999999999999999999999999999", now); !ok || delay != 24*time.Hour {
		t.Fatalf("huge Retry-After = %s, %v", delay, ok)
	}
	headerDate := now.Add(90 * time.Second).Format(http.TimeFormat)
	if delay, ok := parseRetryAfter(headerDate, now); !ok || delay != 90*time.Second {
		t.Fatalf("date Retry-After = %s, %v", delay, ok)
	}
	if _, ok := parseRetryAfter("not-a-delay", now); ok {
		t.Fatal("invalid Retry-After was accepted")
	}
}

func TestControllerErrorInfoSupportsLegacyAndMachineCodes(t *testing.T) {
	tests := []struct {
		name       string
		body       string
		wantCode   string
		wantDetail string
	}{
		{
			name:       "legacy FastAPI detail",
			body:       `{"detail":"host-results batch is still being processed; retry later"}`,
			wantDetail: "host-results batch is still being processed; retry later",
		},
		{
			name:       "top-level machine code",
			body:       `{"code":"host_results_in_progress","detail":"retry later"}`,
			wantCode:   "host_results_in_progress",
			wantDetail: "retry later",
		},
		{
			name:       "nested machine code",
			body:       `{"detail":{"code":"batch_id_payload_collision","message":"different payload"}}`,
			wantCode:   "batch_id_payload_collision",
			wantDetail: "different payload",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			code, detail, ok := controllerErrorInfo(&client.StatusError{
				Code: http.StatusConflict, Body: test.body,
			})
			if !ok || code != test.wantCode || detail != test.wantDetail {
				t.Fatalf("controllerErrorInfo() = (%q, %q, %v), want (%q, %q, true)", code, detail, ok, test.wantCode, test.wantDetail)
			}
		})
	}
}

func enqueueBatch(t *testing.T, store *spool.Store, batchID string) {
	t.Helper()
	batch := model.Batch{
		AgentID: "agent-1", ServerID: "server-1", BatchID: batchID,
		Metrics: []model.Metric{{Kind: "cpu", Timestamp: time.Now().UTC(), Data: map[string]any{"usage": 10}}},
	}
	payload, err := json.Marshal(batch)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Enqueue(batch.BatchID, payload, 1024*1024); err != nil {
		t.Fatal(err)
	}
}
