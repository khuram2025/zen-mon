package pinger

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/devicestatus"
	"go.uber.org/zap"
)

func TestDueReturnsTrueForNeverRunCheck(t *testing.T) {
	if !due(time.Now(), time.Time{}, time.Minute) {
		t.Fatal("expected check with zero last-run timestamp to be due")
	}
}

func TestDueHonorsConfiguredInterval(t *testing.T) {
	now := time.Now()
	last := now.Add(-30 * time.Second)

	if due(now, last, time.Minute) {
		t.Fatal("expected check to wait until interval has elapsed")
	}

	if !due(now, last, 15*time.Second) {
		t.Fatal("expected check to be due after configured interval elapsed")
	}
}

func TestEffectiveIntervalFallsBackWhenUnset(t *testing.T) {
	fallback := time.Minute

	if got := effectiveInterval(0, fallback); got != fallback {
		t.Fatalf("expected fallback interval %s, got %s", fallback, got)
	}

	configured := 15 * time.Second
	if got := effectiveInterval(configured, fallback); got != configured {
		t.Fatalf("expected configured interval %s, got %s", configured, got)
	}
}

// fakeDeviceLoader stands in for the PostgreSQL store. LoadDevices returns
// fresh copies of devices (as the store builds new structs on every sync);
// saves and releases are recorded.
type fakeDeviceLoader struct {
	mu        sync.Mutex
	devices   []*Device
	policyRaw map[string]any
	policyErr error
	// saveResult overrides the default result (devices and state written).
	saveResult func(*DeviceStatusSave) (DeviceStatusSaved, error)
	saves      []DeviceStatusSave
	released   [][]uuid.UUID
}

func (f *fakeDeviceLoader) LoadDevices(ctx context.Context) ([]*Device, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]*Device, 0, len(f.devices))
	for _, d := range f.devices {
		c := *d
		out = append(out, &c)
	}
	return out, nil
}

func (f *fakeDeviceLoader) SaveDeviceStatus(ctx context.Context, save *DeviceStatusSave) (DeviceStatusSaved, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.saves = append(f.saves, *save)
	if f.saveResult != nil {
		return f.saveResult(save)
	}
	return DeviceStatusSaved{Device: true, State: true}, nil
}

func (f *fakeDeviceLoader) ReleaseDevices(ctx context.Context, ids []uuid.UUID) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.released = append(f.released, append([]uuid.UUID(nil), ids...))
	return nil
}

func (f *fakeDeviceLoader) LoadActiveMaintenanceDeviceIDs(ctx context.Context) (map[uuid.UUID]struct{}, error) {
	return map[uuid.UUID]struct{}{}, nil
}

func (f *fakeDeviceLoader) LoadStatusPolicy(ctx context.Context, defaults devicestatus.Defaults) (devicestatus.Policy, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.policyErr != nil {
		return devicestatus.Policy{}, f.policyErr
	}
	return devicestatus.PolicyFromSettings(f.policyRaw, defaults), nil
}

// savedStatuses returns the devices.status written by every save so far.
func (f *fakeDeviceLoader) savedStatuses() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, 0, len(f.saves))
	for _, s := range f.saves {
		out = append(out, s.State.Displayed())
	}
	return out
}

func (f *fakeDeviceLoader) savesCopy() []DeviceStatusSave {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]DeviceStatusSave(nil), f.saves...)
}

// fakeAlertEvaluator records alert evaluations instead of POSTing to the
// local API. block, when set, holds every call for that device until the
// channel is closed.
type fakeAlertEvaluator struct {
	mu     sync.Mutex
	events []DeviceAlertEvent
	block  map[uuid.UUID]chan struct{}
}

func (f *fakeAlertEvaluator) EvaluateDeviceStatus(ctx context.Context, ev DeviceAlertEvent) error {
	f.mu.Lock()
	gate := f.block[ev.DeviceID]
	f.mu.Unlock()
	if gate != nil {
		select {
		case <-gate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, ev)
	return nil
}

func (f *fakeAlertEvaluator) eventsCopy() []DeviceAlertEvent {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]DeviceAlertEvent(nil), f.events...)
}

type fakeMetricWriter struct {
	mu            sync.Mutex
	statusChanges []*StatusChange
}

func (f *fakeMetricWriter) WriteResult(result *PingResult) {}

func (f *fakeMetricWriter) RunBatchWriter(ctx context.Context) {}

func (f *fakeMetricWriter) WriteStatusChange(ctx context.Context, sc *StatusChange, durationSec uint64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statusChanges = append(f.statusChanges, sc)
	return nil
}

func (f *fakeMetricWriter) changes() []*StatusChange {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]*StatusChange(nil), f.statusChanges...)
}

type fakeEventPublisher struct {
	mu            sync.Mutex
	statusChanges []*StatusChange
}

func (f *fakeEventPublisher) PublishMetric(ctx context.Context, result *PingResult) error {
	return nil
}

func (f *fakeEventPublisher) PublishStatusChange(ctx context.Context, sc *StatusChange) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statusChanges = append(f.statusChanges, sc)
	return nil
}

func (f *fakeEventPublisher) changes() []*StatusChange {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]*StatusChange(nil), f.statusChanges...)
}

type fakeServiceLoader struct {
	mu      sync.Mutex
	updates []string
}

func (f *fakeServiceLoader) LoadServiceChecks(ctx context.Context) ([]*checker.ServiceCheck, error) {
	return nil, nil
}

func (f *fakeServiceLoader) UpdateServiceCheckStatus(ctx context.Context, id uuid.UUID, status string, lastCheckAt time.Time, responseMs float64, lastError string, tlsExpiry *time.Time, tlsDaysRemaining *int, tlsIssuer string, tlsSubject string, verificationAttempt int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.updates = append(f.updates, status)
	return nil
}

func (f *fakeServiceLoader) IsServiceCheckCentrallyOwned(context.Context, uuid.UUID) (bool, error) {
	return true, nil
}

func (f *fakeServiceLoader) LoadActiveMaintenanceCheckIDs(ctx context.Context) (map[uuid.UUID]struct{}, error) {
	return map[uuid.UUID]struct{}{}, nil
}

type fakeServiceMetricWriter struct {
	mu            sync.Mutex
	statusChanges []*checker.ServiceStatusChange
}

func (f *fakeServiceMetricWriter) WriteServiceResult(result *checker.ServiceCheckResult) {}

func (f *fakeServiceMetricWriter) RunServiceBatchWriter(ctx context.Context) {}

func (f *fakeServiceMetricWriter) WriteServiceStatusChange(ctx context.Context, sc *checker.ServiceStatusChange, durationSec uint64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statusChanges = append(f.statusChanges, sc)
	return nil
}

type fakeServiceEventPublisher struct {
	mu            sync.Mutex
	statusChanges []*checker.ServiceStatusChange
}

func (f *fakeServiceEventPublisher) PublishServiceMetric(ctx context.Context, result *checker.ServiceCheckResult) error {
	return nil
}

func (f *fakeServiceEventPublisher) PublishServiceStatusChange(ctx context.Context, sc *checker.ServiceStatusChange) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statusChanges = append(f.statusChanges, sc)
	return nil
}

func testEngine() *Engine {
	cfg := &config.Config{
		Poller: config.PollerConfig{
			ID:              "test-poller",
			DownThreshold:   2,
			DegradedRTTMs:   100,
			DegradedLossPct: 10,
		},
	}

	return &Engine{
		cfg:           cfg,
		policy:        devicestatus.PolicyFromSettings(nil, policyDefaults(cfg)),
		loader:        &fakeDeviceLoader{},
		writer:        &fakeMetricWriter{},
		publisher:     &fakeEventPublisher{},
		alerts:        &fakeAlertEvaluator{},
		svcLoader:     &fakeServiceLoader{},
		svcWriter:     &fakeServiceMetricWriter{},
		svcPublisher:  &fakeServiceEventPublisher{},
		logger:        zap.NewNop().Sugar(),
		devices:       make(map[uuid.UUID]*Device),
		serviceChecks: make(map[uuid.UUID]*checker.ServiceCheck),
		lastPingAt:    make(map[uuid.UUID]time.Time),
		lastServiceAt: make(map[uuid.UUID]time.Time),
		startTime:     time.Now(),
	}
}

// flushStatus waits until the status writer persisted everything queued so
// far and delivered its side effects.
func flushStatus(t *testing.T, e *Engine) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	e.statusPipeline().flush(ctx)
	if ctx.Err() != nil {
		t.Fatal("device status writer did not drain")
	}
}

// stopStatusOnCleanup ends the engine's status writer when the test ends.
func stopStatusOnCleanup(t *testing.T, e *Engine) {
	t.Cleanup(func() {
		if p := e.statusPipelineIfStarted(); p != nil {
			p.stop()
		}
	})
}

func TestDeviceStatusRequiresConsecutiveFailuresBeforeDown(t *testing.T) {
	e := testEngine()
	stopStatusOnCleanup(t, e)
	id := uuid.New()
	e.devices[id] = &Device{
		ID:        id,
		Hostname:  "edge-01",
		IPAddress: net.ParseIP("192.0.2.10"),
		Status:    "up",
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	base := time.Now()

	e.processStatusChange(ctx, &PingResult{
		DeviceID:  id,
		IsUp:      false,
		Timestamp: base,
	})

	if got := e.devices[id].Status; got != "up" {
		t.Fatalf("expected status to remain up before threshold, got %q", got)
	}
	if got := e.devices[id].State.FailStreak; got != 1 {
		t.Fatalf("expected fail streak 1, got %d", got)
	}

	e.processStatusChange(ctx, &PingResult{
		DeviceID:  id,
		IsUp:      false,
		Timestamp: base.Add(30 * time.Second),
	})

	if got := e.devices[id].Status; got != "down" {
		t.Fatalf("expected status to transition down at threshold, got %q", got)
	}
	flushStatus(t, e)
	if got := e.loader.(*fakeDeviceLoader).savedStatuses(); len(got) != 2 || got[0] != "up" || got[1] != "down" {
		t.Fatalf("persisted statuses = %v, want [up down]", got)
	}
}

// One slow check used to flip the device to degraded (single-sample
// classification). It is now evidence in the confirmation window: degraded
// needs degraded_confirm_checks (3) bad checks among the last 5.
func TestDeviceStatusTransitionsToDegradedForHighLatency(t *testing.T) {
	e := testEngine()
	stopStatusOnCleanup(t, e)
	id := uuid.New()
	e.devices[id] = &Device{
		ID:        id,
		Hostname:  "edge-01",
		IPAddress: net.ParseIP("192.0.2.10"),
		Status:    "up",
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	base := time.Now()

	for i := 0; i < 3; i++ {
		e.processStatusChange(ctx, &PingResult{
			DeviceID:   id,
			IsUp:       true,
			RTT:        250 * time.Millisecond,
			PacketLoss: 0,
			Timestamp:  base.Add(time.Duration(i) * 30 * time.Second),
		})
		want := "up"
		if i == 2 {
			want = "degraded"
		}
		if got := e.devices[id].Status; got != want {
			t.Fatalf("after %d slow checks: status %q, want %q", i+1, got, want)
		}
	}

	device := e.devices[id]
	if device.State.FailStreak != 0 {
		t.Fatalf("expected fail streak reset, got %d", device.State.FailStreak)
	}
	if device.LastRTT != 250 {
		t.Fatalf("expected last RTT 250ms, got %f", device.LastRTT)
	}
}

func TestServiceStatusRequiresRetryThresholdBeforeDown(t *testing.T) {
	e := testEngine()
	id := uuid.New()
	e.serviceChecks[id] = &checker.ServiceCheck{
		ID:         id,
		Name:       "Website",
		CheckType:  "http",
		Status:     "up",
		RetryCount: 2,
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result := &checker.ServiceCheckResult{
		ServiceCheckID: id,
		CheckType:      "http",
		IsUp:           false,
		Error:          "connection refused",
		Timestamp:      time.Now(),
	}

	e.processServiceStatusChange(ctx, result, map[uuid.UUID]struct{}{})
	if got := e.serviceChecks[id].Status; got != "up" {
		t.Fatalf("expected status to remain up before retry threshold, got %q", got)
	}

	e.processServiceStatusChange(ctx, result, map[uuid.UUID]struct{}{})
	if got := e.serviceChecks[id].Status; got != "down" {
		t.Fatalf("expected status to transition down at retry threshold, got %q", got)
	}
}

func TestServiceTLSWarningStatus(t *testing.T) {
	e := testEngine()
	id := uuid.New()
	e.serviceChecks[id] = &checker.ServiceCheck{
		ID:              id,
		Name:            "TLS",
		CheckType:       "tls",
		Status:          "up",
		RetryCount:      1,
		TLSWarnDays:     30,
		TLSCriticalDays: 7,
	}

	daysRemaining := 14
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	e.processServiceStatusChange(ctx, &checker.ServiceCheckResult{
		ServiceCheckID:   id,
		CheckType:        "tls",
		IsUp:             true,
		TLSDaysRemaining: &daysRemaining,
		Timestamp:        time.Now(),
	}, map[uuid.UUID]struct{}{})

	if got := e.serviceChecks[id].Status; got != "warning" {
		t.Fatalf("expected TLS warning status, got %q", got)
	}
}

func TestServiceMaintenanceSuppressesStatusChangeEvents(t *testing.T) {
	e := testEngine()
	id := uuid.New()
	writer := e.svcWriter.(*fakeServiceMetricWriter)
	publisher := e.svcPublisher.(*fakeServiceEventPublisher)
	e.serviceChecks[id] = &checker.ServiceCheck{
		ID:         id,
		Name:       "Website",
		CheckType:  "http",
		Status:     "up",
		RetryCount: 1,
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	e.processServiceStatusChange(ctx, &checker.ServiceCheckResult{
		ServiceCheckID: id,
		CheckType:      "http",
		IsUp:           false,
		Error:          "connection refused",
		Timestamp:      time.Now(),
	}, map[uuid.UUID]struct{}{id: {}})

	if got := e.serviceChecks[id].Status; got != "down" {
		t.Fatalf("expected in-memory status to update during maintenance, got %q", got)
	}

	time.Sleep(10 * time.Millisecond)

	writer.mu.Lock()
	writes := len(writer.statusChanges)
	writer.mu.Unlock()
	if writes != 0 {
		t.Fatalf("expected maintenance to suppress status-change writes, got %d", writes)
	}

	publisher.mu.Lock()
	publishes := len(publisher.statusChanges)
	publisher.mu.Unlock()
	if publishes != 0 {
		t.Fatalf("expected maintenance to suppress status-change publishes, got %d", publishes)
	}
}

// Isolated failures stay diagnostic under the default policy: fewer than 3
// consecutive failures never confirm Down, and fewer than 3 bad checks among
// the last 5 never confirm Degraded (unanswered checks are bad evidence in
// that window, see TestIntermittentFailuresConfirmDegradedNotDown). The ctx
// is live: alert evaluation goes to the injected fake, never to the local API.
func TestDeviceDefaultPolicyKeepsIsolatedFailuresDiagnostic(t *testing.T) {
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Poller.DownThreshold != 3 || cfg.Poller.PingCount != 3 {
		t.Fatal("default confirmation policy changed")
	}
	e := testEngine()
	stopStatusOnCleanup(t, e)
	e.cfg.Poller.DownThreshold = cfg.Poller.DownThreshold
	e.policy = devicestatus.PolicyFromSettings(nil, policyDefaults(e.cfg))
	if e.policy.DownConfirmChecks != 3 || e.policy.UpConfirmChecks != 1 {
		t.Fatalf("default policy: down=%d up=%d", e.policy.DownConfirmChecks, e.policy.UpConfirmChecks)
	}
	alerts := e.alerts.(*fakeAlertEvaluator)
	id := uuid.New()
	e.devices[id] = &Device{ID: id, Hostname: "fixture", IPAddress: net.ParseIP("192.0.2.10"), Status: "up"}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sequence := []struct {
		up       bool
		status   string
		failures int
	}{
		{false, "up", 1}, {true, "up", 0}, {true, "up", 0}, {true, "up", 0}, {true, "up", 0},
		{false, "up", 1}, {false, "up", 2}, {true, "up", 0}, {true, "up", 0}, {true, "up", 0},
		{false, "up", 1}, {false, "up", 2}, {false, "down", 3}, {true, "up", 0},
	}
	base := time.Now()
	for i, step := range sequence {
		e.processStatusChange(ctx, &PingResult{DeviceID: id, IsUp: step.up, Timestamp: base.Add(time.Duration(i) * 30 * time.Second)})
		if e.devices[id].Status != step.status || e.devices[id].State.FailStreak != step.failures {
			t.Fatalf("step %d: status=%s failures=%d", i, e.devices[id].Status, e.devices[id].State.FailStreak)
		}
	}
	flushStatus(t, e)
	got := alerts.eventsCopy()
	if len(got) != 2 || got[0].OldStatus != "up" || got[0].NewStatus != "down" || got[1].NewStatus != "up" {
		t.Fatalf("alert evaluations = %+v, want up→down then down→up", got)
	}
}

// Failures that are not consecutive enough for Down still are evidence: three
// unanswered checks among the last five confirm Degraded on the next answer
// (a failing check itself never enters Degraded; Down outranks it).
func TestIntermittentFailuresConfirmDegradedNotDown(t *testing.T) {
	e := testEngine()
	stopStatusOnCleanup(t, e)
	e.policy = devicestatus.PolicyFromSettings(map[string]any{"down_confirm_checks": 3.0}, policyDefaults(e.cfg))
	id := uuid.New()
	e.devices[id] = &Device{ID: id, Hostname: "fixture", IPAddress: net.ParseIP("192.0.2.10"), Status: "up"}
	ctx := context.Background()
	base := time.Now()
	for i, up := range []bool{false, true, false, false} {
		e.processStatusChange(ctx, &PingResult{DeviceID: id, IsUp: up, Timestamp: base.Add(time.Duration(i) * 30 * time.Second)})
		if got := e.devices[id].Status; got != "up" {
			t.Fatalf("check %d: status %s, want up", i, got)
		}
	}
	e.processStatusChange(ctx, &PingResult{DeviceID: id, IsUp: true, RTT: 5 * time.Millisecond, Timestamp: base.Add(4 * 30 * time.Second)})
	if got := e.devices[id].Status; got != "degraded" {
		t.Fatalf("status %s, want degraded", got)
	}
	flushStatus(t, e)
	rows := e.writer.(*fakeMetricWriter).changes()
	if len(rows) != 1 || rows[0].Reason != "3 of the last 5 checks over limits or unanswered; latest check within limits" {
		t.Fatalf("status log rows = %+v", rows)
	}
}
