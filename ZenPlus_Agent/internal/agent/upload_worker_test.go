package agent

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

type scriptedDrainer struct {
	mu        sync.Mutex
	calls     int
	active    int
	maxActive int
	limits    []int
	drain     func(context.Context, int, int) (int, error)
}

func (d *scriptedDrainer) Drain(ctx context.Context, limit int) (int, error) {
	d.mu.Lock()
	d.calls++
	call := d.calls
	d.active++
	if d.active > d.maxActive {
		d.maxActive = d.active
	}
	d.limits = append(d.limits, limit)
	d.mu.Unlock()

	defer func() {
		d.mu.Lock()
		d.active--
		d.mu.Unlock()
	}()
	if d.drain == nil {
		return 0, nil
	}
	return d.drain(ctx, limit, call)
}

func (d *scriptedDrainer) snapshot() (calls, maxActive int, limits []int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls, d.maxActive, append([]int(nil), d.limits...)
}

type workerTestClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *workerTestClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *workerTestClock) Set(now time.Time) {
	c.mu.Lock()
	c.now = now
	c.mu.Unlock()
}

func TestUploadWorkerContinuouslyCatchesUpToEmptyInBoundedDrains(t *testing.T) {
	store := openUploadWorkerStore(t)
	for i := 0; i < 25; i++ {
		enqueueUploadWorkerRecord(t, store, fmt.Sprintf("batch-%02d", i))
	}

	drainer := &scriptedDrainer{}
	drainer.drain = func(_ context.Context, limit, _ int) (int, error) {
		records, err := store.PeekDue(limit, time.Now().UTC())
		if err != nil {
			return 0, err
		}
		keys := make([]uint64, 0, len(records))
		for _, record := range records {
			keys = append(keys, record.Key)
		}
		if err := store.Ack(keys...); err != nil {
			return 0, err
		}
		return len(keys), nil
	}

	worker := newUploadWorker(context.Background(), store)
	defer worker.Close()
	worker.catchUpDelay = func() time.Duration { return 100 * time.Millisecond }
	worker.SetUploader(drainer)
	worker.SetEnabled(true)

	wantUploaded := []int{10, 10, 5}
	wantDepth := []int{15, 5, 0}
	for i := range wantUploaded {
		outcome := awaitUploadOutcome(t, worker)
		if outcome.Err != nil {
			t.Fatalf("outcome %d error = %v", i, outcome.Err)
		}
		if outcome.Uploaded != wantUploaded[i] || outcome.Queue.Depth != wantDepth[i] {
			t.Fatalf(
				"outcome %d = uploaded %d depth %d, want %d and %d",
				i,
				outcome.Uploaded,
				outcome.Queue.Depth,
				wantUploaded[i],
				wantDepth[i],
			)
		}
	}

	calls, maxActive, limits := drainer.snapshot()
	if calls != 3 || maxActive != 1 {
		t.Fatalf("drainer calls=%d maxActive=%d, want 3 and 1", calls, maxActive)
	}
	for i, limit := range limits {
		if limit != uploadDrainLimit {
			t.Fatalf("drain %d limit = %d, want %d", i, limit, uploadDrainLimit)
		}
	}
	time.Sleep(150 * time.Millisecond)
	if callsAfter, _, _ := drainer.snapshot(); callsAfter != 3 {
		t.Fatalf("empty spool triggered an extra drain: calls=%d", callsAfter)
	}
}

func TestUploadWorkerCoalescesRepeatedWakeWhileDrainIsActive(t *testing.T) {
	store := openUploadWorkerStore(t)
	started := make(chan struct{})
	release := make(chan struct{})
	drainer := &scriptedDrainer{}
	drainer.drain = func(ctx context.Context, _ int, call int) (int, error) {
		if call == 1 {
			close(started)
			select {
			case <-release:
			case <-ctx.Done():
				return 0, ctx.Err()
			}
		}
		return 0, nil
	}

	worker := newUploadWorker(context.Background(), store)
	defer worker.Close()
	worker.SetUploader(drainer)
	worker.SetEnabled(true)
	awaitSignal(t, started, "first drain start")

	for i := 0; i < 100; i++ {
		worker.Wake()
	}
	_, maxActive, _ := drainer.snapshot()
	if maxActive != 1 {
		t.Fatalf("concurrent drains while first was active: maxActive=%d", maxActive)
	}
	close(release)
	_ = awaitUploadOutcome(t, worker)
	_ = awaitUploadOutcome(t, worker)

	time.Sleep(150 * time.Millisecond)
	calls, maxActive, _ := drainer.snapshot()
	if calls != 2 || maxActive != 1 {
		t.Fatalf("100 wakes produced calls=%d maxActive=%d, want 2 and 1", calls, maxActive)
	}
}

func TestUploadWorkerRetryablePauseCannotBeDefeatedByWake(t *testing.T) {
	store := openUploadWorkerStore(t)
	key := enqueueUploadWorkerRecord(t, store, "retryable")
	base := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	clock := &workerTestClock{now: base}
	drainer := &scriptedDrainer{}
	drainer.drain = func(_ context.Context, _ int, call int) (int, error) {
		if call == 1 {
			return 0, &uploader.RetryableError{
				Err:        errors.New("controller unavailable"),
				RetryAfter: 5 * time.Second,
			}
		}
		if err := store.Ack(key); err != nil {
			return 0, err
		}
		return 1, nil
	}

	worker := newUploadWorker(context.Background(), store)
	defer worker.Close()
	worker.now = clock.Now
	worker.SetUploader(drainer)
	worker.SetEnabled(true)

	first := awaitUploadOutcome(t, worker)
	var retryErr *uploader.RetryableError
	if !errors.As(first.Err, &retryErr) {
		t.Fatalf("first outcome error = %T %v, want RetryableError", first.Err, first.Err)
	}
	wantNext := base.Add(5 * time.Second)
	if first.NextUploadAttempt == nil || !first.NextUploadAttempt.Equal(wantNext) {
		t.Fatalf("retry deadline = %v, want %v", first.NextUploadAttempt, wantNext)
	}

	for i := 0; i < 100; i++ {
		worker.Wake()
	}
	time.Sleep(150 * time.Millisecond)
	if calls, _, _ := drainer.snapshot(); calls != 1 {
		t.Fatalf("Wake bypassed Retry-After; calls=%d", calls)
	}

	clock.Set(base.Add(6 * time.Second))
	worker.Wake()
	second := awaitUploadOutcome(t, worker)
	if second.Err != nil || second.Uploaded != 1 || second.Queue.Depth != 0 {
		t.Fatalf("post-deadline outcome = %#v", second)
	}
}

func TestUploadWorkerReplacementFencesOldRetryOutcome(t *testing.T) {
	store := openUploadWorkerStore(t)
	key := enqueueUploadWorkerRecord(t, store, "replacement")
	started := make(chan struct{})
	release := make(chan struct{})
	oldDrainer := &scriptedDrainer{}
	oldDrainer.drain = func(ctx context.Context, _ int, _ int) (int, error) {
		close(started)
		select {
		case <-release:
			return 0, &uploader.RetryableError{Err: errors.New("old client failed"), RetryAfter: time.Hour}
		case <-ctx.Done():
			return 0, ctx.Err()
		}
	}
	newDrainer := &scriptedDrainer{}
	newDrainer.drain = func(_ context.Context, _ int, _ int) (int, error) {
		if err := store.Ack(key); err != nil {
			return 0, err
		}
		return 1, nil
	}

	worker := newUploadWorker(context.Background(), store)
	defer worker.Close()
	worker.catchUpDelay = func() time.Duration { return 100 * time.Millisecond }
	oldGeneration := worker.SetUploader(oldDrainer)
	worker.SetEnabled(true)
	awaitSignal(t, started, "old uploader drain")
	newGeneration := worker.SetUploader(newDrainer)
	worker.Wake()
	close(release)

	stale := awaitUploadOutcome(t, worker)
	if stale.Generation != oldGeneration || stale.Err == nil {
		t.Fatalf("stale outcome = %#v, want generation %d with error", stale, oldGeneration)
	}
	fresh := awaitUploadOutcome(t, worker)
	if fresh.Generation != newGeneration || fresh.Err != nil || fresh.Uploaded != 1 || fresh.Queue.Depth != 0 {
		t.Fatalf("replacement outcome = %#v, want successful generation %d", fresh, newGeneration)
	}
}

func TestUploadWorkerReplacementClearsAlreadyScheduledOldCooldown(t *testing.T) {
	store := openUploadWorkerStore(t)
	key := enqueueUploadWorkerRecord(t, store, "cooldown-replacement")
	oldDrainer := &scriptedDrainer{}
	oldDrainer.drain = func(context.Context, int, int) (int, error) {
		return 0, &uploader.RetryableError{Err: errors.New("old backpressure"), RetryAfter: time.Hour}
	}
	newDrainer := &scriptedDrainer{}
	newDrainer.drain = func(context.Context, int, int) (int, error) {
		if err := store.Ack(key); err != nil {
			return 0, err
		}
		return 1, nil
	}

	worker := newUploadWorker(context.Background(), store)
	defer worker.Close()
	oldGeneration := worker.SetUploader(oldDrainer)
	worker.SetEnabled(true)
	first := awaitUploadOutcome(t, worker)
	if first.Generation != oldGeneration || first.NextUploadAttempt == nil {
		t.Fatalf("old cooldown outcome = %#v", first)
	}
	newGeneration := worker.SetUploader(newDrainer)
	second := awaitUploadOutcome(t, worker)
	if second.Generation != newGeneration || second.Err != nil || second.Uploaded != 1 {
		t.Fatalf("replacement remained gated by old cooldown: %#v", second)
	}
}

func TestUploadWorkerContinuesAfterNonRetryableAndSchedulesNextDue(t *testing.T) {
	store := openUploadWorkerStore(t)
	key := enqueueUploadWorkerRecord(t, store, "later-due")
	base := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	deferredUntil := base.Add(30 * time.Second)
	clock := &workerTestClock{now: base}
	drainer := &scriptedDrainer{}
	drainer.drain = func(_ context.Context, _ int, call int) (int, error) {
		switch call {
		case 1:
			return 0, &uploader.QuarantinedBatchError{BatchIDs: []string{"collision"}}
		case 2:
			if err := store.Defer(key, deferredUntil, "still processing"); err != nil {
				return 0, err
			}
			return 0, nil
		default:
			if err := store.Ack(key); err != nil {
				return 0, err
			}
			return 1, nil
		}
	}

	worker := newUploadWorker(context.Background(), store)
	defer worker.Close()
	worker.now = clock.Now
	worker.catchUpDelay = func() time.Duration { return 100 * time.Millisecond }
	worker.SetUploader(drainer)
	worker.SetEnabled(true)

	first := awaitUploadOutcome(t, worker)
	var quarantineErr *uploader.QuarantinedBatchError
	if !errors.As(first.Err, &quarantineErr) {
		t.Fatalf("first outcome error = %T %v, want QuarantinedBatchError", first.Err, first.Err)
	}
	if first.NextUploadAttempt == nil || !first.NextUploadAttempt.Equal(base.Add(100*time.Millisecond)) {
		t.Fatalf("nonretryable outcome did not schedule due catch-up: %v", first.NextUploadAttempt)
	}

	second := awaitUploadOutcome(t, worker)
	if second.Err != nil {
		t.Fatalf("second outcome error = %v", second.Err)
	}
	if second.NextUploadAttempt == nil || !second.NextUploadAttempt.Equal(deferredUntil) {
		t.Fatalf("persisted next-due deadline = %v, want %v", second.NextUploadAttempt, deferredUntil)
	}
	time.Sleep(150 * time.Millisecond)
	if calls, _, _ := drainer.snapshot(); calls != 2 {
		t.Fatalf("future-due record retried early; calls=%d", calls)
	}

	clock.Set(deferredUntil.Add(time.Second))
	worker.Wake()
	third := awaitUploadOutcome(t, worker)
	if third.Err != nil || third.Uploaded != 1 || third.Queue.Depth != 0 {
		t.Fatalf("due outcome = %#v", third)
	}
}

func TestUploadWorkerCloseCancellationSafety(t *testing.T) {
	t.Run("active drain", func(t *testing.T) {
		store := openUploadWorkerStore(t)
		started := make(chan struct{})
		cancelled := make(chan struct{})
		drainer := &scriptedDrainer{}
		drainer.drain = func(ctx context.Context, _ int, _ int) (int, error) {
			close(started)
			<-ctx.Done()
			close(cancelled)
			return 0, ctx.Err()
		}
		worker := newUploadWorker(context.Background(), store)
		worker.SetUploader(drainer)
		worker.SetEnabled(true)
		awaitSignal(t, started, "blocking drain start")

		closed := make(chan struct{})
		go func() {
			worker.Close()
			close(closed)
		}()
		awaitSignal(t, cancelled, "drain context cancellation")
		awaitSignal(t, closed, "worker close")
	})

	t.Run("full outcome channel", func(t *testing.T) {
		store := openUploadWorkerStore(t)
		enqueueUploadWorkerRecord(t, store, "still-due")
		drainer := &scriptedDrainer{}
		worker := newUploadWorker(context.Background(), store)
		worker.catchUpDelay = func() time.Duration { return 100 * time.Millisecond }
		worker.SetUploader(drainer)
		worker.SetEnabled(true)

		// Do not consume outcomes. Fill the bounded channel, then let one more
		// drain block while trying to publish its result.
		awaitDrainerCalls(t, drainer, cap(worker.outcomes)+1)
		closed := make(chan struct{})
		go func() {
			worker.Close()
			close(closed)
		}()
		awaitSignal(t, closed, "worker close with full outcome channel")
	})
}

func openUploadWorkerStore(t *testing.T) *spool.Store {
	t.Helper()
	store, err := spool.Open(filepath.Join(t.TempDir(), "spool.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func enqueueUploadWorkerRecord(t *testing.T, store *spool.Store, batchID string) uint64 {
	t.Helper()
	key, err := store.Enqueue(batchID, []byte(fmt.Sprintf("%q", batchID)), 1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func awaitUploadOutcome(t *testing.T, worker *uploadWorker) uploadOutcome {
	t.Helper()
	select {
	case outcome := <-worker.Outcomes():
		return outcome
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for upload outcome")
		return uploadOutcome{}
	}
}

func awaitSignal(t *testing.T, signal <-chan struct{}, description string) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(2 * time.Second):
		t.Fatalf("timed out waiting for %s", description)
	}
}

func awaitDrainerCalls(t *testing.T, drainer *scriptedDrainer, want int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if calls, _, _ := drainer.snapshot(); calls >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	calls, _, _ := drainer.snapshot()
	t.Fatalf("timed out waiting for %d drain calls; got %d", want, calls)
}
