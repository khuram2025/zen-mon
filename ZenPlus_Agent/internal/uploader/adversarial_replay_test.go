package uploader_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

func TestAmbiguousResponseLossRetainsAndReplaysSameBatchIdentity(t *testing.T) {
	t.Parallel()

	var (
		mu       sync.Mutex
		received []model.Batch
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var batch model.Batch
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Errorf("decode request: %v", err)
			return
		}
		mu.Lock()
		received = append(received, batch)
		attempt := len(received)
		mu.Unlock()

		if attempt == 1 {
			hijacker, ok := w.(http.Hijacker)
			if !ok {
				t.Error("test server does not support connection hijacking")
				return
			}
			conn, _, err := hijacker.Hijack()
			if err != nil {
				t.Errorf("hijack response: %v", err)
				return
			}
			_ = conn.Close()
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("{\"ok\":true,\"accepted\":0,\"rejected\":0,\"duplicates\":1}"))
	}))
	defer server.Close()

	store := openTestStore(t)
	original := enqueueTestBatch(t, store, "ambiguous")
	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	up := uploader.New(api, store, "agent-1", "server-1")

	removed, err := up.Drain(context.Background(), 1)
	var retryErr *uploader.RetryableError
	if !errors.As(err, &retryErr) {
		t.Fatalf("first Drain() error = %T %v, want RetryableError", err, err)
	}
	if removed != 0 {
		t.Fatalf("first Drain() removed = %d, want 0", removed)
	}
	records, err := store.Peek(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].BatchID != original.BatchID || records[0].Attempts != 1 {
		t.Fatalf("ambiguous request was not retained: %#v", records)
	}

	// Make the persisted record due without sleeping through production retry
	// delays. Defer is itself durable, so this also exercises retry metadata.
	if err := store.Defer(records[0].Key, time.Now().Add(-time.Second), records[0].LastError); err != nil {
		t.Fatal(err)
	}
	removed, err = up.Drain(context.Background(), 1)
	if err != nil || removed != 1 {
		t.Fatalf("second Drain() = (%d, %v), want (1, nil)", removed, err)
	}

	mu.Lock()
	got := append([]model.Batch(nil), received...)
	mu.Unlock()
	if len(got) != 2 {
		t.Fatalf("server received %d attempts, want 2", len(got))
	}
	if got[0].BatchID != original.BatchID || got[1].BatchID != original.BatchID {
		t.Fatalf("batch identity changed across replay: %q then %q", got[0].BatchID, got[1].BatchID)
	}
	if !got[0].CollectedAt.Equal(got[1].CollectedAt) || !reflect.DeepEqual(got[0].Metrics, got[1].Metrics) {
		t.Fatalf("immutable payload changed across replay:\nfirst=%#v\nsecond=%#v", got[0], got[1])
	}
	stats, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 0 {
		t.Fatalf("duplicate success did not acknowledge replay; depth=%d", stats.Depth)
	}
}

func TestUnknownConflictIsPreservedAndStopsCycle(t *testing.T) {
	t.Parallel()

	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", "2")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte("{\"detail\":\"some future conflict meaning\"}"))
	}))
	defer server.Close()

	store := openTestStore(t)
	enqueueTestBatch(t, store, "unknown-conflict")
	enqueueTestBatch(t, store, "later")
	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}

	removed, err := uploader.New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
	var retryErr *uploader.RetryableError
	if !errors.As(err, &retryErr) {
		t.Fatalf("Drain() error = %T %v, want RetryableError", err, err)
	}
	if removed != 0 || requests.Load() != 1 {
		t.Fatalf("Drain() removed=%d requests=%d, want 0 and 1", removed, requests.Load())
	}
	active, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	quarantined, err := store.QuarantineStats()
	if err != nil {
		t.Fatal(err)
	}
	if active.Depth != 2 || quarantined.Depth != 0 {
		t.Fatalf("unknown conflict was lost or quarantined: active=%#v quarantine=%#v", active, quarantined)
	}
}

func TestControllerWideBackpressureStopsBeforeNextBatch(t *testing.T) {
	for _, status := range []int{http.StatusTooManyRequests, http.StatusServiceUnavailable} {
		status := status
		t.Run(http.StatusText(status), func(t *testing.T) {
			t.Parallel()

			var requests atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				attempt := requests.Add(1)
				w.Header().Set("Content-Type", "application/json")
				if attempt == 1 {
					w.Header().Set("Retry-After", "9")
					w.WriteHeader(status)
					_, _ = w.Write([]byte("{\"detail\":\"controller backpressure\"}"))
					return
				}
				_, _ = w.Write([]byte("{\"ok\":true,\"accepted\":1,\"rejected\":0,\"duplicates\":0}"))
			}))
			defer server.Close()

			store := openTestStore(t)
			enqueueTestBatch(t, store, "backpressured")
			enqueueTestBatch(t, store, "must-not-send")
			api, err := client.New(server.URL, "", true, "agent-1", "secret")
			if err != nil {
				t.Fatal(err)
			}
			removed, err := uploader.New(api, store, "agent-1", "server-1").Drain(context.Background(), 10)
			var retryErr *uploader.RetryableError
			if !errors.As(err, &retryErr) {
				t.Fatalf("Drain() error = %T %v, want RetryableError", err, err)
			}
			if removed != 0 || requests.Load() != 1 || retryErr.RetryAfter != 9*time.Second {
				t.Fatalf(
					"Drain() removed=%d requests=%d retry=%s, want 0, 1, 9s",
					removed,
					requests.Load(),
					retryErr.RetryAfter,
				)
			}
			records, err := store.Peek(2)
			if err != nil {
				t.Fatal(err)
			}
			if len(records) != 2 || records[0].Attempts != 1 || records[1].Attempts != 0 {
				t.Fatalf("controller-wide backpressure did not stop cycle: %#v", records)
			}
		})
	}
}

func TestHTTPDateRetryAfterIsPersisted(t *testing.T) {
	t.Parallel()

	retryAt := time.Now().UTC().Add(2 * time.Minute).Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", retryAt.Format(http.TimeFormat))
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte("{\"detail\":\"host-results batch is still being processed; retry later\"}"))
	}))
	defer server.Close()

	store := openTestStore(t)
	enqueueTestBatch(t, store, "busy")
	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	removed, err := uploader.New(api, store, "agent-1", "server-1").Drain(context.Background(), 1)
	if err != nil || removed != 0 {
		t.Fatalf("Drain() = (%d, %v), want (0, nil)", removed, err)
	}
	records, err := store.Peek(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].NextAttemptAt == nil || !records[0].NextAttemptAt.Equal(retryAt) {
		t.Fatalf("HTTP-date Retry-After persisted as %#v, want %v", records, retryAt)
	}
}

func TestHugeRetryAfterCannotOverflowIntoHotRetry(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", "9223372036854775807")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("{\"detail\":\"storage unavailable\"}"))
	}))
	defer server.Close()

	store := openTestStore(t)
	enqueueTestBatch(t, store, "huge-retry")
	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	before := time.Now().UTC()
	removed, err := uploader.New(api, store, "agent-1", "server-1").Drain(context.Background(), 1)
	var retryErr *uploader.RetryableError
	if !errors.As(err, &retryErr) {
		t.Fatalf("Drain() error = %T %v, want RetryableError", err, err)
	}
	if removed != 0 || retryErr.RetryAfter <= time.Minute || retryErr.RetryAfter > 24*time.Hour {
		t.Fatalf("overflowing Retry-After became unsafe delay %s", retryErr.RetryAfter)
	}
	records, err := store.Peek(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || records[0].NextAttemptAt == nil ||
		records[0].NextAttemptAt.Before(before.Add(time.Minute)) {
		t.Fatalf("overflowing Retry-After produced hot retry: %#v", records)
	}
}

func TestDrainLimitBoundsCatchUpRequests(t *testing.T) {
	t.Parallel()

	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("{\"ok\":true,\"accepted\":1,\"rejected\":0,\"duplicates\":0}"))
	}))
	defer server.Close()

	store := openTestStore(t)
	for i := 0; i < 25; i++ {
		enqueueTestBatch(t, store, "catchup-"+time.Unix(int64(i), 0).UTC().Format("150405"))
	}
	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	removed, err := uploader.New(api, store, "agent-1", "server-1").Drain(context.Background(), 7)
	if err != nil {
		t.Fatal(err)
	}
	if removed != 7 || requests.Load() != 7 {
		t.Fatalf("bounded Drain() removed=%d requests=%d, want 7 and 7", removed, requests.Load())
	}
	stats, err := store.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.Depth != 18 {
		t.Fatalf("remaining queue depth = %d, want 18", stats.Depth)
	}
}

func TestConcurrentDrainsCannotReplaySameBatch(t *testing.T) {
	t.Parallel()

	started := make(chan struct{})
	release := make(chan struct{})
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requests.Add(1) == 1 {
			close(started)
			<-release
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("{\"ok\":true,\"accepted\":1,\"rejected\":0,\"duplicates\":0}"))
	}))
	defer server.Close()

	store := openTestStore(t)
	enqueueTestBatch(t, store, "single-flight")
	api, err := client.New(server.URL, "", true, "agent-1", "secret")
	if err != nil {
		t.Fatal(err)
	}
	up := uploader.New(api, store, "agent-1", "server-1")
	type result struct {
		removed int
		err     error
	}
	results := make(chan result, 2)
	go func() {
		n, drainErr := up.Drain(context.Background(), 1)
		results <- result{removed: n, err: drainErr}
	}()
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("first Drain did not reach controller")
	}
	go func() {
		n, drainErr := up.Drain(context.Background(), 1)
		results <- result{removed: n, err: drainErr}
	}()
	time.Sleep(50 * time.Millisecond)
	if got := requests.Load(); got != 1 {
		t.Fatalf("concurrent Drain opened %d requests while first was alive", got)
	}
	close(release)

	totalRemoved := 0
	for i := 0; i < 2; i++ {
		select {
		case got := <-results:
			if got.err != nil {
				t.Fatal(got.err)
			}
			totalRemoved += got.removed
		case <-time.After(2 * time.Second):
			t.Fatal("serialized Drain did not finish")
		}
	}
	if requests.Load() != 1 || totalRemoved != 1 {
		t.Fatalf("single batch replayed: requests=%d totalRemoved=%d", requests.Load(), totalRemoved)
	}
}

func openTestStore(t *testing.T) *spool.Store {
	t.Helper()
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func enqueueTestBatch(t *testing.T, store *spool.Store, batchID string) model.Batch {
	t.Helper()
	batch := model.Batch{
		AgentID: "agent-1", ServerID: "server-1", BatchID: batchID,
		CollectedAt: time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC),
		Metrics: []model.Metric{{
			Kind: "cpu", Timestamp: time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC),
			Data: map[string]any{"usage": 10.0},
		}},
	}
	payload, err := json.Marshal(batch)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Enqueue(batch.BatchID, payload, 1024*1024); err != nil {
		t.Fatal(err)
	}
	return batch
}
