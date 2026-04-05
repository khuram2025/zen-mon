package pinger

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
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

// ServiceCheckLoader loads service checks from the database.
type ServiceCheckLoader interface {
	LoadServiceChecks(ctx context.Context) ([]*checker.ServiceCheck, error)
	UpdateServiceCheckStatus(ctx context.Context, id uuid.UUID, status string, lastCheckAt time.Time, responseMs float64, lastError string, tlsExpiry *time.Time, tlsDaysRemaining *int, tlsIssuer string, tlsSubject string) error
}

// ServiceMetricWriter writes service check results.
type ServiceMetricWriter interface {
	WriteServiceResult(result *checker.ServiceCheckResult)
	RunServiceBatchWriter(ctx context.Context)
	WriteServiceStatusChange(ctx context.Context, sc *checker.ServiceStatusChange, durationSec uint64) error
}

// ServiceEventPublisher publishes service check events.
type ServiceEventPublisher interface {
	PublishServiceMetric(ctx context.Context, result *checker.ServiceCheckResult) error
	PublishServiceStatusChange(ctx context.Context, sc *checker.ServiceStatusChange) error
}

// Engine is the main monitoring engine.
type Engine struct {
	cfg       *config.Config
	pinger    *Pinger
	loader    DeviceLoader
	writer    MetricWriter
	publisher EventPublisher

	// Service check interfaces
	svcLoader    ServiceCheckLoader
	svcWriter    ServiceMetricWriter
	svcPublisher ServiceEventPublisher
	checker      *checker.Checker

	logger *zap.SugaredLogger

	mu            sync.RWMutex
	devices       map[uuid.UUID]*Device
	serviceChecks map[uuid.UUID]*checker.ServiceCheck
	startTime     time.Time
	lastCycleMs   int64
	activePings   int
}

// NewEngine creates a new monitoring engine.
func NewEngine(
	cfg *config.Config,
	loader DeviceLoader,
	writer MetricWriter,
	publisher EventPublisher,
	svcLoader ServiceCheckLoader,
	svcWriter ServiceMetricWriter,
	svcPublisher ServiceEventPublisher,
	logger *zap.SugaredLogger,
) (*Engine, error) {
	p := NewPinger(
		cfg.Poller.PingTimeout,
		cfg.Poller.PingCount,
		cfg.Poller.PingInterval,
		cfg.Poller.Privileged,
		logger,
	)

	return &Engine{
		cfg:           cfg,
		pinger:        p,
		loader:        loader,
		writer:        writer,
		publisher:     publisher,
		svcLoader:     svcLoader,
		svcWriter:     svcWriter,
		svcPublisher:  svcPublisher,
		checker:       checker.NewChecker(logger),
		logger:        logger,
		devices:       make(map[uuid.UUID]*Device),
		serviceChecks: make(map[uuid.UUID]*checker.ServiceCheck),
		startTime:     time.Now(),
	}, nil
}

// Run starts the main monitoring loop.
func (e *Engine) Run(ctx context.Context) {
	// Start batch writers
	go e.writer.RunBatchWriter(ctx)
	go e.svcWriter.RunServiceBatchWriter(ctx)

	// Initial loads
	if err := e.syncDevices(ctx); err != nil {
		e.logger.Errorf("Initial device sync failed: %v", err)
	}
	if err := e.syncServiceChecks(ctx); err != nil {
		e.logger.Errorf("Initial service check sync failed: %v", err)
	}

	// Tickers
	syncTicker := time.NewTicker(e.cfg.Poller.DeviceSyncInterval)
	defer syncTicker.Stop()

	pingTicker := time.NewTicker(60 * time.Second)
	defer pingTicker.Stop()

	serviceCheckTicker := time.NewTicker(60 * time.Second)
	defer serviceCheckTicker.Stop()

	// Run first cycles immediately
	e.runPingCycle(ctx)
	e.runServiceCheckCycle(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-syncTicker.C:
			if err := e.syncDevices(ctx); err != nil {
				e.logger.Errorf("Device sync failed: %v", err)
			}
			if err := e.syncServiceChecks(ctx); err != nil {
				e.logger.Errorf("Service check sync failed: %v", err)
			}
		case <-pingTicker.C:
			e.runPingCycle(ctx)
		case <-serviceCheckTicker.C:
			e.runServiceCheckCycle(ctx)
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
		Status:            "ok",
		PollerID:          e.cfg.Poller.ID,
		DeviceCount:       len(e.devices),
		ServiceCheckCount: len(e.serviceChecks),
		ActivePings:       e.activePings,
		Uptime:            time.Since(e.startTime).String(),
		LastCycleMs:       e.lastCycleMs,
	}
}

// --- Device Ping Logic (unchanged) ---

func (e *Engine) syncDevices(ctx context.Context) error {
	devices, err := e.loader.LoadDevices(ctx)
	if err != nil {
		return err
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	seen := make(map[uuid.UUID]bool)
	for _, d := range devices {
		seen[d.ID] = true
		if existing, ok := e.devices[d.ID]; ok {
			d.DownCount = existing.DownCount
			d.Status = existing.Status
		}
		e.devices[d.ID] = d
	}

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

	for _, result := range results {
		e.writer.WriteResult(result)

		if err := e.publisher.PublishMetric(ctx, result); err != nil {
			e.logger.Debugf("Failed to publish metric: %v", err)
		}

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

		rttMs := float64(result.RTT.Microseconds()) / 1000.0
		go func() {
			if err := e.loader.UpdateDeviceStatus(ctx, device.ID, newStatus, result.Timestamp, rttMs); err != nil {
				e.logger.Errorf("Failed to update device status in PG: %v", err)
			}
		}()

		go func() {
			if err := e.writer.WriteStatusChange(ctx, sc, 0); err != nil {
				e.logger.Errorf("Failed to write status change to CH: %v", err)
			}
		}()

		go func() {
			e.evaluateAlerts(ctx, device, oldStatus, newStatus, result)
		}()

		go func() {
			if err := e.publisher.PublishStatusChange(ctx, sc); err != nil {
				e.logger.Errorf("Failed to publish status change: %v", err)
			}
		}()
	}
}

func (e *Engine) evaluateAlerts(ctx context.Context, device *Device, oldStatus, newStatus string, result *PingResult) {
	apiURL := "http://localhost:8000/api/v1/alert-engine/evaluate"

	payload := map[string]interface{}{
		"device_id":   device.ID.String(),
		"hostname":    device.Hostname,
		"ip_address":  device.IPAddress.String(),
		"old_status":  oldStatus,
		"new_status":  newStatus,
		"rtt_ms":      float64(result.RTT.Microseconds()) / 1000.0,
		"packet_loss": result.PacketLoss,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		e.logger.Errorf("Failed to marshal alert payload: %v", err)
		return
	}

	req, err := http.NewRequestWithContext(ctx, "POST", apiURL, bytes.NewReader(body))
	if err != nil {
		e.logger.Errorf("Failed to create alert request: %v", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		e.logger.Errorf("Failed to call alert engine: %v", err)
		return
	}
	defer resp.Body.Close()

	var result2 map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result2)

	sent := result2["notifications_sent"]
	e.logger.Infof("Alert evaluation: %s %s→%s, notifications sent: %v", device.Hostname, oldStatus, newStatus, sent)
}

// --- Service Check Logic ---

func (e *Engine) syncServiceChecks(ctx context.Context) error {
	checks, err := e.svcLoader.LoadServiceChecks(ctx)
	if err != nil {
		return err
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	seen := make(map[uuid.UUID]bool)
	for _, sc := range checks {
		seen[sc.ID] = true
		if existing, ok := e.serviceChecks[sc.ID]; ok {
			sc.DownCount = existing.DownCount
			sc.Status = existing.Status
		}
		e.serviceChecks[sc.ID] = sc
	}

	for id := range e.serviceChecks {
		if !seen[id] {
			delete(e.serviceChecks, id)
		}
	}

	e.logger.Infof("Service check sync complete: %d checks loaded", len(e.serviceChecks))
	return nil
}

func (e *Engine) runServiceCheckCycle(ctx context.Context) {
	e.mu.RLock()
	checkList := make([]*checker.ServiceCheck, 0, len(e.serviceChecks))
	for _, sc := range e.serviceChecks {
		if sc.Enabled {
			checkList = append(checkList, sc)
		}
	}
	e.mu.RUnlock()

	if len(checkList) == 0 {
		return
	}

	e.logger.Infof("Starting service check cycle for %d checks", len(checkList))
	start := time.Now()

	maxWorkers := 50
	if len(checkList) < maxWorkers {
		maxWorkers = len(checkList)
	}

	results := e.checker.CheckBatch(ctx, checkList, e.cfg.Poller.ID, maxWorkers)

	e.logger.Infof("Service check cycle complete: %d results in %dms", len(results), time.Since(start).Milliseconds())

	for _, result := range results {
		// Write to ClickHouse
		e.svcWriter.WriteServiceResult(result)

		// Publish to Redis
		if err := e.svcPublisher.PublishServiceMetric(ctx, result); err != nil {
			e.logger.Debugf("Failed to publish service metric: %v", err)
		}

		// Process status change
		e.processServiceStatusChange(ctx, result)
	}
}

func (e *Engine) processServiceStatusChange(ctx context.Context, result *checker.ServiceCheckResult) {
	e.mu.Lock()
	defer e.mu.Unlock()

	sc, ok := e.serviceChecks[result.ServiceCheckID]
	if !ok {
		return
	}

	oldStatus := sc.Status
	var newStatus string

	if !result.IsUp {
		sc.DownCount++
		if sc.DownCount >= e.cfg.Poller.DownThreshold {
			newStatus = "down"
		} else {
			// Not yet confirmed down — still update PG with latest result
			responseMs := float64(result.ResponseTime.Microseconds()) / 1000.0
			go func() {
				e.svcLoader.UpdateServiceCheckStatus(ctx, sc.ID, oldStatus, result.Timestamp, responseMs, result.Error,
					result.TLSExpiry, result.TLSDaysRemaining, result.TLSIssuer, result.TLSSubject)
			}()
			return
		}
	} else {
		sc.DownCount = 0

		// TLS-specific status: warning if cert expiring soon
		if sc.CheckType == "tls" && result.TLSDaysRemaining != nil {
			days := *result.TLSDaysRemaining
			if days <= sc.TLSCriticalDays {
				newStatus = "down"
			} else if days <= sc.TLSWarnDays {
				newStatus = "warning"
			} else {
				newStatus = "up"
			}
		} else {
			newStatus = "up"
		}
	}

	// Update PG with latest state
	responseMs := float64(result.ResponseTime.Microseconds()) / 1000.0
	go func() {
		if err := e.svcLoader.UpdateServiceCheckStatus(ctx, sc.ID, newStatus, result.Timestamp, responseMs, result.Error,
			result.TLSExpiry, result.TLSDaysRemaining, result.TLSIssuer, result.TLSSubject); err != nil {
			e.logger.Errorf("Failed to update service check status in PG: %v", err)
		}
	}()

	if newStatus != oldStatus {
		sc.Status = newStatus

		reason := ""
		switch {
		case newStatus == "down" && sc.CheckType == "tls":
			reason = "Certificate expired or critically close to expiry"
		case newStatus == "down":
			reason = fmt.Sprintf("Service check failed: %s", result.Error)
		case newStatus == "warning":
			if result.TLSDaysRemaining != nil {
				reason = fmt.Sprintf("TLS certificate expires in %d days", *result.TLSDaysRemaining)
			} else {
				reason = "Service degraded"
			}
		case newStatus == "up":
			reason = "Service check passing"
		}

		ssc := &checker.ServiceStatusChange{
			ServiceCheckID: sc.ID,
			DeviceID:       sc.DeviceID,
			CheckType:      sc.CheckType,
			OldStatus:      oldStatus,
			NewStatus:      newStatus,
			Reason:         reason,
			Timestamp:      time.Now().UTC(),
		}

		e.logger.Infof("Service status change: %s (%s) %s → %s: %s",
			sc.Name, sc.CheckType, oldStatus, newStatus, reason)

		go func() {
			if err := e.svcWriter.WriteServiceStatusChange(ctx, ssc, 0); err != nil {
				e.logger.Errorf("Failed to write service status change to CH: %v", err)
			}
		}()

		go func() {
			if err := e.svcPublisher.PublishServiceStatusChange(ctx, ssc); err != nil {
				e.logger.Errorf("Failed to publish service status change: %v", err)
			}
		}()
	}
}
