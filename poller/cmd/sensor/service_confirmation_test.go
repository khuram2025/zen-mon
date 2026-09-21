package main

import (
	"context"
	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"go.uber.org/zap"
	"testing"
	"time"
)

func TestSensorConfirmationEmitsEveryAttemptAndFinalVerdict(t *testing.T) {
	for _, values := range [][]bool{{false, true}, {false, false, false}} {
		runner := &sequenceProbeRunner{results: values}
		scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
		var attempts []int
		var finals []bool
		result := scheduler.runServiceWithRetries(context.Background(), &checker.ServiceCheck{RetryCount: 3, RetryDelay: time.Millisecond}, func(_ *checker.ServiceCheckResult, n int, final bool) {
			attempts = append(attempts, n)
			finals = append(finals, final)
		})
		if len(attempts) != len(values) || result.IsUp != values[len(values)-1] {
			t.Fatal("missing attempt or wrong verdict")
		}
		for i, n := range attempts {
			if n != i+1 || finals[i] != (i == len(attempts)-1) {
				t.Fatal("incorrect confirmation sequence")
			}
		}
	}
}

func TestSensorRecoveryChecksOnceAndReturnsToHealthySchedule(t *testing.T) {
	runner := &sequenceProbeRunner{results: []bool{false, true}}
	var payloads []map[string]any
	scheduler := newCheckScheduler(runner, "test", 1, func(_ string, v any) error { payloads = append(payloads, v.(map[string]any)); return nil }, zap.NewNop().Sugar())
	id := uuid.New()
	key := "service:" + id.String()
	scheduler.targets[key] = &targetSchedule{serviceDown: true}
	sc := configServiceCheck{ID: id.String(), CheckType: "http", RetryCount: 5, RetryDelayS: 10, ConfirmationProgress: true}
	scheduler.runService(context.Background(), sc, id, time.Minute)
	if runner.calls != 1 || !scheduler.targets[key].serviceDown {
		t.Fatal("Down service repeated confirmation")
	}
	scheduler.runService(context.Background(), sc, id, time.Minute)
	if runner.calls != 2 || scheduler.targets[key].serviceDown {
		t.Fatal("first successful check did not recover")
	}
	if len(payloads) != 2 || payloads[0]["confirmation_complete"] != true {
		t.Fatal("recovery result missing")
	}
}

func TestSensorLegacyServerReceivesFinalResultOnly(t *testing.T) {
	runner := &sequenceProbeRunner{results: []bool{false, true}}
	count := 0
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { count++; return nil }, zap.NewNop().Sugar())
	// Use the low-level helper for millisecond waits and the same optional callback
	// contract used by legacy runService: without it, partial failures stay local.
	result := scheduler.runServiceWithRetries(context.Background(), &checker.ServiceCheck{RetryCount: 2, RetryDelay: time.Millisecond})
	scheduler.emitServiceAttempt(configServiceCheck{ID: uuid.NewString()}, result, 0, true)
	if count != 1 || !result.IsUp {
		t.Fatal("partial failure sent to legacy server")
	}
}
