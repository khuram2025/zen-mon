package pinger

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/checker/snmp"
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

// SNMPLoader loads SNMP-enabled devices with decrypted credentials
// and writes discovery output back to Postgres.
type SNMPLoader interface {
	LoadSNMPDevices(ctx context.Context) ([]*snmp.Device, error)
	UpsertSystemInfo(ctx context.Context, deviceID uuid.UUID, sysObjectID, vendor, model, osVersion string) error
	UpsertInterfaces(ctx context.Context, deviceID uuid.UUID, ifs []snmp.Interface) error
	UpsertEntities(ctx context.Context, deviceID uuid.UUID, ents []snmp.Entity) error
	UpsertSensors(ctx context.Context, deviceID uuid.UUID, sensors []snmp.Sensor) error
	UpsertProfile(ctx context.Context, p *snmp.Profile) error
	AssignProfileIfUnset(ctx context.Context, deviceID, profileID uuid.UUID) error
}

// SNMPMetricWriter persists SNMP metrics to the time-series store.
type SNMPMetricWriter interface {
	WriteSNMPMetric(m snmp.MetricSample)
	WriteSNMPIfMetric(m snmp.InterfaceSample)
	RunSNMPBatchWriter(ctx context.Context)
	WriteTrap(t snmp.TrapRecord)
	RunTrapBatchWriter(ctx context.Context)
}

// SNMPDeviceLookup resolves a source IP to a device UUID.
type SNMPDeviceLookup interface {
	LookupDeviceByIP(ctx context.Context, ip net.IP) (uuid.UUID, bool)
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

	// SNMP
	snmpLoader    SNMPLoader
	snmpWriter    SNMPMetricWriter
	snmpLookup    SNMPDeviceLookup
	snmpCollector *snmp.Collector
	snmpSessions  *snmp.SessionCache
	snmpClassifier *snmp.Classifier
	snmpTrapListener *snmp.TrapListener

	logger *zap.SugaredLogger

	mu            sync.RWMutex
	devices       map[uuid.UUID]*Device
	serviceChecks map[uuid.UUID]*checker.ServiceCheck
	snmpDevices   map[uuid.UUID]*snmp.Device
	startTime     time.Time
	lastCycleMs   int64
	activePings   int
	snmpActive    int
	snmpRunning   bool
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
	snmpLoader SNMPLoader,
	snmpWriter SNMPMetricWriter,
	snmpLookup SNMPDeviceLookup,
	logger *zap.SugaredLogger,
) (*Engine, error) {
	p := NewPinger(
		cfg.Poller.PingTimeout,
		cfg.Poller.PingCount,
		cfg.Poller.PingInterval,
		cfg.Poller.Privileged,
		logger,
	)

	sessions := snmp.NewSessionCache()
	collector := snmp.NewCollector(cfg.Poller.ID, sessions)
	classifier := snmp.NewClassifier()

	return &Engine{
		cfg:            cfg,
		pinger:         p,
		loader:         loader,
		writer:         writer,
		publisher:      publisher,
		svcLoader:      svcLoader,
		svcWriter:      svcWriter,
		svcPublisher:   svcPublisher,
		checker:        checker.NewChecker(logger),
		snmpLoader:     snmpLoader,
		snmpWriter:     snmpWriter,
		snmpLookup:     snmpLookup,
		snmpCollector:  collector,
		snmpSessions:   sessions,
		snmpClassifier: classifier,
		logger:        logger,
		devices:       make(map[uuid.UUID]*Device),
		serviceChecks: make(map[uuid.UUID]*checker.ServiceCheck),
		snmpDevices:   make(map[uuid.UUID]*snmp.Device),
		startTime:     time.Now(),
	}, nil
}

// Run starts the main monitoring loop.
func (e *Engine) Run(ctx context.Context) {
	// Start batch writers
	go e.writer.RunBatchWriter(ctx)
	go e.svcWriter.RunServiceBatchWriter(ctx)
	go e.snmpWriter.RunSNMPBatchWriter(ctx)
	go e.snmpWriter.RunTrapBatchWriter(ctx)

	// Load built-in SNMP profile packs and seed them into Postgres.
	// Failures here are non-fatal — devices still poll, just without
	// classification.
	if err := e.seedSNMPProfiles(ctx); err != nil {
		e.logger.Warnf("SNMP profile seed failed: %v", err)
	}

	// Start the trap listener. Binding UDP/162 requires CAP_NET_BIND_SERVICE
	// (set on the poller binary by the installer) — if it fails we log and
	// continue, so ping polling is unaffected.
	trapBind := os.Getenv("SNMP_TRAP_BIND")
	if trapBind == "" {
		trapBind = "0.0.0.0:162"
	}
	e.snmpTrapListener = snmp.NewTrapListener(
		e.cfg.Poller.ID, trapBind, e.snmpWriter, e.snmpLookup, e.logger,
	)
	if err := e.snmpTrapListener.Start(ctx); err != nil {
		e.logger.Warnf("SNMP trap listener not started: %v", err)
	}

	// Initial loads
	if err := e.syncDevices(ctx); err != nil {
		e.logger.Errorf("Initial device sync failed: %v", err)
	}
	if err := e.syncServiceChecks(ctx); err != nil {
		e.logger.Errorf("Initial service check sync failed: %v", err)
	}
	if err := e.syncSNMPDevices(ctx); err != nil {
		e.logger.Errorf("Initial SNMP device sync failed: %v", err)
	}

	// Tickers
	syncTicker := time.NewTicker(e.cfg.Poller.DeviceSyncInterval)
	defer syncTicker.Stop()

	pingTicker := time.NewTicker(60 * time.Second)
	defer pingTicker.Stop()

	serviceCheckTicker := time.NewTicker(60 * time.Second)
	defer serviceCheckTicker.Stop()

	// SNMP runs on its own ticker so that a slow table walk never
	// pushes ping cycles out of schedule. 60 s matches the default
	// poll interval; per-device intervals are honored inside
	// runSNMPCycle via a next-due map.
	snmpTicker := time.NewTicker(30 * time.Second)
	defer snmpTicker.Stop()

	// Run first cycles immediately
	e.runPingCycle(ctx)
	e.runServiceCheckCycle(ctx)
	go e.runSNMPCycle(ctx)

	for {
		select {
		case <-ctx.Done():
			if e.snmpTrapListener != nil {
				e.snmpTrapListener.Close()
			}
			e.snmpSessions.Close()
			return
		case <-syncTicker.C:
			if err := e.syncDevices(ctx); err != nil {
				e.logger.Errorf("Device sync failed: %v", err)
			}
			if err := e.syncServiceChecks(ctx); err != nil {
				e.logger.Errorf("Service check sync failed: %v", err)
			}
			if err := e.syncSNMPDevices(ctx); err != nil {
				e.logger.Errorf("SNMP device sync failed: %v", err)
			}
		case <-pingTicker.C:
			e.runPingCycle(ctx)
		case <-serviceCheckTicker.C:
			e.runServiceCheckCycle(ctx)
		case <-snmpTicker.C:
			go e.runSNMPCycle(ctx)
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
		SNMPDeviceCount:   len(e.snmpDevices),
		ActivePings:       e.activePings,
		ActiveSNMP:        e.snmpActive,
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

// --- SNMP Logic ---

func (e *Engine) syncSNMPDevices(ctx context.Context) error {
	devices, err := e.snmpLoader.LoadSNMPDevices(ctx)
	if err != nil {
		return err
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	seen := make(map[uuid.UUID]bool, len(devices))
	for _, d := range devices {
		seen[d.ID] = true
		e.snmpDevices[d.ID] = d
	}
	for id := range e.snmpDevices {
		if !seen[id] {
			delete(e.snmpDevices, id)
			e.snmpSessions.Drop(id)
		}
	}
	e.logger.Infof("SNMP device sync complete: %d devices loaded", len(e.snmpDevices))
	return nil
}

// runSNMPCycle polls all SNMP devices whose next-due time has
// elapsed. Workers are bounded so that a large fleet cannot starve
// the ping pool — 200 concurrent SNMP sessions by default.
//
// Only one cycle runs at a time: sessions are cached per device and
// gosnmp.GoSNMP is not safe for concurrent use on the same session,
// so overlapping cycles would race on reused sessions.
func (e *Engine) runSNMPCycle(ctx context.Context) {
	e.mu.Lock()
	if e.snmpRunning {
		e.mu.Unlock()
		e.logger.Debugf("SNMP cycle skipped: previous cycle still running")
		return
	}
	e.snmpRunning = true
	devices := make([]*snmp.Device, 0, len(e.snmpDevices))
	for _, d := range e.snmpDevices {
		if d.Enabled {
			devices = append(devices, d)
		}
	}
	e.mu.Unlock()

	defer func() {
		e.mu.Lock()
		e.snmpRunning = false
		e.mu.Unlock()
	}()

	if len(devices) == 0 {
		return
	}

	maxWorkers := 200
	if len(devices) < maxWorkers {
		maxWorkers = len(devices)
	}

	e.mu.Lock()
	e.snmpActive = len(devices)
	e.mu.Unlock()

	e.logger.Infof("Starting SNMP cycle for %d devices (workers=%d)", len(devices), maxWorkers)
	start := time.Now()

	sem := make(chan struct{}, maxWorkers)
	var wg sync.WaitGroup
	var okCount, errCount int
	var countMu sync.Mutex

	// Hard per-device budget — no single unreachable device can stall
	// the cycle longer than this.
	const devBudget = 20 * time.Second

	for _, d := range devices {
		select {
		case <-ctx.Done():
			break
		default:
		}
		wg.Add(1)
		sem <- struct{}{}
		go func(d *snmp.Device) {
			defer wg.Done()
			defer func() { <-sem }()
			// gosnmp ignores context, so run Collect in a nested
			// goroutine and race it against devBudget. A runaway
			// goroutine will still finish eventually (bounded by
			// gosnmp Timeout * (1+Retries) * #collectors), but the
			// cycle report will not wait for it.
			done := make(chan *snmp.Result, 1)
			go func() { done <- e.snmpCollector.Collect(ctx, d) }()
			var res *snmp.Result
			select {
			case res = <-done:
			case <-time.After(devBudget):
				res = &snmp.Result{DeviceID: d.ID, Err: fmt.Errorf("device budget %s exceeded", devBudget)}
			case <-ctx.Done():
				res = &snmp.Result{DeviceID: d.ID, Err: ctx.Err()}
			}
			e.handleSNMPResult(ctx, d, res)
			countMu.Lock()
			if res.Err == nil {
				okCount++
			} else {
				errCount++
			}
			countMu.Unlock()
		}(d)
	}
	wg.Wait()

	e.mu.Lock()
	e.snmpActive = 0
	e.mu.Unlock()

	e.logger.Infof("SNMP cycle complete: %d ok, %d errors in %dms",
		okCount, errCount, time.Since(start).Milliseconds())
}

func (e *Engine) handleSNMPResult(ctx context.Context, d *snmp.Device, r *snmp.Result) {
	if r.Err != nil {
		e.logger.Warnf("SNMP poll failed for %s (%s): %v", d.Hostname, d.IPAddress, r.Err)
		return
	}

	// 1) discovery writeback — system info, interfaces, entities, sensors
	if r.System != nil {
		// Classify based on sysObjectID + sysDescr.
		vendor, model, osVersion := "", "", ""
		if prof := e.snmpClassifier.Match(r.System.SysObjectID, r.System.SysDescr); prof != nil {
			v, m, o := e.snmpClassifier.Extract(prof, r.System.SysDescr)
			vendor, model, osVersion = v, m, o
			if prof.ID != uuid.Nil {
				if err := e.snmpLoader.AssignProfileIfUnset(ctx, d.ID, prof.ID); err != nil {
					e.logger.Warnf("AssignProfile %s → %s: %v", d.Hostname, prof.Name, err)
				}
			}
		}
		if err := e.snmpLoader.UpsertSystemInfo(ctx, d.ID, r.System.SysObjectID, vendor, model, osVersion); err != nil {
			e.logger.Warnf("UpsertSystemInfo %s: %v", d.Hostname, err)
		}
	}
	if len(r.Interfaces) > 0 {
		if err := e.snmpLoader.UpsertInterfaces(ctx, d.ID, r.Interfaces); err != nil {
			e.logger.Warnf("UpsertInterfaces %s (%d rows): %v", d.Hostname, len(r.Interfaces), err)
		}
	}
	if len(r.Entities) > 0 {
		if err := e.snmpLoader.UpsertEntities(ctx, d.ID, r.Entities); err != nil {
			e.logger.Warnf("UpsertEntities %s (%d rows): %v", d.Hostname, len(r.Entities), err)
		}
	}
	if len(r.Sensors) > 0 {
		if err := e.snmpLoader.UpsertSensors(ctx, d.ID, r.Sensors); err != nil {
			e.logger.Warnf("UpsertSensors %s (%d rows): %v", d.Hostname, len(r.Sensors), err)
		}
	}

	// 2) ClickHouse time-series writes
	for _, m := range r.Scalars {
		e.snmpWriter.WriteSNMPMetric(m)
	}
	for _, m := range r.IfSamples {
		e.snmpWriter.WriteSNMPIfMetric(m)
	}
}

// seedSNMPProfiles loads built-in profile packs from disk and upserts
// them into device_profiles. Runs once at startup. The directory can
// be overridden via SNMP_PROFILES_DIR for dev / testing.
func (e *Engine) seedSNMPProfiles(ctx context.Context) error {
	dir := os.Getenv("SNMP_PROFILES_DIR")
	if dir == "" {
		dir = "/opt/zenplus/data/profiles"
	}
	profiles, loadErrs := e.snmpClassifier.LoadFromDir(dir)
	for _, le := range loadErrs {
		e.logger.Warnf("profile load: %v", le)
	}
	if len(profiles) == 0 {
		return fmt.Errorf("no profiles loaded from %s", dir)
	}
	for _, p := range profiles {
		if err := e.snmpLoader.UpsertProfile(ctx, p); err != nil {
			e.logger.Warnf("upsert profile %s: %v", p.Name, err)
			continue
		}
	}
	e.logger.Infof("SNMP profile seed complete: %d profiles from %s", len(profiles), dir)
	return nil
}
