package pinger

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
)

type confirmationRunner struct {
	calls   atomic.Int32
	values  []bool
	release <-chan struct{}
}

func (r *confirmationRunner) CheckOne(ctx context.Context, sc *checker.ServiceCheck, _ string) *checker.ServiceCheckResult {
	index := int(r.calls.Add(1)) - 1
	if r.release != nil {
		select {
		case <-r.release:
		case <-ctx.Done():
			return nil
		}
	}
	if index >= len(r.values) {
		index = len(r.values) - 1
	}
	return &checker.ServiceCheckResult{ServiceCheckID: sc.ID, CheckType: "http", IsUp: r.values[index], Timestamp: time.Now().UTC()}
}

type confirmationWriter struct {
	fakeServiceMetricWriter
	samples atomic.Int32
}

func (w *confirmationWriter) WriteServiceResult(*checker.ServiceCheckResult) { w.samples.Add(1) }

type confirmationLoader struct {
	fakeServiceLoader
	id uuid.UUID
}

func (l *confirmationLoader) LoadActiveMaintenanceCheckIDs(context.Context) (map[uuid.UUID]struct{}, error) {
	// Suppress real alert HTTP calls from unit tests; confirmation still runs.
	return map[uuid.UUID]struct{}{l.id: {}}, nil
}

func TestConfirmationCadenceUsesCompletionAndConfiguredGaps(t *testing.T) {
	started := time.Now().Add(-time.Minute)
	finished := started.Add(15 * time.Second)
	sc := &checker.ServiceCheck{Status: "up", CheckInterval: 60 * time.Second, RetryDelay: 10 * time.Second, DownCheckInterval: 30 * time.Second}
	for _, tc := range []struct {
		state    string
		failures int
		interval time.Duration
	}{
		{"up", 0, 60 * time.Second}, {"up", 1, 10 * time.Second}, {"unknown", 2, 10 * time.Second}, {"down", 3, 30 * time.Second},
	} {
		sc.Status, sc.DownCount = tc.state, tc.failures
		base, interval := serviceCadence(sc, started, finished)
		if !base.Equal(finished) || interval != tc.interval {
			t.Fatalf("%+v: base=%v interval=%v", tc, base, interval)
		}
		if due(finished.Add(interval-time.Millisecond), base, interval) {
			t.Fatal("attempt ran before gap elapsed")
		}
		if !due(finished.Add(interval), base, interval) {
			t.Fatal("attempt not due after gap")
		}
	}
}

func TestFastConfirmationRetainsProbesAndResetsOnSuccess(t *testing.T) {
	for _, values := range [][]bool{{false, true, false}, {false, false, false, true}} {
		e := testEngine()
		id := uuid.New()
		e.serviceChecks[id] = &checker.ServiceCheck{ID: id, Enabled: true, CheckType: "http", Status: "up", RetryCount: 3, RetryDelay: 10 * time.Second, CheckInterval: time.Minute}
		runner := &confirmationRunner{values: values}
		writer := &confirmationWriter{}
		e.checker = runner
		e.svcWriter = writer
		e.svcLoader = &confirmationLoader{id: id}
		for i, up := range values {
			e.mu.Lock()
			if e.serviceFinished != nil {
				e.serviceFinished[id] = time.Now().Add(-time.Hour)
			}
			e.mu.Unlock()
			e.runServiceCheckCycle(context.Background())
			e.serviceWorkers.Wait()
			sc := e.serviceChecks[id]
			if i == 2 && !values[1] && sc.Status != "down" {
				t.Fatal("third consecutive failure did not confirm Down")
			}
			if values[1] && sc.Status != "up" {
				t.Fatal("isolated failures changed confirmed availability")
			}
			if up && (sc.Status != "up" || sc.DownCount != 0) {
				t.Fatal("success did not recover and reset")
			}
		}
		if int(writer.samples.Load()) != len(values) {
			t.Fatal("diagnostic attempt was discarded")
		}
	}
}

func TestServiceSchedulerPreventsOverlapAndCancelsWithoutFailure(t *testing.T) {
	e := testEngine()
	id := uuid.New()
	release := make(chan struct{})
	e.serviceChecks[id] = &checker.ServiceCheck{ID: id, Enabled: true, CheckType: "http", Status: "up", RetryCount: 3}
	runner := &confirmationRunner{values: []bool{false}, release: release}
	writer := &confirmationWriter{}
	e.checker = runner
	e.svcWriter = writer
	ctx, cancel := context.WithCancel(context.Background())
	e.runServiceCheckCycle(ctx)
	for i := 0; i < 20; i++ {
		e.runServiceCheckCycle(ctx)
	}
	cancel()
	e.serviceWorkers.Wait()
	if runner.calls.Load() != 1 || writer.samples.Load() != 0 || e.serviceChecks[id].DownCount != 0 {
		t.Fatal("overlap or cancellation recorded as a failure")
	}
}
