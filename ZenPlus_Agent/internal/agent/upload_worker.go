package agent

import (
	"context"
	"errors"
	"math/rand"
	"sync"
	"time"

	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

const (
	uploadDrainLimit      = 10
	uploadCatchUpMinDelay = 1 * time.Second
	uploadCatchUpJitter   = 2 * time.Second
)

type batchDrainer interface {
	Drain(context.Context, int) (int, error)
}

type generationBatchDrainer interface {
	DrainGeneration(context.Context, int, uint64) (int, error)
}

type uploadOutcome struct {
	Uploaded          int
	Err               error
	Queue             spool.Stats
	Quarantine        spool.Stats
	NextUploadAttempt *time.Time
	Generation        uint64
}

// uploadWorker owns normal spool replay. It keeps controller I/O off the main
// collection/heartbeat loop, runs only one drain at a time, and continues
// catching up while due work remains. The uploader itself serializes manual
// command drains with this worker as a second single-flight boundary.
type uploadWorker struct {
	ctx    context.Context
	cancel context.CancelFunc
	store  *spool.Store

	mu          sync.RWMutex
	drainer     batchDrainer
	enabled     bool
	notBefore   time.Time
	generation  uint64
	drainCancel context.CancelFunc

	wake     chan struct{}
	outcomes chan uploadOutcome
	done     chan struct{}

	now          func() time.Time
	catchUpDelay func() time.Duration
}

func newUploadWorker(parent context.Context, store *spool.Store) *uploadWorker {
	ctx, cancel := context.WithCancel(parent)
	w := &uploadWorker{
		ctx:    ctx,
		cancel: cancel,
		store:  store,
		wake:   make(chan struct{}, 1),
		// A collection or heartbeat can legitimately occupy the main loop for
		// several seconds. Buffer outcomes so catch-up keeps progressing during
		// that work without losing terminal/auth errors.
		outcomes: make(chan uploadOutcome, 16),
		done:     make(chan struct{}),
		now:      time.Now,
		catchUpDelay: func() time.Duration {
			return uploadCatchUpMinDelay + time.Duration(rand.Int63n(int64(uploadCatchUpJitter)+1))
		},
	}
	go w.run()
	return w
}

func (w *uploadWorker) SetUploader(drainer batchDrainer) uint64 {
	w.mu.Lock()
	if w.drainCancel != nil {
		w.drainCancel()
	}
	w.drainer = drainer
	w.generation = w.store.AdvanceReplayGeneration()
	generation := w.generation
	w.mu.Unlock()
	w.Wake()
	return generation
}

func (w *uploadWorker) SetEnabled(enabled bool) {
	w.mu.Lock()
	changed := w.enabled != enabled
	w.enabled = enabled
	w.mu.Unlock()
	if changed && enabled {
		w.Wake()
	}
}

// SetNotBefore applies appliance backpressure and the controller supervisor's
// outage cooldown to uploads without coupling their network I/O back into the
// heartbeat/collection loop.
func (w *uploadWorker) SetNotBefore(notBefore time.Time) {
	notBefore = notBefore.UTC()
	w.mu.Lock()
	changed := !w.notBefore.Equal(notBefore)
	w.notBefore = notBefore
	enabled := w.enabled
	w.mu.Unlock()
	if changed && enabled {
		w.Wake()
	}
}

func (w *uploadWorker) Wake() {
	select {
	case w.wake <- struct{}{}:
	default:
	}
}

func (w *uploadWorker) Outcomes() <-chan uploadOutcome { return w.outcomes }

func (w *uploadWorker) Close() {
	w.cancel()
	w.store.AdvanceReplayGeneration()
	<-w.done
}

func (w *uploadWorker) snapshot() (batchDrainer, bool, time.Time, uint64) {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return w.drainer, w.enabled, w.notBefore, w.generation
}

func (w *uploadWorker) currentTime() time.Time {
	if w.now == nil {
		return time.Now().UTC()
	}
	return w.now().UTC()
}

func (w *uploadWorker) nextCatchUpDelay() time.Duration {
	if w.catchUpDelay == nil {
		return uploadCatchUpMinDelay
	}
	delay := w.catchUpDelay()
	if delay < 100*time.Millisecond {
		return 100 * time.Millisecond
	}
	return delay
}

func (w *uploadWorker) beginDrain(generation uint64) (context.Context, context.CancelFunc, bool) {
	drainCtx, cancel := context.WithCancel(w.ctx)
	w.mu.Lock()
	if w.generation != generation {
		w.mu.Unlock()
		cancel()
		return nil, nil, false
	}
	w.drainCancel = cancel
	w.mu.Unlock()
	return drainCtx, cancel, true
}

func (w *uploadWorker) finishDrain(generation uint64, cancel context.CancelFunc) {
	cancel()
	w.mu.Lock()
	if w.generation == generation {
		w.drainCancel = nil
	}
	w.mu.Unlock()
}

func (w *uploadWorker) run() {
	defer close(w.done)
	var timer *time.Timer
	var timerC <-chan time.Time
	var controllerNotBefore time.Time
	var controllerGeneration uint64

	stopTimer := func() {
		if timer == nil {
			timerC = nil
			return
		}
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timerC = nil
	}
	schedule := func(delay time.Duration) {
		if delay < 0 {
			delay = 0
		}
		if timer == nil {
			timer = time.NewTimer(delay)
		} else {
			stopTimer()
			timer.Reset(delay)
		}
		timerC = timer.C
	}
	defer func() {
		if timer != nil {
			timer.Stop()
		}
	}()

	for {
		select {
		case <-w.ctx.Done():
			return
		case <-w.wake:
			stopTimer()
		case <-timerC:
			timerC = nil
		}

		drainer, enabled, externalNotBefore, generation := w.snapshot()
		if !enabled || drainer == nil {
			continue
		}
		now := w.currentTime()
		if controllerGeneration != generation {
			controllerNotBefore = time.Time{}
			controllerGeneration = 0
		}
		effectiveNotBefore := controllerNotBefore
		if externalNotBefore.After(effectiveNotBefore) {
			effectiveNotBefore = externalNotBefore
		}
		if effectiveNotBefore.After(now) {
			schedule(effectiveNotBefore.Sub(now))
			continue
		}

		drainCtx, cancelDrain, ok := w.beginDrain(generation)
		if !ok {
			continue
		}
		var uploaded int
		var drainErr error
		if generationDrainer, ok := drainer.(generationBatchDrainer); ok {
			uploaded, drainErr = generationDrainer.DrainGeneration(drainCtx, uploadDrainLimit, generation)
		} else {
			uploaded, drainErr = drainer.Drain(drainCtx, uploadDrainLimit)
		}
		w.finishDrain(generation, cancelDrain)
		now = w.currentTime()
		queue, queueErr := w.store.Stats()
		quarantine, quarantineErr := w.store.QuarantineStats()
		if queueErr != nil {
			drainErr = errors.Join(drainErr, queueErr)
		}
		if quarantineErr != nil {
			drainErr = errors.Join(drainErr, quarantineErr)
		}

		var nextAttempt *time.Time
		var retryable *uploader.RetryableError
		var nonRetryable interface{ NonRetryable() bool }
		_, _, _, currentGeneration := w.snapshot()
		switch {
		case generation != currentGeneration:
			// The old client may finish after credentials or controller settings
			// were refreshed. Replay-generation fencing rejects any late durable
			// mutation, and its auth/backpressure result must not gate the replacement.
			controllerNotBefore = time.Time{}
			controllerGeneration = 0
			due, next, scheduleErr := w.store.NextAttempt(now)
			if scheduleErr != nil {
				drainErr = errors.Join(drainErr, scheduleErr)
			} else if due {
				delay := w.nextCatchUpDelay()
				candidate := now.Add(delay)
				nextAttempt = &candidate
				schedule(delay)
			} else if next != nil {
				candidate := next.UTC()
				nextAttempt = &candidate
				schedule(candidate.Sub(now))
			}
		case errors.As(drainErr, &retryable) && retryable.RetryAfter > 0:
			delay := retryable.RetryAfter
			if delay < time.Second {
				delay = time.Second
			}
			if delay > 24*time.Hour {
				delay = 24 * time.Hour
			}
			controllerNotBefore = now.Add(delay)
			controllerGeneration = generation
			next := controllerNotBefore
			nextAttempt = &next
			schedule(delay)
		case drainErr != nil && !(errors.As(drainErr, &nonRetryable) && nonRetryable.NonRetryable()):
			// Auth, local storage, and unknown permanent failures are surfaced and
			// retried only by the configured upload tick. This avoids a hot loop.
			controllerNotBefore = time.Time{}
			controllerGeneration = 0
		case drainErr == nil || (errors.As(drainErr, &nonRetryable) && nonRetryable.NonRetryable()):
			controllerNotBefore = time.Time{}
			controllerGeneration = 0
			due, next, scheduleErr := w.store.NextAttempt(now)
			if scheduleErr != nil {
				drainErr = errors.Join(drainErr, scheduleErr)
			} else if due {
				delay := w.nextCatchUpDelay()
				candidate := now.Add(delay)
				nextAttempt = &candidate
				schedule(delay)
			} else if next != nil {
				candidate := next.UTC()
				nextAttempt = &candidate
				schedule(candidate.Sub(now))
			}
		}

		outcome := uploadOutcome{
			Uploaded:          uploaded,
			Err:               drainErr,
			Queue:             queue,
			Quarantine:        quarantine,
			NextUploadAttempt: nextAttempt,
			Generation:        generation,
		}
		select {
		case w.outcomes <- outcome:
		case <-w.ctx.Done():
			return
		}
	}
}
