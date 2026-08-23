package agent

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"zenplus-agent/internal/netcapture"
)

func TestCaptureManagerStartIsIdempotentAndStopCancels(t *testing.T) {
	var runs atomic.Int32
	started := make(chan struct{})
	runner := func(ctx context.Context, _ string, _ netcapture.Options, _ captureSender, _ func(string, ...any)) string {
		if runs.Add(1) == 1 {
			close(started)
		}
		<-ctx.Done()
		return "cancelled"
	}
	m := newCaptureManager(runner)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	first, err := m.Start(ctx, "capture-1", netcapture.Options{}, nil, t.Logf)
	if err != nil || first.Status != "running" || first.Duplicate {
		t.Fatalf("unexpected first start: %+v, %v", first, err)
	}
	<-started
	duplicate, err := m.Start(ctx, "capture-1", netcapture.Options{}, nil, t.Logf)
	if err != nil || !duplicate.Duplicate || duplicate.Status != "running" {
		t.Fatalf("duplicate start was not idempotent: %+v, %v", duplicate, err)
	}
	if got := runs.Load(); got != 1 {
		t.Fatalf("duplicate start invoked runner %d times", got)
	}
	stopped, err := m.Stop(ctx, "capture-1")
	if err != nil || stopped.Status != "cancelled" || stopped.Duplicate {
		t.Fatalf("unexpected stop result: %+v, %v", stopped, err)
	}
	again, err := m.Stop(ctx, "capture-1")
	if err != nil || !again.Duplicate || again.Status != "cancelled" {
		t.Fatalf("completed stop was not idempotent: %+v, %v", again, err)
	}
	restart, err := m.Start(ctx, "capture-1", netcapture.Options{}, nil, t.Logf)
	if err != nil || !restart.Duplicate || restart.Status != "cancelled" {
		t.Fatalf("completed start was not idempotent: %+v, %v", restart, err)
	}
}

func TestCaptureManagerRejectsDifferentConcurrentCapture(t *testing.T) {
	runner := func(ctx context.Context, _ string, _ netcapture.Options, _ captureSender, _ func(string, ...any)) string {
		<-ctx.Done()
		return "cancelled"
	}
	m := newCaptureManager(runner)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := m.Start(ctx, "capture-1", netcapture.Options{}, nil, t.Logf); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Start(ctx, "capture-2", netcapture.Options{}, nil, t.Logf); err == nil {
		t.Fatal("expected a different concurrent capture to be rejected")
	}
	if _, err := m.Stop(ctx, ""); err != nil {
		t.Fatal(err)
	}
}

func TestCaptureManagerStopAllowsImmediateDifferentStart(t *testing.T) {
	runner := func(ctx context.Context, _ string, _ netcapture.Options, _ captureSender, _ func(string, ...any)) string {
		<-ctx.Done()
		return "cancelled"
	}
	m := newCaptureManager(runner)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := m.Start(ctx, "capture-1", netcapture.Options{}, nil, t.Logf); err != nil {
		t.Fatal(err)
	}
	stopped, err := m.Stop(ctx, "capture-1")
	if err != nil || stopped.Status != "cancelled" {
		t.Fatalf("stop did not wait for teardown: %+v, %v", stopped, err)
	}
	if _, err := m.Start(ctx, "capture-2", netcapture.Options{}, nil, t.Logf); err != nil {
		t.Fatalf("new capture was rejected immediately after successful stop: %v", err)
	}
	if _, err := m.Stop(ctx, "capture-2"); err != nil {
		t.Fatal(err)
	}
}

func waitCaptureState(t *testing.T, m *captureManager, id, want string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if got, ok := m.Status(id); ok && got == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	got, _ := m.Status(id)
	t.Fatalf("capture %s did not reach %s (last state %s)", id, want, got)
}
