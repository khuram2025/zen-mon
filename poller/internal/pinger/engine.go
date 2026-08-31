package pinger

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
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
	LoadActiveMaintenanceDeviceIDs(ctx context.Context) (map[uuid.UUID]struct{}, error)
	// LoadDegradedThresholds returns the admin-set degraded RTT (ms) and
	// packet-loss (%) thresholds; zeros mean unset (keep config defaults).
	LoadDegradedThresholds(ctx context.Context) (float64, float64, error)
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
	LoadActiveMaintenanceCheckIDs(ctx context.Context) (map[uuid.UUID]struct{}, error)
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
	UpsertSystemInfo(ctx context.Context, deviceID uuid.UUID, sysObjectID, vendor, model, osVersion, sysName string) error
	UpsertInterfaces(ctx context.Context, deviceID uuid.UUID, ifs []snmp.Interface) error
	UpsertEntities(ctx context.Context, deviceID uuid.UUID, ents []snmp.Entity) error
	UpsertSensors(ctx context.Context, deviceID uuid.UUID, sensors []snmp.Sensor) error
	UpsertProfile(ctx context.Context, p *snmp.Profile) error
	LoadProfiles(ctx context.Context) ([]*snmp.Profile, error)
	AssignProfileIfUnset(ctx context.Context, deviceID, profileID uuid.UUID) error
	UpsertUdtData(ctx context.Context, deviceID uuid.UUID, u *snmp.UdtData) error
	LoadUdtGlobalInterval(ctx context.Context) (int, error)
	UpsertTemplateValues(ctx context.Context, deviceID uuid.UUID, vals []snmp.TemplateValue, polledGroups []string) error
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
	snmpLoader       SNMPLoader
	snmpWriter       SNMPMetricWriter
	snmpLookup       SNMPDeviceLookup
	snmpCollector    *snmp.Collector
	snmpSessions     *snmp.SessionCache
	snmpClassifier   *snmp.Classifier
	snmpTrapListener *snmp.TrapListener

	logger *zap.SugaredLogger

	mu            sync.RWMutex
	devices       map[uuid.UUID]*Device
	deviceMaint   map[uuid.UUID]struct{} // devices inside an active maintenance window
	serviceChecks map[uuid.UUID]*checker.ServiceCheck
	snmpDevices   map[uuid.UUID]*snmp.Device
	lastPingAt    map[uuid.UUID]time.Time
	lastServiceAt map[uuid.UUID]time.Time
	lastUdtAt     map[uuid.UUID]time.Time
	udtInterval   time.Duration
	// Effective degraded thresholds: config defaults, overridden by the
	// admin-set values in system_settings (key 'monitoring'). Written under
	// e.mu in syncDevices, read under e.mu in processStatusChange.
	degradedRTTMs   float64
	degradedLossPct float64
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
		logger:         logger,
		devices:        make(map[uuid.UUID]*Device),
		serviceChecks:  make(map[uuid.UUID]*checker.ServiceCheck),
		snmpDevices:    make(map[uuid.UUID]*snmp.Device),
		lastPingAt:     make(map[uuid.UUID]time.Time),
		lastServiceAt:  make(map[uuid.UUID]time.Time),
		lastUdtAt:       make(map[uuid.UUID]time.Time),
		udtInterval:     udtIntervalFromEnv(),
		degradedRTTMs:   cfg.Poller.DegradedRTTMs,
		degradedLossPct: cfg.Poller.DegradedLossPct,
		startTime:       time.Now(),
	}, nil
}

// udtIntervalFromEnv returns the UDT (MAC/ARP/LLDP) collection cadence.
// Default 5 minutes — an order of magnitude fresher than the 30-minute
// industry norm, while staying gentle on switch CPUs.
func udtIntervalFromEnv() time.Duration {
	if v := os.Getenv("UDT_POLL_INTERVAL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 30 {
			return time.Duration(n) * time.Second
		}
	}
	return 5 * time.Minute
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
		e.cfg.Poller.ID, trapBind, &trapAlertSink{inner: e.snmpWriter, engine: e}, e.snmpLookup, e.logger,
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

	pingTicker := time.NewTicker(1 * time.Second)
	defer pingTicker.Stop()

	serviceCheckTicker := time.NewTicker(1 * time.Second)
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

	// Admin-set degraded thresholds ride the same sync cadence. On error the
	// previous values are kept — never clobbered with the defaults.
	rttMs, lossPct, thrErr := e.loader.LoadDegradedThresholds(ctx)

	e.mu.Lock()
	defer e.mu.Unlock()

	if thrErr != nil {
		e.logger.Warnf("Failed to load degraded thresholds: %v", thrErr)
	} else {
		newRTT, newLoss := e.cfg.Poller.DegradedRTTMs, e.cfg.Poller.DegradedLossPct
		if rttMs > 0 {
			newRTT = rttMs
		}
		if lossPct > 0 {
			newLoss = lossPct
		}
		if newRTT != e.degradedRTTMs || newLoss != e.degradedLossPct {
			e.logger.Infof("Degraded thresholds now rtt>%.0fms or loss>%.0f%%", newRTT, newLoss)
		}
		e.degradedRTTMs, e.degradedLossPct = newRTT, newLoss
	}

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
			delete(e.lastPingAt, id)
		}
	}

	e.logger.Infof("Device sync complete: %d devices loaded", len(e.devices))
	return nil
}

func (e *Engine) runPingCycle(ctx context.Context) {
	now := time.Now()
	e.mu.RLock()
	deviceList := make([]*Device, 0, len(e.devices))
	for _, d := range e.devices {
		last := e.lastPingAt[d.ID]
		interval := effectiveInterval(d.PingInterval, 60*time.Second)
		if d.PingEnabled && due(now, last, interval) {
			deviceList = append(deviceList, d)
		}
	}
	e.mu.RUnlock()

	if len(deviceList) == 0 {
		return
	}

	// Active device maintenance windows — processStatusChange uses this set
	// to mute transitions/alerting while metrics keep flowing. On error the
	// previous set is kept rather than clobbered with an empty one.
	if maint, err := e.loader.LoadActiveMaintenanceDeviceIDs(ctx); err != nil {
		e.logger.Warnf("Failed to load device maintenance ids: %v", err)
	} else {
		e.mu.Lock()
		e.deviceMaint = maint
		e.mu.Unlock()
	}

	e.logger.Infof("Starting ping cycle for %d devices", len(deviceList))
	start := time.Now()

	e.mu.Lock()
	e.activePings = len(deviceList)
	for _, d := range deviceList {
		e.lastPingAt[d.ID] = now
		d.LastPingAt = now
	}
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

	_, inMaint := e.deviceMaint[result.DeviceID]

	oldStatus := device.Status
	var newStatus string

	if !result.IsUp {
		device.DownCount++
		if device.DownCount >= e.cfg.Poller.DownThreshold {
			newStatus = "down"
		} else {
			// Not confirmed down yet — but still flag a freshly opened
			// maintenance window so the UI shows it immediately.
			if inMaint && oldStatus != "maintenance" {
				e.enterMaintenance(ctx, device, oldStatus, result)
			}
			return
		}
	} else {
		device.DownCount = 0
		rttMs := float64(result.RTT.Microseconds()) / 1000.0

		if rttMs > e.degradedRTTMs || result.PacketLoss > float32(e.degradedLossPct)/100.0 {
			newStatus = "degraded"
		} else {
			newStatus = "up"
		}

		device.LastSeen = result.Timestamp
		device.LastRTT = rttMs

		// While a maintenance window is active the stored status stays
		// pinned to 'maintenance'; last_seen/rtt still refresh.
		statusForPG := newStatus
		if inMaint {
			statusForPG = "maintenance"
		}
		go func() {
			if err := e.loader.UpdateDeviceStatus(ctx, device.ID, statusForPG, result.Timestamp, rttMs); err != nil {
				e.logger.Errorf("Failed to update device last_seen in PG: %v", err)
			}
		}()
	}

	if inMaint {
		// Maintenance mute (mirrors the service-check behavior): metrics were
		// already written above, DownCount keeps counting so the post-window
		// state is confirmed immediately, but there are no transitions and no
		// alert evaluation while the window is active.
		if oldStatus != "maintenance" {
			e.enterMaintenance(ctx, device, oldStatus, result)
		}
		return
	}

	// Note: when a window ends, oldStatus is 'maintenance' and the normal
	// path below transitions to the real status — including alerting, so a
	// device that is still down after maintenance pages right away.
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

// enterMaintenance transitions a device into 'maintenance': one status-log
// row, a PG status write and a realtime publish — and deliberately NO alert
// evaluation. Caller must hold e.mu.
func (e *Engine) enterMaintenance(ctx context.Context, device *Device, oldStatus string, result *PingResult) {
	device.Status = "maintenance"

	sc := &StatusChange{
		DeviceID:  device.ID,
		OldStatus: oldStatus,
		NewStatus: "maintenance",
		Reason:    "Maintenance window active",
		Timestamp: time.Now().UTC(),
	}

	e.logger.Infof("Status change: %s (%s) %s → maintenance (alerting muted)",
		device.Hostname, device.IPAddress, oldStatus)

	rttMs := float64(result.RTT.Microseconds()) / 1000.0
	go func() {
		if err := e.loader.UpdateDeviceStatus(ctx, device.ID, "maintenance", result.Timestamp, rttMs); err != nil {
			e.logger.Errorf("Failed to update device status in PG: %v", err)
		}
	}()

	go func() {
		if err := e.writer.WriteStatusChange(ctx, sc, 0); err != nil {
			e.logger.Errorf("Failed to write status change to CH: %v", err)
		}
	}()

	go func() {
		if err := e.publisher.PublishStatusChange(ctx, sc); err != nil {
			e.logger.Errorf("Failed to publish status change: %v", err)
		}
	}()
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

// trapAlertSink wraps the metrics-store trap sink so that every received trap
// is ALSO handed to the API alert engine for trap-rule evaluation, without
// disturbing the existing ClickHouse persistence path.
type trapAlertSink struct {
	inner  snmp.TrapSink
	engine *Engine
}

func (s *trapAlertSink) WriteTrap(t snmp.TrapRecord) {
	if s.inner != nil {
		s.inner.WriteTrap(t)
	}
	if s.engine != nil {
		go s.engine.evaluateTrapAlert(t)
	}
}

// evaluateTrapAlert posts a received SNMP trap to the API alert engine, which
// fires any matching metric='trap' alert rules (alerts-only — no channel
// dispatch). Best-effort: failures are logged and ignored so trap persistence
// is never blocked.
func (e *Engine) evaluateTrapAlert(t snmp.TrapRecord) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	deviceID := ""
	if t.DeviceID != nil {
		deviceID = t.DeviceID.String()
	}
	payload := map[string]interface{}{
		"device_id": deviceID,
		"source_ip": t.SourceIP.String(),
		"trap_oid":  t.TrapOID,
		"trap_name": t.TrapName,
		"severity":  t.Severity,
		"message":   t.Message,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	req, err := http.NewRequestWithContext(ctx, "POST",
		"http://localhost:8000/api/v1/alert-engine/evaluate-trap", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		e.logger.Warnf("trap alert eval failed: %v", err)
		return
	}
	defer resp.Body.Close()
	var r map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&r)
	e.logger.Infof("trap alert eval: oid=%s alerts_created=%v", t.TrapOID, r["alerts_created"])
}

// evaluateServiceAlerts fires the API-side alert engine when a service check
// transitions status (and is not muted by a maintenance window — that's
// already filtered upstream in processServiceStatusChange).
func (e *Engine) evaluateServiceAlerts(ctx context.Context, sc *checker.ServiceCheck, oldStatus, newStatus string, result *checker.ServiceCheckResult) {
	apiURL := "http://localhost:8000/api/v1/alert-engine/evaluate-service"

	payload := map[string]interface{}{
		"service_check_id": sc.ID.String(),
		"check_name":       sc.Name,
		"check_type":       sc.CheckType,
		"old_status":       oldStatus,
		"new_status":       newStatus,
		"response_ms":      float64(result.ResponseTime.Microseconds()) / 1000.0,
		"error":            result.Error,
		"tags":             sc.Tags,
	}
	if sc.DeviceID != nil {
		payload["device_id"] = sc.DeviceID.String()
	}
	if sc.GroupID != nil {
		payload["group_id"] = sc.GroupID.String()
	}
	if sc.TargetURL != "" {
		payload["target"] = sc.TargetURL
	} else if sc.TargetHost != "" && sc.TargetPort > 0 {
		payload["target"] = fmt.Sprintf("%s:%d", sc.TargetHost, sc.TargetPort)
	} else {
		payload["target"] = sc.TargetHost
	}

	body, err := json.Marshal(payload)
	if err != nil {
		e.logger.Errorf("Failed to marshal service alert payload: %v", err)
		return
	}

	req, err := http.NewRequestWithContext(ctx, "POST", apiURL, bytes.NewReader(body))
	if err != nil {
		e.logger.Errorf("Failed to create service alert request: %v", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		e.logger.Errorf("Failed to call service alert engine: %v", err)
		return
	}
	defer resp.Body.Close()

	var result2 map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result2)
	sent := result2["notifications_sent"]
	e.logger.Infof("Service alert evaluation: %s %s→%s, notifications sent: %v",
		sc.Name, oldStatus, newStatus, sent)
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
			delete(e.lastServiceAt, id)
		}
	}

	e.logger.Infof("Service check sync complete: %d checks loaded", len(e.serviceChecks))
	return nil
}

func (e *Engine) runServiceCheckCycle(ctx context.Context) {
	// Snapshot enabled checks + the current parent-status map under the
	// read lock, then filter out children whose parent is currently DOWN
	// (parent-dependency suppression — avoids alert storms when an upstream
	// fails and all its dependents would otherwise page simultaneously).
	now := time.Now()
	e.mu.RLock()
	parentStatus := map[uuid.UUID]string{}
	for id, sc := range e.serviceChecks {
		parentStatus[id] = sc.Status
	}

	checkList := make([]*checker.ServiceCheck, 0, len(e.serviceChecks))
	skipped := 0
	for _, sc := range e.serviceChecks {
		if !sc.Enabled {
			continue
		}
		last := e.lastServiceAt[sc.ID]
		interval := effectiveInterval(sc.CheckInterval, 60*time.Second)
		if !due(now, last, interval) {
			continue
		}
		if sc.ParentCheckID != nil {
			if ps, ok := parentStatus[*sc.ParentCheckID]; ok && ps == "down" {
				skipped++
				continue
			}
		}
		checkList = append(checkList, sc)
	}
	e.mu.RUnlock()

	if len(checkList) == 0 {
		return
	}

	// Load active maintenance windows once per cycle — used below to
	// suppress status transitions for muted checks.
	maintIDs, err := e.svcLoader.LoadActiveMaintenanceCheckIDs(ctx)
	if err != nil {
		e.logger.Warnf("Failed to load maintenance window ids: %v", err)
		maintIDs = map[uuid.UUID]struct{}{}
	}

	e.logger.Infof("Starting service check cycle for %d checks (%d skipped via dependency, %d in maintenance)",
		len(checkList), skipped, len(maintIDs))
	start := time.Now()

	e.mu.Lock()
	for _, sc := range checkList {
		e.lastServiceAt[sc.ID] = now
		sc.LastCheckAt = now
	}
	e.mu.Unlock()

	maxWorkers := 50
	if len(checkList) < maxWorkers {
		maxWorkers = len(checkList)
	}

	results := e.checker.CheckBatch(ctx, checkList, e.cfg.Poller.ID, maxWorkers)

	e.logger.Infof("Service check cycle complete: %d results in %dms", len(results), time.Since(start).Milliseconds())

	for _, result := range results {
		// Write to ClickHouse (always — SLA stays accurate even in maintenance)
		e.svcWriter.WriteServiceResult(result)

		// Publish to Redis
		if err := e.svcPublisher.PublishServiceMetric(ctx, result); err != nil {
			e.logger.Debugf("Failed to publish service metric: %v", err)
		}

		// Process status change (with maintenance mute)
		e.processServiceStatusChange(ctx, result, maintIDs)
	}
}

func effectiveInterval(configured time.Duration, fallback time.Duration) time.Duration {
	if configured > 0 {
		return configured
	}
	return fallback
}

func due(now time.Time, last time.Time, interval time.Duration) bool {
	return last.IsZero() || now.Sub(last) >= interval
}

func (e *Engine) processServiceStatusChange(ctx context.Context, result *checker.ServiceCheckResult, maintIDs map[uuid.UUID]struct{}) {
	e.mu.Lock()
	defer e.mu.Unlock()

	sc, ok := e.serviceChecks[result.ServiceCheckID]
	if !ok {
		return
	}

	oldStatus := sc.Status
	var newStatus string

	// Per-check retry threshold: use sc.RetryCount, fall back to engine default.
	retryThreshold := sc.RetryCount
	if retryThreshold < 1 {
		retryThreshold = e.cfg.Poller.DownThreshold
	}

	if !result.IsUp {
		sc.DownCount++
		if sc.DownCount >= retryThreshold {
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

		// Maintenance mute: still track the new in-memory status so the UI
		// reflects reality, but don't log the transition (no alert firing,
		// no status_log row) while a window is active.
		if _, muted := maintIDs[sc.ID]; muted {
			e.logger.Infof("Service status change suppressed (in maintenance): %s %s → %s",
				sc.Name, oldStatus, newStatus)
			return
		}

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

		// Fire alert engine (muted checks were already returned above).
		go e.evaluateServiceAlerts(ctx, sc, oldStatus, newStatus, result)
	}
}

// --- SNMP Logic ---

// syncSNMPProfiles refreshes the classifier from device_profiles. The
// DB is the template source of truth (builtins arrive via SQL
// migration, custom templates via the API), so this runs on the same
// cadence as the device sync — template edits go live within a minute
// without a poller restart.
func (e *Engine) syncSNMPProfiles(ctx context.Context) error {
	profiles, err := e.snmpLoader.LoadProfiles(ctx)
	if err != nil {
		return err
	}
	for _, cerr := range e.snmpClassifier.SetProfiles(profiles) {
		e.logger.Warnf("profile compile: %v", cerr)
	}
	return nil
}

func (e *Engine) syncSNMPDevices(ctx context.Context) error {
	if err := e.syncSNMPProfiles(ctx); err != nil {
		e.logger.Warnf("SNMP profile sync failed: %v", err)
	}
	devices, err := e.snmpLoader.LoadSNMPDevices(ctx)
	if err != nil {
		return err
	}

	// Operator-configured global UDT cadence (Settings → UDT). Falls
	// back to the UDT_POLL_INTERVAL env var / 5m default when unset.
	udtGlobal := udtIntervalFromEnv()
	if n, err := e.snmpLoader.LoadUdtGlobalInterval(ctx); err != nil {
		e.logger.Warnf("UDT global interval load failed: %v", err)
	} else if n > 0 {
		udtGlobal = time.Duration(n) * time.Second
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	e.udtInterval = udtGlobal

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
	now := time.Now()
	devices := make([]*snmp.Device, 0, len(e.snmpDevices))
	udtDue := make(map[uuid.UUID]bool, len(e.snmpDevices))
	for _, d := range e.snmpDevices {
		if d.Enabled {
			devices = append(devices, d)
			ival := e.udtInterval
			if d.UdtInterval > 0 {
				ival = d.UdtInterval
			}
			udtDue[d.ID] = d.UdtEnabled && now.Sub(e.lastUdtAt[d.ID]) >= ival
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
	// the cycle longer than this. UDT cycles walk large FDB/ARP tables
	// (plus per-VLAN sessions on Cisco), so they get a bigger budget.
	const devBudgetBase = 20 * time.Second
	const devBudgetUdt = 75 * time.Second

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
			devBudget := devBudgetBase
			if udtDue[d.ID] {
				devBudget = devBudgetUdt
			}
			// Pre-allocate the Result so the collector can write into
			// it progressively. If the per-device budget fires mid-poll
			// we still have whatever was collected so far (at minimum
			// the System info — enough to populate vendor/model/OS).
			res := &snmp.Result{DeviceID: d.ID, Timestamp: time.Now().UTC(), WantUdt: udtDue[d.ID]}

			// Cancel the collector's context if budget expires so any
			// in-flight walk stops as soon as it checks ctx.
			collectCtx, cancel := context.WithTimeout(ctx, devBudget)
			defer cancel()

			done := make(chan struct{})
			go func() {
				e.snmpCollector.Collect(collectCtx, d, res)
				close(done)
			}()

			select {
			case <-done:
			case <-time.After(devBudget):
				res.Mu.Lock()
				if res.Err == nil {
					res.Err = fmt.Errorf("device budget %s exceeded", devBudget)
				}
				res.Mu.Unlock()
				cancel()
			case <-ctx.Done():
				res.Mu.Lock()
				if res.Err == nil {
					res.Err = ctx.Err()
				}
				res.Mu.Unlock()
				cancel()
			}

			// Snapshot under lock so handleSNMPResult can safely read
			// fields even if the collect goroutine is still running.
			res.Mu.Lock()
			resErr := res.Err
			res.Mu.Unlock()

			e.handleSNMPResult(ctx, d, res)
			countMu.Lock()
			if resErr == nil {
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
	// Snapshot fields under the result mutex so we don't race with a
	// collector goroutine that's still running past the budget.
	r.Mu.Lock()
	rErr := r.Err
	rSystem := r.System
	rIfs := r.Interfaces
	rEnts := r.Entities
	rSens := r.Sensors
	rScal := r.Scalars
	rIfSamp := r.IfSamples
	rUdt := r.Udt
	rTplVals := r.TplValues
	rTplGroups := r.TplGroups
	r.Mu.Unlock()

	// IMPORTANT: even when the poll cycle errored (e.g. per-device budget
	// exceeded, or interface walk failed), we still want to persist any
	// partial results that were collected before the failure — otherwise a
	// slow device's basic identity (sys_object_id / vendor / model / OS)
	// never makes it to the UI. We only skip the heavy time-series writes.
	if rErr != nil {
		e.logger.Warnf("SNMP poll failed for %s (%s): %v", d.Hostname, d.IPAddress, rErr)
	}

	// 1) discovery writeback — system info, interfaces, entities, sensors
	if rSystem != nil {
		// Classify based on sysObjectID + sysDescr.
		vendor, model, osVersion := "", "", ""
		if prof := e.snmpClassifier.Match(rSystem.SysObjectID, rSystem.SysDescr); prof != nil {
			v, m, o := e.snmpClassifier.Extract(prof, rSystem.SysDescr)
			vendor, model, osVersion = v, m, o
			if prof.ID != uuid.Nil {
				if err := e.snmpLoader.AssignProfileIfUnset(ctx, d.ID, prof.ID); err != nil {
					e.logger.Warnf("AssignProfile %s → %s: %v", d.Hostname, prof.Name, err)
				}
			}
		}
		if err := e.snmpLoader.UpsertSystemInfo(ctx, d.ID, rSystem.SysObjectID, vendor, model, osVersion, rSystem.SysName); err != nil {
			e.logger.Warnf("UpsertSystemInfo %s: %v", d.Hostname, err)
		}
	}
	if len(rIfs) > 0 {
		if err := e.snmpLoader.UpsertInterfaces(ctx, d.ID, rIfs); err != nil {
			e.logger.Warnf("UpsertInterfaces %s (%d rows): %v", d.Hostname, len(rIfs), err)
		}
	}
	if len(rEnts) > 0 {
		if err := e.snmpLoader.UpsertEntities(ctx, d.ID, rEnts); err != nil {
			e.logger.Warnf("UpsertEntities %s (%d rows): %v", d.Hostname, len(rEnts), err)
		}
	}
	if len(rSens) > 0 {
		if err := e.snmpLoader.UpsertSensors(ctx, d.ID, rSens); err != nil {
			e.logger.Warnf("UpsertSensors %s (%d rows): %v", d.Hostname, len(rSens), err)
		}
	}
	if len(rTplVals) > 0 || len(rTplGroups) > 0 {
		if err := e.snmpLoader.UpsertTemplateValues(ctx, d.ID, rTplVals, rTplGroups); err != nil {
			e.logger.Warnf("UpsertTemplateValues %s (%d rows): %v", d.Hostname, len(rTplVals), err)
		}
	}
	if rUdt != nil {
		if err := e.snmpLoader.UpsertUdtData(ctx, d.ID, rUdt); err != nil {
			e.logger.Warnf("UpsertUdtData %s (%d fdb, %d arp, %d nbrs): %v",
				d.Hostname, len(rUdt.Fdb), len(rUdt.Arp), len(rUdt.Neighbors), err)
		} else {
			e.logger.Infof("UDT %s: %d fdb, %d arp, %d neighbors, %d vlans",
				d.Hostname, len(rUdt.Fdb), len(rUdt.Arp), len(rUdt.Neighbors), len(rUdt.Vlans))
		}
		if rUdt.FdbNote != "" {
			e.logger.Warnf("UDT %s: no switch-port data — %s", d.Hostname, rUdt.FdbNote)
		}
		e.mu.Lock()
		e.lastUdtAt[d.ID] = time.Now()
		e.mu.Unlock()
	}

	// 2) ClickHouse time-series writes — skip when the poll errored so we
	// don't write a partial / inconsistent counter diff.
	if rErr == nil {
		for _, m := range rScal {
			e.snmpWriter.WriteSNMPMetric(m)
		}
		for _, m := range rIfSamp {
			e.snmpWriter.WriteSNMPIfMetric(m)
		}
	}
}

// seedSNMPProfiles optionally imports profile packs from disk (a dev /
// air-gapped bootstrap path), then loads the authoritative profile set
// from the database into the classifier. Builtin templates normally
// arrive via SQL migration, so a missing directory is not an error.
func (e *Engine) seedSNMPProfiles(ctx context.Context) error {
	dir := os.Getenv("SNMP_PROFILES_DIR")
	if dir == "" {
		dir = "/opt/zenplus/data/profiles"
	}
	if _, statErr := os.Stat(dir); statErr == nil {
		profiles, loadErrs := e.snmpClassifier.LoadFromDir(dir)
		for _, le := range loadErrs {
			e.logger.Warnf("profile load: %v", le)
		}
		for _, p := range profiles {
			if err := e.snmpLoader.UpsertProfile(ctx, p); err != nil {
				e.logger.Warnf("upsert profile %s: %v", p.Name, err)
				continue
			}
		}
		if len(profiles) > 0 {
			e.logger.Infof("SNMP profile file seed: %d profiles from %s", len(profiles), dir)
		}
	}
	// DB is the source of truth — always finish by (re)loading from it,
	// which also replaces whatever LoadFromDir put in the classifier.
	if err := e.syncSNMPProfiles(ctx); err != nil {
		return err
	}
	return nil
}
