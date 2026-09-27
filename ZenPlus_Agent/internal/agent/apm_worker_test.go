package agent

import (
	"context"
	"sync"
	"testing"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
)

type blockingReconciler struct {
	mu      sync.Mutex
	started chan struct{}
	release chan struct{}
	seen    []int
}

func (r *blockingReconciler) Reconcile(ctx context.Context, cfg config.Config, _, _ string, _ *client.Client) {
	r.mu.Lock()
	r.seen = append(r.seen, cfg.CollectIntervalSeconds)
	first := len(r.seen) == 1
	r.mu.Unlock()
	if first {
		close(r.started)
		select {
		case <-r.release:
		case <-ctx.Done():
		}
	}
}

func (r *blockingReconciler) calls() []int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]int(nil), r.seen...)
}

func TestAPMWorkerNeverBlocksAndCoalesces(t *testing.T) {
	r := &blockingReconciler{started: make(chan struct{}), release: make(chan struct{})}
	w := startAPMWorker(context.Background(), r)
	defer w.Close()

	request := func(n int) apmReconcileRequest {
		cfg := config.Default()
		cfg.CollectIntervalSeconds = n
		return apmReconcileRequest{cfg: cfg}
	}
	w.Submit(request(1))
	<-r.started

	// The first reconcile is stuck (for example on an unreachable appliance).
	// Submitting must still return immediately, and only the newest queued
	// request may run afterwards.
	done := make(chan struct{})
	go func() {
		for n := 2; n <= 50; n++ {
			w.Submit(request(n))
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Submit blocked behind a slow reconcile")
	}
	close(r.release)

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if calls := r.calls(); len(calls) == 2 {
			if calls[1] != 50 {
				t.Fatalf("worker ran stale request %d, want the newest (50)", calls[1])
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("reconcile calls = %v, want [1 50]", r.calls())
}

func TestAPMWorkerCloseCancelsInFlightReconcile(t *testing.T) {
	r := &blockingReconciler{started: make(chan struct{}), release: make(chan struct{})}
	w := startAPMWorker(context.Background(), r)
	w.Submit(apmReconcileRequest{cfg: config.Default()})
	<-r.started
	closed := make(chan struct{})
	go func() {
		w.Close()
		close(closed)
	}()
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("Close did not cancel the in-flight reconcile")
	}
}
