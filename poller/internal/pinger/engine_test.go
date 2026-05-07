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

type fakeDeviceLoader struct {
	mu      sync.Mutex
	updates []string
}

func (f *fakeDeviceLoader) LoadDevices(ctx context.Context) ([]*Device, error) {
	return nil, nil
}

func (f *fakeDeviceLoader) UpdateDeviceStatus(ctx context.Context, deviceID uuid.UUID, status string, lastSeen time.Time, rttMs float64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.updates = append(f.updates, status)
	return nil
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

type fakeServiceLoader struct {
	mu      sync.Mutex
	updates []string
}

func (f *fakeServiceLoader) LoadServiceChecks(ctx context.Context) ([]*checker.ServiceCheck, error) {
	return nil, nil
}

func (f *fakeServiceLoader) UpdateServiceCheckStatus(ctx context.Context, id uuid.UUID, status string, lastCheckAt time.Time, responseMs float64, lastError string, tlsExpiry *time.Time, tlsDaysRemaining *int, tlsIssuer string, tlsSubject string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.updates = append(f.updates, status)
	return nil
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
		loader:        &fakeDeviceLoader{},
		writer:        &fakeMetricWriter{},
		publisher:     &fakeEventPublisher{},
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

func TestDeviceStatusRequiresConsecutiveFailuresBeforeDown(t *testing.T) {
	e := testEngine()
	id := uuid.New()
	e.devices[id] = &Device{
		ID:        id,
		Hostname:  "edge-01",
		IPAddress: net.ParseIP("192.0.2.10"),
		Status:    "up",
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	e.processStatusChange(ctx, &PingResult{
		DeviceID:  id,
		IsUp:      false,
		Timestamp: time.Now(),
	})

	if got := e.devices[id].Status; got != "up" {
		t.Fatalf("expected status to remain up before threshold, got %q", got)
	}
	if got := e.devices[id].DownCount; got != 1 {
		t.Fatalf("expected down count 1, got %d", got)
	}

	e.processStatusChange(ctx, &PingResult{
		DeviceID:  id,
		IsUp:      false,
		Timestamp: time.Now(),
	})

	if got := e.devices[id].Status; got != "down" {
		t.Fatalf("expected status to transition down at threshold, got %q", got)
	}
}

func TestDeviceStatusTransitionsToDegradedForHighLatency(t *testing.T) {
	e := testEngine()
	id := uuid.New()
	e.devices[id] = &Device{
		ID:        id,
		Hostname:  "edge-01",
		IPAddress: net.ParseIP("192.0.2.10"),
		Status:    "up",
		DownCount: 1,
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	e.processStatusChange(ctx, &PingResult{
		DeviceID:   id,
		IsUp:       true,
		RTT:        250 * time.Millisecond,
		PacketLoss: 0,
		Timestamp:  time.Now(),
	})

	device := e.devices[id]
	if device.Status != "degraded" {
		t.Fatalf("expected degraded status, got %q", device.Status)
	}
	if device.DownCount != 0 {
		t.Fatalf("expected down count reset, got %d", device.DownCount)
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
