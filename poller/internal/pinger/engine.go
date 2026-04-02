package pinger

import (
	"context"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/config"
	"go.uber.org/zap"
)

// DeviceLoader loads devices from the database.
type DeviceLoader interface {
	LoadDevices(ctx context.Context) ([]*Device, error)
	UpdateDeviceStatus(ctx context.Context, deviceID uuid.UUID, status string, lastSeen time.Time, rttMs float64) error
}

// MetricWriter writes ping results to the metrics store.
type MetricWriter interface {
	WriteResult(result *PingResult)
	RunBatchWriter(ctx context.Context)
	WriteStatusChange(ctx context.Context, sc *StatusChange, durationSec uint64) error
}

// EventPublisher publishes real-time events.
type EventPublisher interface {
	PublishMetric(ctx context.Context, result *PingResult) error
	PublishStatusChange(ctx context.Context, sc *StatusChange) error
}

// Engine is the main ping monitoring engine.
type Engine struct {
	cfg       *config.Config
	pinger    *Pinger
	loader    DeviceLoader
	writer    MetricWriter
	publisher EventPublisher
	logger    *zap.SugaredLogger

	mu          sync.RWMutex
	devices     map[uuid.UUID]*Device
	startTime   time.Time
	lastCycleMs int64
	activePings int
}

// NewEngine creates a new monitoring engine.
func NewEngine(cfg *config.Config, loader DeviceLoader, writer MetricWriter, publisher EventPublisher, logger *zap.SugaredLogger) (*Engine, error) {
	p := NewPinger(
		cfg.Poller.PingTimeout,
		cfg.Poller.PingCount,
		cfg.Poller.PingInterval,
		cfg.Poller.Privileged,
		logger,
	)

	return &Engine{
		cfg:       cfg,
		pinger:    p,
		loader:    loader,
		writer:    writer,
		publisher: publisher,
		logger:    logger,
		devices:   make(map[uuid.UUID]*Device),
		startTime: time.Now(),
	}, nil
}

// Run starts the main monitoring loop.
func (e *Engine) Run(ctx context.Context) {
	// Start the batch writer
	go e.writer.RunBatchWriter(ctx)

	// Initial device load
	if err := e.syncDevices(ctx); err != nil {
		e.logger.Errorf("Initial device sync failed: %v", err)
	}

	// Start device sync ticker
	syncTicker := time.NewTicker(e.cfg.Poller.DeviceSyncInterval)
	defer syncTicker.Stop()

	// Start ping cycle ticker (use the minimum device interval, default 60s)
	pingTicker := time.NewTicker(60 * time.Second)
	defer pingTicker.Stop()

	// Run first ping cycle immediately
	e.runPingCycle(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-syncTicker.C:
			if err := e.syncDevices(ctx); err != nil {
				e.logger.Errorf("Device sync failed: %v", err)
			}
		case <-pingTicker.C:
			e.runPingCycle(ctx)
		}
	}
}

// Shutdown performs graceful shutdown.
func (e *Engine) Shutdown(ctx context.Context) {
	e.logger.Info("Engine shutting down...")
}

// HealthStatus returns the current health status.
func (e *Engine) HealthStatus() *HealthStatus {
	e.mu.RLock()
	defer e.mu.RUnlock()

	return &HealthStatus{
		Status:      "ok",
		PollerID:    e.cfg.Poller.ID,
		DeviceCount: len(e.devices),
		ActivePings: e.activePings,
		Uptime:      time.Since(e.startTime).String(),
		LastCycleMs: e.lastCycleMs,
	}
}

func (e *Engine) syncDevices(ctx context.Context) error {
	devices, err := e.loader.LoadDevices(ctx)
	if err != nil {
		return err
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	// Update existing devices, add new ones
	seen := make(map[uuid.UUID]bool)
	for _, d := range devices {
		seen[d.ID] = true
		if existing, ok := e.devices[d.ID]; ok {
			// Preserve runtime state
			d.DownCount = existing.DownCount
			d.Status = existing.Status
		}
		e.devices[d.ID] = d
	}

	// Remove deleted devices
	for id := range e.devices {
		if !seen[id] {
			delete(e.devices, id)
		}
	}

	e.logger.Infof("Device sync complete: %d devices loaded", len(e.devices))
	return nil
}

func (e *Engine) runPingCycle(ctx context.Context) {
	e.mu.RLock()
	deviceList := make([]*Device, 0, len(e.devices))
	for _, d := range e.devices {
		if d.PingEnabled {
			deviceList = append(deviceList, d)
		}
	}
	e.mu.RUnlock()

	if len(deviceList) == 0 {
		return
	}

	e.logger.Infof("Starting ping cycle for %d devices", len(deviceList))
	start := time.Now()

	e.mu.Lock()
	e.activePings = len(deviceList)
	e.mu.Unlock()

	// Use worker pool - limit concurrent pings
	maxWorkers := 100
	if len(deviceList) < maxWorkers {
		maxWorkers = len(deviceList)
	}

	results := e.pinger.PingBatch(ctx, deviceList, e.cfg.Poller.ID, maxWorkers)

	e.mu.Lock()
	e.activePings = 0
	e.lastCycleMs = time.Since(start).Milliseconds()
	e.mu.Unlock()

	e.logger.Infof("Ping cycle complete: %d results in %dms", len(results), time.Since(start).Milliseconds())

	// Process results
	for _, result := range results {
		// Write metric to ClickHouse
		e.writer.WriteResult(result)

		// Publish to Redis for real-time updates
		if err := e.publisher.PublishMetric(ctx, result); err != nil {
			e.logger.Debugf("Failed to publish metric: %v", err)
		}

		// Detect status changes
		e.processStatusChange(ctx, result)
	}
}

func (e *Engine) processStatusChange(ctx context.Context, result *PingResult) {
	e.mu.Lock()
	defer e.mu.Unlock()

	device, ok := e.devices[result.DeviceID]
	if !ok {
		return
	}

	oldStatus := device.Status
	var newStatus string

	if !result.IsUp {
		device.DownCount++
		if device.DownCount >= e.cfg.Poller.DownThreshold {
			newStatus = "down"
		} else {
			newStatus = oldStatus // Not yet confirmed down
			return
		}
	} else {
		device.DownCount = 0
		rttMs := float64(result.RTT.Microseconds()) / 1000.0

		if rttMs > e.cfg.Poller.DegradedRTTMs || result.PacketLoss > float32(e.cfg.Poller.DegradedLossPct)/100.0 {
			newStatus = "degraded"
		} else {
			newStatus = "up"
		}

		device.LastSeen = result.Timestamp
		device.LastRTT = rttMs

		// Always update last_seen/rtt in PostgreSQL on successful ping
		go func() {
			if err := e.loader.UpdateDeviceStatus(ctx, device.ID, newStatus, result.Timestamp, rttMs); err != nil {
				e.logger.Errorf("Failed to update device last_seen in PG: %v", err)
			}
		}()
	}

	if newStatus != oldStatus {
		device.Status = newStatus

		reason := ""
		switch newStatus {
		case "down":
			reason = "No response for consecutive checks"
		case "degraded":
			reason = "High latency or packet loss"
		case "up":
			reason = "Device responding normally"
		}

		sc := &StatusChange{
			DeviceID:  device.ID,
			OldStatus: oldStatus,
			NewStatus: newStatus,
			Reason:    reason,
			Timestamp: time.Now().UTC(),
		}

		e.logger.Infof("Status change: %s (%s) %s → %s: %s",
			device.Hostname, device.IPAddress, oldStatus, newStatus, reason)

		// Update PostgreSQL
		rttMs := float64(result.RTT.Microseconds()) / 1000.0
		go func() {
			if err := e.loader.UpdateDeviceStatus(ctx, device.ID, newStatus, result.Timestamp, rttMs); err != nil {
				e.logger.Errorf("Failed to update device status in PG: %v", err)
			}
		}()

		// Log to ClickHouse
		go func() {
			if err := e.writer.WriteStatusChange(ctx, sc, 0); err != nil {
				e.logger.Errorf("Failed to write status change to CH: %v", err)
			}
		}()

		// Publish to Redis
		go func() {
			if err := e.publisher.PublishStatusChange(ctx, sc); err != nil {
				e.logger.Errorf("Failed to publish status change: %v", err)
			}
		}()
	}
}
