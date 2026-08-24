package apm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	goruntime "runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/shirou/gopsutil/v4/process"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	agentruntime "zenplus-agent/internal/runtime"
)

const gatewayVersion = "0.158.0-zp2"

const (
	gatewayStopTimeout       = 5 * time.Second
	gatewayMetricsPort       = 18888
	gatewayMetricsMaxBytes   = 2 * 1024 * 1024
	gatewayMetricsWindow     = time.Minute
	exportFailureStateWindow = 2 * time.Minute
	gatewayLogMaxBytes       = 10 * 1024 * 1024
	gatewayLogBackups        = 3
)

type gatewayMetricTotals struct {
	Sent, Failed, Dropped int64
	SentKnown             bool
	FailedKnown           bool
	DroppedKnown          bool
}

type gatewayMetricSample struct {
	At           time.Time
	Sent, Failed int64
}

type gatewayMetricSnapshot struct {
	gatewayMetricTotals
	QueueSize      int64
	QueueSizeKnown bool
}

type rotatingGatewayLog struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	backups  int
	file     *os.File
	size     int64
}

type Manager struct {
	reconcileMu                 sync.Mutex
	mu                          sync.Mutex
	paths                       agentruntime.Paths
	installDir                  string
	cmd                         *exec.Cmd
	cmdDone                     chan struct{}
	closeJob                    func()
	activeCredentialFingerprint string
	activeConfigFingerprint     string
	gatewayLogOffset            int64
	lastExportFailure           time.Time
	lastExportSuccess           time.Time
	gatewayMetricTotals         gatewayMetricTotals
	gatewayMetricSamples        []gatewayMetricSample
	status                      model.AgentAPMHeartbeat
	lastDiscovery               time.Time
	logf                        func(string, ...any)
}

type enrollResponse struct {
	Key         string `json:"key"`
	KeyID       string `json:"key_id"`
	Environment string `json:"environment"`
	TracesPath  string `json:"traces_path"`
}

type discoveryReport struct {
	Processes []discoveredProcess `json:"processes"`
}

type discoveredProcess struct {
	ProcessKey           string     `json:"process_key"`
	PID                  int32      `json:"pid"`
	PPID                 int32      `json:"ppid"`
	ExePath              string     `json:"exe_path"`
	Cmdline              string     `json:"cmdline"`
	Runtime              string     `json:"runtime"`
	RuntimeVersion       string     `json:"runtime_version"`
	ServiceNameGuess     string     `json:"service_name_guess"`
	WindowsService       string     `json:"windows_service,omitempty"`
	IISAppPool           string     `json:"iis_app_pool,omitempty"`
	InstrumentationState string     `json:"instrumentation_state"`
	OTelDetected         bool       `json:"otel_detected"`
	OTelEndpoint         string     `json:"otel_endpoint,omitempty"`
	ArtifactPath         string     `json:"artifact_path,omitempty"`
	ArtifactFingerprint  string     `json:"artifact_fingerprint,omitempty"`
	ArtifactModifiedAt   *time.Time `json:"artifact_modified_at,omitempty"`
}

func New(paths agentruntime.Paths, logf func(string, ...any)) *Manager {
	exe, _ := os.Executable()
	return &Manager{
		paths:      paths,
		installDir: filepath.Dir(exe),
		logf:       logf,
		status: model.AgentAPMHeartbeat{
			State: "disabled", Gateway: model.APMGatewayStatus{Managed: true, GRPCPort: 4317, HTTPPort: 4318},
			CheckedAt: time.Now().UTC(), Bundles: map[string]string{},
		},
	}
}

// Reconcile makes the local gateway match the endpoint setting. It is safe to
// call repeatedly; credentials and configuration are only rotated when needed.
func (m *Manager) Reconcile(ctx context.Context, cfg config.Config, agentID, serverID string, api *client.Client) {
	m.reconcileMu.Lock()
	defer m.reconcileMu.Unlock()

	profile := first(cfg.APM.Profile, "combined")
	environment := first(cfg.APM.Environment, "prod")
	m.mu.Lock()
	m.status.Enabled = cfg.APM.Enabled
	m.status.Profile = profile
	m.status.Environment = environment
	m.status.CheckedAt = time.Now().UTC()
	m.mu.Unlock()

	if !cfg.APM.Enabled || profile == "infrastructure" {
		m.stop("disabled")
		return
	}
	if api == nil || agentID == "" || serverID == "" {
		m.stop("waiting_authorization")
		m.setFailure("waiting_authorization", "Waiting for appliance authorization before starting APM")
		return
	}

	binding := newCredentialBinding(cfg.ControllerURL, agentID, serverID, environment)
	credential, credentialChanged, err := m.ensureCredential(ctx, api, binding)
	if err != nil {
		m.stop("credential_error")
		m.setFailure("credential_error", err.Error())
		return
	}
	configFingerprint, configChanged, err := m.ensureConfig(cfg, agentID, serverID)
	if err != nil {
		m.stop("configuration_error")
		m.setFailure("configuration_error", err.Error())
		return
	}
	if err := m.ensureStarted(ctx, credential.Key, credential.Fingerprint, configFingerprint, credentialChanged, configChanged); err != nil {
		m.setFailure("failed", err.Error())
		return
	}
	if m.refreshHealth(normalizedAPMBind(cfg.APM.BindAddress)) {
		if err := m.stopAndWait("credential_refresh", gatewayStopTimeout); err != nil {
			m.setFailure("credential_error", err.Error())
			return
		}
		if err := invalidateBoundCredential(m.paths); err != nil {
			m.setFailure("credential_error", "The appliance rejected APM telemetry and the stale credential could not be cleared: "+err.Error())
			return
		}
		m.setFailure("credential_rejected", "The appliance rejected the APM ingest credential; ZenPlus will obtain a replacement")
		return
	}
	m.reportDiscovery(ctx, api)
}

func (m *Manager) Snapshot() *model.AgentAPMHeartbeat {
	m.mu.Lock()
	defer m.mu.Unlock()
	copy := m.status
	copy.Bundles = map[string]string{}
	for k, v := range m.status.Bundles {
		copy.Bundles[k] = v
	}
	return &copy
}

func (m *Manager) Close() {
	m.reconcileMu.Lock()
	defer m.reconcileMu.Unlock()
	m.stop("stopped")
}

func (m *Manager) ensureCredential(ctx context.Context, api *client.Client, binding credentialBinding) (credentialMaterial, bool, error) {
	if err := binding.validate(); err != nil {
		return credentialMaterial{}, false, err
	}
	if credential, ok := loadBoundCredential(m.paths, binding); ok {
		return credential, false, nil
	}
	if api == nil {
		return credentialMaterial{}, false, fmt.Errorf("APM enrollment client is unavailable")
	}

	// A cached key without matching metadata belongs to an unknown or previous
	// context. Stop the old gateway before the controller revokes that key and
	// issues its replacement so telemetry cannot cross bindings.
	if err := m.stopAndWait("credential_refresh", gatewayStopTimeout); err != nil {
		return credentialMaterial{}, false, err
	}
	m.logf("APM credential binding is missing or changed; requesting a replacement")
	var response enrollResponse
	_, _, err := api.PostJSON(ctx, "/api/v1/agents/apm/enroll", map[string]string{"environment": binding.Environment}, &response)
	if err != nil {
		return credentialMaterial{}, false, fmt.Errorf("obtain appliance-managed APM credential: %w", err)
	}
	response.Key = strings.TrimSpace(response.Key)
	response.KeyID = strings.TrimSpace(response.KeyID)
	response.Environment = strings.TrimSpace(response.Environment)
	if !strings.HasPrefix(response.Key, "zpi_") || response.KeyID == "" {
		return credentialMaterial{}, false, fmt.Errorf("controller returned an invalid APM credential")
	}
	if response.Environment != binding.Environment {
		return credentialMaterial{}, false, fmt.Errorf("controller returned APM environment %q, expected %q", response.Environment, binding.Environment)
	}
	credential, err := persistBoundCredential(m.paths, binding, response.KeyID, response.Key)
	if err != nil {
		return credentialMaterial{}, false, err
	}
	return credential, true, nil
}

func (m *Manager) ensureConfig(cfg config.Config, agentID, serverID string) (string, bool, error) {
	bind := normalizedAPMBind(cfg.APM.BindAddress)
	if bind != "127.0.0.1" && bind != "::1" {
		return "", false, fmt.Errorf("refusing non-loopback OTLP bind address %q", bind)
	}
	endpoint := strings.TrimRight(cfg.ControllerURL, "/")
	content := fmt.Sprintf(`extensions:
  health_check:
    endpoint: %s
  file_storage:
    directory: %s
    create_directory: true
    timeout: 5s
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: %s
      http:
        endpoint: %s
processors:
  memory_limiter:
    check_interval: 2s
    limit_mib: 128
    spike_limit_mib: 32
  resource/zenplus:
    attributes:
      - key: zenplus.agent_id
        value: %s
        action: upsert
      - key: zenplus.server_id
        value: %s
        action: upsert
  batch:
    timeout: 2s
    send_batch_size: 512
    send_batch_max_size: 2048
exporters:
  otlp_http/zenplus:
    endpoint: %s
    compression: gzip
    headers:
      Authorization: "Bearer ${env:ZENPLUS_APM_KEY}"
    tls:
      insecure_skip_verify: %t
    sending_queue:
      enabled: true
      num_consumers: 2
      sizer: items
      queue_size: 5000
      storage: file_storage
    retry_on_failure:
      enabled: true
      initial_interval: 1s
      max_interval: 30s
      max_elapsed_time: 0s
service:
  extensions: [health_check, file_storage]
  telemetry:
    logs:
      level: info
    metrics:
      level: basic
      readers:
        - pull:
            exporter:
              prometheus:
                host: %s
                port: %d
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resource/zenplus, batch]
      exporters: [otlp_http/zenplus]
`, quoteYAML(netJoin(bind, 13133)), quoteYAML(m.paths.APMStorage), quoteYAML(netJoin(bind, 4317)),
		quoteYAML(netJoin(bind, 4318)), quoteYAML(agentID), quoteYAML(serverID), quoteYAML(endpoint), !cfg.VerifyTLS,
		quoteYAML(bind), gatewayMetricsPort)
	configHash := sha256.Sum256([]byte(content))
	fingerprint := hex.EncodeToString(configHash[:])

	if existing, err := os.ReadFile(m.paths.APMConfig); err == nil && string(existing) == content {
		return fingerprint, false, nil
	}
	if err := writeAtomic(m.paths.APMConfig, 0o600, func(tempPath string) error {
		return os.WriteFile(tempPath, []byte(content), 0o600)
	}); err != nil {
		return "", false, err
	}
	return fingerprint, true, nil
}

func (m *Manager) ensureStarted(
	ctx context.Context,
	key, credentialFingerprint, configFingerprint string,
	credentialChanged, configChanged bool,
) error {
	m.mu.Lock()
	managedProcess := m.cmd != nil
	running := m.cmd != nil && m.cmd.Process != nil && m.cmd.ProcessState == nil
	credentialNeedsRestart := credentialChanged || m.activeCredentialFingerprint != credentialFingerprint
	configNeedsRestart := configChanged || m.activeConfigFingerprint != configFingerprint
	if running && !credentialNeedsRestart && !configNeedsRestart {
		m.mu.Unlock()
		return nil
	}
	m.mu.Unlock()
	if managedProcess && !running {
		if err := m.stopAndWait("restarting", gatewayStopTimeout); err != nil {
			return err
		}
	}
	if running {
		reasons := make([]string, 0, 2)
		if credentialNeedsRestart {
			reasons = append(reasons, "credential")
		}
		if configNeedsRestart {
			reasons = append(reasons, "configuration")
		}
		m.logf("APM telemetry gateway restart required after %s change", strings.Join(reasons, " and "))
		if err := m.stopAndWait("restarting", gatewayStopTimeout); err != nil {
			return err
		}
	}

	binary := filepath.Join(m.installDir, "apm", "gateway", "zenplus-telemetry-gateway.exe")
	if goruntime.GOOS != "windows" {
		binary = filepath.Join(m.installDir, "apm", "gateway", "zenplus-telemetry-gateway")
	}
	if _, err := os.Stat(binary); err != nil {
		return fmt.Errorf("managed telemetry gateway is not installed: %s", binary)
	}
	logWriter, err := newRotatingGatewayLog(m.paths.APMLog, gatewayLogMaxBytes, gatewayLogBackups)
	if err != nil {
		return fmt.Errorf("open managed telemetry gateway log: %w", err)
	}
	logStartOffset := logWriter.Size()
	cmd := exec.CommandContext(ctx, binary, "--config", m.paths.APMConfig)
	cmd.Stdout = logWriter
	cmd.Stderr = logWriter
	cmd.Env = append(os.Environ(), "ZENPLUS_APM_KEY="+key, "GOMEMLIMIT=160MiB")
	setHiddenProcess(cmd)
	if err := cmd.Start(); err != nil {
		_ = logWriter.Close()
		return fmt.Errorf("start managed telemetry gateway: %w", err)
	}
	closeJob, err := attachProcessLifecycle(cmd)
	if err != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		_ = logWriter.Close()
		return fmt.Errorf("protect telemetry gateway lifecycle: %w", err)
	}
	done := make(chan struct{})
	m.mu.Lock()
	m.cmd = cmd
	m.cmdDone = done
	m.closeJob = closeJob
	m.activeCredentialFingerprint = credentialFingerprint
	m.activeConfigFingerprint = configFingerprint
	m.gatewayLogOffset = logStartOffset
	m.lastExportFailure = time.Time{}
	m.lastExportSuccess = time.Time{}
	m.gatewayMetricTotals = gatewayMetricTotals{}
	m.gatewayMetricSamples = nil
	m.status.SpansForwarded1M = 0
	m.status.ExportErrors1M = 0
	m.status.SpoolDepthSpans = 0
	m.status.SpoolBytes = 0
	m.status.DroppedSpansTotal = 0
	m.status.State = "starting"
	m.status.LastError = ""
	m.status.Gateway.Version = gatewayVersion
	m.status.Gateway.Managed = true
	m.status.Bundles = m.detectBundles()
	m.mu.Unlock()
	m.logf("APM telemetry gateway %s started pid=%d", gatewayVersion, cmd.Process.Pid)
	go func() {
		err := cmd.Wait()
		_ = logWriter.Close()
		close(done)
		m.mu.Lock()
		if m.cmd == cmd {
			m.cmd = nil
			m.cmdDone = nil
			m.activeCredentialFingerprint = ""
			m.activeConfigFingerprint = ""
			if m.closeJob != nil {
				m.closeJob()
				m.closeJob = nil
			}
			m.status.Gateway.Listening = false
			m.status.Gateway.Healthy = false
			if ctx.Err() == nil {
				m.status.State = "failed"
				m.status.LastError = fmt.Sprintf("telemetry gateway exited: %v", err)
			}
		}
		m.mu.Unlock()
	}()
	return nil
}

// refreshHealth returns true when the appliance rejected the active ingest
// credential. Local ports alone cannot establish end-to-end export health, so
// the manager also consumes new, bounded collector diagnostics.
func (m *Manager) refreshHealth(bindAddress string) bool {
	bindAddress = first(bindAddress, "127.0.0.1")
	client := &http.Client{Timeout: 750 * time.Millisecond}
	resp, err := client.Get("http://" + netJoin(bindAddress, 13133) + "/")
	healthy := err == nil && resp.StatusCode >= 200 && resp.StatusCode < 300
	if resp != nil {
		_ = resp.Body.Close()
	}
	listening := portListening(bindAddress, 4317) || portListening(bindAddress, 4318)
	logChunk := m.readGatewayLogDelta()
	authRejected, exportFailed := classifyGatewayLogChunk(logChunk)
	if healthy && listening {
		m.refreshGatewayMetrics(bindAddress)
	}
	m.mu.Lock()
	if exportFailed {
		m.lastExportFailure = time.Now().UTC()
	}
	m.status.Gateway.Listening = listening
	m.status.Gateway.Healthy = healthy
	m.status.CheckedAt = time.Now().UTC()
	if authRejected {
		m.status.State = "credential_rejected"
		m.status.LastError = "The appliance rejected the APM ingest credential"
	} else if healthy && listening && !m.lastExportFailure.IsZero() &&
		(m.lastExportSuccess.IsZero() || !m.lastExportFailure.Before(m.lastExportSuccess)) &&
		time.Since(m.lastExportFailure) < exportFailureStateWindow {
		m.status.State = "degraded"
		m.status.LastError = "The local APM gateway is running, but recent telemetry export attempts failed"
	} else if healthy && listening {
		m.status.State = "active"
		m.status.LastError = ""
	} else if m.status.State != "failed" {
		m.status.State = "starting"
	}
	m.mu.Unlock()
	return authRejected
}

func (m *Manager) readGatewayLogDelta() string {
	const maxGatewayDiagnosticBytes int64 = 512 * 1024
	m.mu.Lock()
	offset := m.gatewayLogOffset
	m.mu.Unlock()
	file, err := os.Open(m.paths.APMLog)
	if err != nil {
		return ""
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return ""
	}
	if info.Size() < offset {
		offset = 0
	}
	if info.Size()-offset > maxGatewayDiagnosticBytes {
		offset = info.Size() - maxGatewayDiagnosticBytes
	}
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return ""
	}
	data, _ := io.ReadAll(io.LimitReader(file, maxGatewayDiagnosticBytes))
	newOffset := offset + int64(len(data))
	m.mu.Lock()
	if newOffset > m.gatewayLogOffset || info.Size() < m.gatewayLogOffset {
		m.gatewayLogOffset = newOffset
	}
	m.mu.Unlock()
	return string(data)
}

func (m *Manager) refreshGatewayMetrics(bindAddress string) {
	client := &http.Client{Timeout: 750 * time.Millisecond}
	resp, err := client.Get("http://" + netJoin(bindAddress, gatewayMetricsPort) + "/metrics")
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, gatewayMetricsMaxBytes))
	if err != nil {
		return
	}
	metrics, ok := parseGatewayMetrics(string(data))
	if !ok {
		return
	}
	m.applyGatewayMetrics(metrics, time.Now().UTC(), directorySize(m.paths.APMStorage))
}

func parseGatewayMetrics(text string) (gatewayMetricSnapshot, bool) {
	var result gatewayMetricSnapshot
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		metric := fields[0]
		labels := ""
		if index := strings.IndexByte(metric, '{'); index >= 0 {
			labels = metric[index+1:]
			metric = metric[:index]
		}
		if !strings.Contains(labels, `exporter="otlp_http/zenplus"`) ||
			(strings.Contains(labels, `data_type=`) && !strings.Contains(labels, `data_type="traces"`)) {
			continue
		}
		metric = strings.TrimSuffix(metric, "_total")
		value, err := strconv.ParseFloat(fields[1], 64)
		if err != nil || value < 0 {
			continue
		}
		count := int64(value)
		switch metric {
		case "otelcol_exporter_sent_spans":
			result.Sent += count
			result.SentKnown = true
		case "otelcol_exporter_send_failed_spans":
			result.Failed += count
			result.FailedKnown = true
		case "otelcol_exporter_enqueue_failed_spans":
			result.Dropped += count
			result.DroppedKnown = true
		case "otelcol_exporter_queue_size":
			result.QueueSize = count
			result.QueueSizeKnown = true
		}
	}
	return result, result.SentKnown || result.FailedKnown || result.DroppedKnown || result.QueueSizeKnown
}

func (m *Manager) applyGatewayMetrics(metrics gatewayMetricSnapshot, now time.Time, spoolBytes int64) {
	m.mu.Lock()
	defer m.mu.Unlock()

	var sentDelta, failedDelta int64
	if metrics.SentKnown {
		if m.gatewayMetricTotals.SentKnown && metrics.Sent >= m.gatewayMetricTotals.Sent {
			sentDelta = metrics.Sent - m.gatewayMetricTotals.Sent
		} else {
			sentDelta = metrics.Sent
		}
		m.gatewayMetricTotals.Sent = metrics.Sent
		m.gatewayMetricTotals.SentKnown = true
	}
	if metrics.FailedKnown {
		if m.gatewayMetricTotals.FailedKnown && metrics.Failed >= m.gatewayMetricTotals.Failed {
			failedDelta = metrics.Failed - m.gatewayMetricTotals.Failed
		} else {
			failedDelta = metrics.Failed
		}
		m.gatewayMetricTotals.Failed = metrics.Failed
		m.gatewayMetricTotals.FailedKnown = true
	}
	if metrics.DroppedKnown {
		m.gatewayMetricTotals.Dropped = metrics.Dropped
		m.gatewayMetricTotals.DroppedKnown = true
		m.status.DroppedSpansTotal = metrics.Dropped
	}
	if metrics.QueueSizeKnown {
		m.status.SpoolDepthSpans = boundedMetricInt(metrics.QueueSize)
	}
	m.status.SpoolBytes = spoolBytes
	if sentDelta > 0 || failedDelta > 0 {
		m.gatewayMetricSamples = append(m.gatewayMetricSamples, gatewayMetricSample{At: now, Sent: sentDelta, Failed: failedDelta})
	}
	if sentDelta > 0 {
		m.lastExportSuccess = now
	}
	if failedDelta > 0 {
		m.lastExportFailure = now
	}

	cutoff := now.Add(-gatewayMetricsWindow)
	kept := m.gatewayMetricSamples[:0]
	var sent, failed int64
	for _, sample := range m.gatewayMetricSamples {
		if sample.At.Before(cutoff) {
			continue
		}
		kept = append(kept, sample)
		sent += sample.Sent
		failed += sample.Failed
	}
	m.gatewayMetricSamples = kept
	m.status.SpansForwarded1M = boundedMetricInt(sent)
	m.status.ExportErrors1M = boundedMetricInt(failed)
}

func boundedMetricInt(value int64) int {
	if value <= 0 {
		return 0
	}
	maxInt := int(^uint(0) >> 1)
	if value > int64(maxInt) {
		return maxInt
	}
	return int(value)
}

func directorySize(root string) int64 {
	var size int64
	_ = filepath.WalkDir(root, func(_ string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err == nil && info.Mode().IsRegular() {
			size += info.Size()
		}
		return nil
	})
	return size
}

func newRotatingGatewayLog(path string, maxBytes int64, backups int) (*rotatingGatewayLog, error) {
	if err := rotateGatewayLog(path, maxBytes, backups); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	size := int64(0)
	if info, err := file.Stat(); err == nil {
		size = info.Size()
	}
	return &rotatingGatewayLog{path: path, maxBytes: maxBytes, backups: backups, file: file, size: size}, nil
}

func (w *rotatingGatewayLog) Size() int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.size
}

func (w *rotatingGatewayLog) Write(data []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return 0, os.ErrClosed
	}
	written := 0
	for len(data) > 0 {
		if w.size >= w.maxBytes {
			if err := w.rotateLocked(); err != nil {
				return written, err
			}
		}
		remaining := w.maxBytes - w.size
		chunk := data
		if int64(len(chunk)) > remaining {
			chunk = chunk[:remaining]
		}
		n, err := w.file.Write(chunk)
		written += n
		w.size += int64(n)
		data = data[n:]
		if err != nil {
			return written, err
		}
		if n == 0 {
			return written, io.ErrShortWrite
		}
	}
	return written, nil
}

func (w *rotatingGatewayLog) rotateLocked() error {
	if err := w.file.Close(); err != nil {
		return err
	}
	w.file = nil
	if err := rotateGatewayLog(w.path, w.maxBytes, w.backups); err != nil {
		return err
	}
	file, err := os.OpenFile(w.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	w.file = file
	w.size = 0
	return nil
}

func (w *rotatingGatewayLog) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	return err
}

func rotateGatewayLog(path string, maxBytes int64, backups int) error {
	if strings.TrimSpace(path) == "" || maxBytes <= 0 || backups < 1 {
		return fmt.Errorf("invalid gateway log rotation settings")
	}
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return fmt.Errorf("gateway log is not a regular file")
	}
	if info.Size() < maxBytes {
		return nil
	}
	for index := backups; index >= 1; index-- {
		destination := fmt.Sprintf("%s.%d", path, index)
		if err := os.Remove(destination); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if index == 1 {
			if err := os.Rename(path, destination); err != nil {
				return err
			}
			continue
		}
		source := fmt.Sprintf("%s.%d", path, index-1)
		if err := os.Rename(source, destination); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

func classifyGatewayLogChunk(chunk string) (authRejected, exportFailed bool) {
	text := strings.ToLower(chunk)
	authMarkers := []string{
		"status code: 401", "status_code=401", "status code 401",
		"http status code 401", "code = unauthenticated", "401 unauthorized",
	}
	for _, marker := range authMarkers {
		if strings.Contains(text, marker) {
			return true, true
		}
	}
	exportMarkers := []string{
		"exporting failed", "failed to export", "dropping data", "dropped_items",
	}
	for _, marker := range exportMarkers {
		if strings.Contains(text, marker) {
			return false, true
		}
	}
	return false, false
}

func (m *Manager) stop(state string) {
	if err := m.stopAndWait(state, gatewayStopTimeout); err != nil {
		m.logf("APM: %v", err)
	}
}

func (m *Manager) stopAndWait(state string, timeout time.Duration) error {
	m.mu.Lock()
	cmd := m.cmd
	done := m.cmdDone
	closeJob := m.closeJob
	m.cmd = nil
	m.cmdDone = nil
	m.closeJob = nil
	m.activeCredentialFingerprint = ""
	m.activeConfigFingerprint = ""
	m.status.State = state
	m.status.Gateway.Listening = false
	m.status.Gateway.Healthy = false
	m.status.CheckedAt = time.Now().UTC()
	m.mu.Unlock()
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
	if closeJob != nil {
		closeJob()
	}
	if done != nil {
		timer := time.NewTimer(timeout)
		defer timer.Stop()
		select {
		case <-done:
		case <-timer.C:
			return fmt.Errorf("timed out waiting for the managed telemetry gateway to stop")
		}
	}
	return nil
}

func (m *Manager) setFailure(state, message string) {
	m.mu.Lock()
	m.status.State = state
	m.status.LastError = message
	m.status.CheckedAt = time.Now().UTC()
	m.status.Failed++
	m.mu.Unlock()
	m.logf("APM: %s", message)
}

func (m *Manager) detectBundles() map[string]string {
	bundles := map[string]string{"collector": gatewayVersion}
	checks := map[string]string{
		"dotnet": filepath.Join(m.installDir, "apm", "instrumentation", "dotnet"),
		"java":   filepath.Join(m.installDir, "apm", "instrumentation", "java", "opentelemetry-javaagent.jar"),
		"node":   filepath.Join(m.installDir, "apm", "instrumentation", "node", "node_modules"),
		"python": filepath.Join(m.installDir, "apm", "instrumentation", "python", "wheelhouse"),
	}
	versions := map[string]string{"dotnet": "1.16.0", "java": "2.31.0", "node": "0.79.0", "python": "0.65b0"}
	for name, path := range checks {
		if _, err := os.Stat(path); err == nil {
			bundles[name] = versions[name]
		}
	}
	return bundles
}

func (m *Manager) reportDiscovery(ctx context.Context, api *client.Client) {
	m.mu.Lock()
	if time.Since(m.lastDiscovery) < time.Minute {
		m.mu.Unlock()
		return
	}
	m.lastDiscovery = time.Now()
	m.mu.Unlock()
	rows := discoverProcessesWithState(m.loadInstrumentationState())
	if m.guardInstrumentationCrashLoops(ctx, rows) {
		rows = discoverProcessesWithState(m.loadInstrumentationState())
	}
	var accepted struct {
		Accepted int `json:"accepted"`
	}
	_, _, err := api.PostJSON(ctx, "/api/v1/agents/apm/discovery", discoveryReport{Processes: rows}, &accepted)
	if err != nil {
		m.setFailure("active", "runtime discovery report failed: "+err.Error())
		return
	}
	instrumented := 0
	for _, row := range rows {
		if row.OTelDetected {
			instrumented++
		}
	}
	m.mu.Lock()
	m.status.Discovered = len(rows)
	m.status.Instrumented = instrumented
	m.status.CheckedAt = time.Now().UTC()
	m.mu.Unlock()
}

// guardInstrumentationCrashLoops automatically restores the pre-ZenPlus
// environment when a managed IIS pool or Windows service changes PID twice
// inside 120 seconds. Overlapping workers are skipped to avoid mistaking an
// IIS overlapped recycle for a crash loop.
func (m *Manager) guardInstrumentationCrashLoops(ctx context.Context, rows []discoveredProcess) bool {
	state := m.loadInstrumentationState()
	pids := map[string]map[int32]struct{}{}
	for _, row := range rows {
		if row.PID <= 0 {
			continue
		}
		kind, name := "", ""
		if row.Runtime == "iis" && row.IISAppPool != "" {
			kind, name = "iis_app_pool", row.IISAppPool
		} else if row.WindowsService != "" {
			kind, name = "windows_service", row.WindowsService
		}
		if kind == "" {
			continue
		}
		key := instrumentationTargetKey(kind, name)
		if pids[key] == nil {
			pids[key] = map[int32]struct{}{}
		}
		pids[key][row.PID] = struct{}{}
	}
	now := time.Now().UTC()
	changed := false
	rolledBack := false
	for key, target := range state.Targets {
		if !target.Enabled || (target.TargetKind != "iis_app_pool" && target.TargetKind != "windows_service") || len(pids[key]) != 1 {
			continue
		}
		var pid int32
		for candidate := range pids[key] {
			pid = candidate
		}
		if target.LastPID == 0 {
			target.LastPID = pid
			state.Targets[key] = target
			changed = true
			continue
		}
		if target.LastPID == pid {
			continue
		}
		target.LastPID = pid
		fresh := target.Restarts[:0]
		for _, observed := range target.Restarts {
			if now.Sub(observed) <= 120*time.Second {
				fresh = append(fresh, observed)
			}
		}
		target.Restarts = append(fresh, now)
		state.Targets[key] = target
		changed = true
		if len(target.Restarts) < 2 {
			continue
		}
		request := InstrumentationRequest{
			Enabled: false, Runtime: target.Runtime, ProcessKey: target.ProcessKey,
			TargetKind: target.TargetKind, TargetName: target.TargetName,
			ServiceName: target.ServiceName, Environment: target.Environment, Restart: false,
		}
		if request.Runtime == "" && target.TargetKind == "iis_app_pool" {
			request.Runtime = "iis"
		}
		// Persist observations accumulated for all targets before the rollback
		// helper reloads and rewrites the shared state document.
		_ = writeInstrumentationState(m.paths.APMInstrumentationState, state)
		var rollbackErr error
		if target.TargetKind == "iis_app_pool" {
			_, rollbackErr = applyIISInstrumentation(ctx, m.paths.APMInstrumentationState, instrumentationBundlePath(m.installDir, "iis"), request)
		} else {
			_, rollbackErr = applyWindowsServiceInstrumentation(ctx, m.paths.APMInstrumentationState, instrumentationBundlePath(m.installDir, target.Runtime), request)
		}
		if rollbackErr != nil {
			target.LastError = "Crash-loop protection could not restore the target: " + rollbackErr.Error()
			state.Targets[key] = target
			m.logf("APM crash-loop rollback failed target=%q: %v", target.TargetName, rollbackErr)
			continue
		}
		state = m.loadInstrumentationState()
		target = state.Targets[key]
		target.LastError = "ZenPlus automatically rolled back instrumentation after two worker restarts within 120 seconds"
		target.Restarts = nil
		target.LastPID = pid
		state.Targets[key] = target
		rolledBack = true
		m.logf("APM crash-loop protection restored target=%q", target.TargetName)
	}
	if changed || rolledBack {
		if err := writeInstrumentationState(m.paths.APMInstrumentationState, state); err != nil {
			m.logf("APM: persist crash-loop state: %v", err)
		}
	}
	return rolledBack
}

func discoverProcesses() []discoveredProcess {
	return discoverProcessesWithState(instrumentationState{Targets: map[string]instrumentationTarget{}})
}

func discoverProcessesWithState(instrumentation instrumentationState) []discoveredProcess {
	processes, _ := process.Processes()
	services := windowsServicesByPID()
	rows := make([]discoveredProcess, 0, 32)
	for _, proc := range processes {
		name, _ := proc.Name()
		exe, _ := proc.Exe()
		cmdline, _ := proc.Cmdline()
		argv, _ := proc.CmdlineSlice()
		service := services[proc.Pid]
		runtimeName := classifyRuntime(name, exe, cmdline)
		moduleFacts := processModuleFacts{}
		if runtimeName == "iis" || runtimeName == "dotnet" || service.Name != "" {
			moduleFacts = inspectProcessModules(proc.Pid)
		}
		if runtimeName == "" && service.Name != "" {
			switch {
			case moduleFacts.DotnetCore:
				runtimeName = "dotnet"
			case moduleFacts.DotnetFramework:
				runtimeName = "dotnet_framework"
			}
		}
		if runtimeName == "" {
			continue
		}
		ppid, _ := proc.Ppid()
		lower := strings.ToLower(cmdline)
		processEnvironment, _ := proc.Environ()
		detected := strings.Contains(lower, "opentelemetry") || strings.Contains(lower, "otel_") || strings.Contains(lower, "javaagent") ||
			moduleFacts.OTelDetected || managedRuntimeDetected(runtimeName, processEnvironment)
		state := "none"
		if detected {
			state = "active"
		}
		appPool := ""
		basis := strings.ToLower(exe + "\x00" + cmdline)
		serviceName := strings.TrimSuffix(name, filepath.Ext(name))
		if runtimeName == "iis" {
			appPool = extractIISAppPool(cmdline)
			if appPool != "" {
				basis = "iis\x00" + strings.ToLower(appPool)
				serviceName = appPool
				target, managed := instrumentation.Targets[instrumentationTargetKey("iis_app_pool", appPool)]
				if managed {
					switch {
					case target.LastError != "":
						state = "failed"
					case target.Enabled && detected:
						state = "active"
					case target.Enabled:
						state = "pending"
					case target.PendingRestart:
						state = "pending"
					default:
						state = "none"
					}
				}
			}
		}
		windowsService := ""
		if service.Name != "" && runtimeName != "iis" {
			windowsService = service.Name
			basis = "windows_service\x00" + strings.ToLower(service.Name)
			serviceName = service.Name
			target, managed := instrumentation.Targets[instrumentationTargetKey("windows_service", service.Name)]
			if managed {
				switch {
				case target.LastError != "":
					state = "failed"
				case target.Enabled && detected:
					state = "active"
				case target.Enabled:
					state = "pending"
				case target.PendingRestart:
					state = "pending"
				default:
					state = "none"
				}
			}
		}
		hash := sha256.Sum256([]byte(basis))
		endpoint := ""
		if detected || state == "pending" {
			endpoint = "http://127.0.0.1:4318"
		}
		artifactPath, artifactFingerprint, artifactModifiedAt := "", "", (*time.Time)(nil)
		if windowsService != "" {
			cwd, _ := proc.Cwd()
			artifactPath = deploymentArtifactPath(runtimeName, exe, cmdline, cwd)
			artifactFingerprint, artifactModifiedAt = deploymentArtifactFingerprint(artifactPath)
		}
		rows = append(rows, discoveredProcess{
			ProcessKey: hex.EncodeToString(hash[:16]), PID: proc.Pid, PPID: ppid, ExePath: exe,
			Cmdline: safeDiscoveryCommandLine(name, argv, cmdline), Runtime: runtimeName, ServiceNameGuess: truncate(serviceName, 255),
			WindowsService: truncate(windowsService, 255), IISAppPool: truncate(appPool, 255),
			InstrumentationState: state, OTelDetected: detected, OTelEndpoint: endpoint,
			ArtifactPath: truncate(artifactPath, 4096), ArtifactFingerprint: artifactFingerprint,
			ArtifactModifiedAt: artifactModifiedAt,
		})
		if len(rows) >= 500 {
			break
		}
	}
	return rows
}

var commandArgumentPattern = regexp.MustCompile(`"[^"]*"|'[^']*'|[^\s]+`)

const (
	maxDiscoveryCommandArgs  = 64
	maxDiscoveryCommandBytes = 2048
)

var sensitiveDiscoveryOptions = map[string]bool{
	"--access-key": true, "--api-key": true, "--apikey": true,
	"--authorization": true, "--client-secret": true,
	"--connection-string": true, "--credential": true,
	"--password": true, "--passwd": true, "--pwd": true,
	"--secret": true, "--token": true,
	"-p": true, "-u": true,
}

var safeDiscoveryOptions = map[string]bool{
	"--bind": true, "--config": true, "--debug": true,
	"--environment": true, "--host": true, "--instance": true,
	"--listen": true, "--mode": true, "--port": true,
	"--profile": true, "--service": true, "--verbose": true,
	"-c": true, "-d": true, "-h": true, "-v": true,
}

// safeDiscoveryCommandLine retains only the executable name and argument
// shape. Discovery is controller telemetry, so paths, URLs, SQL, and argument
// values must never leave the host even when the target process is readable.
func safeDiscoveryCommandLine(processName string, argv []string, raw string) string {
	processName = strings.TrimSpace(filepath.Base(processName))
	if processName == "" {
		return ""
	}
	if len(argv) == 0 {
		argv = commandArgumentPattern.FindAllString(raw, -1)
	}
	if len(argv) > 0 {
		argv = argv[1:]
	}
	if len(argv) > maxDiscoveryCommandArgs {
		argv = argv[:maxDiscoveryCommandArgs]
	}
	parts := []string{processName}
	for _, argument := range argv {
		parts = append(parts, discoveryArgumentShape(argument))
	}
	return truncate(strings.Join(parts, " "), maxDiscoveryCommandBytes)
}

func discoveryArgumentShape(argument string) string {
	argument = strings.Trim(strings.TrimSpace(argument), `"'`)
	if argument == "" {
		return "[ARG]"
	}
	key := argument
	hasValue := false
	if index := strings.IndexAny(key, "=:"); index > 0 {
		key = key[:index]
		hasValue = true
	}
	key = strings.ToLower(key)
	if sensitiveDiscoveryOptions[key] {
		return key + "=[REDACTED]"
	}
	if safeDiscoveryOptions[key] {
		if hasValue {
			return key + "=[VALUE]"
		}
		return key
	}
	if strings.HasPrefix(argument, "-") || strings.HasPrefix(argument, "/") {
		return "[OPTION]"
	}
	return "[ARG]"
}

func managedRuntimeDetected(runtimeName string, environment []string) bool {
	for _, entry := range environment {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) != 2 {
			continue
		}
		name, value := strings.ToUpper(strings.TrimSpace(parts[0])), strings.ToLower(parts[1])
		normalizedValue := strings.ReplaceAll(value, "/", `\`)
		switch runtimeName {
		case "java":
			if name == "JAVA_TOOL_OPTIONS" && strings.Contains(value, "-javaagent:") && strings.Contains(value, "opentelemetry-javaagent.jar") {
				return true
			}
		case "node":
			if name == "NODE_OPTIONS" && strings.Contains(value, "--require") &&
				strings.Contains(normalizedValue, `\apm\instrumentation\node\bootstrap.js`) {
				return true
			}
		}
	}
	return false
}

func deploymentArtifactPath(runtimeName, executable, commandLine, cwd string) string {
	if runtimeName == "iis" {
		return ""
	}
	extensions := map[string][]string{
		"dotnet": {".dll", ".exe"}, "dotnet_framework": {".exe"},
		"java": {".jar"}, "node": {".js", ".cjs", ".mjs"},
	}[runtimeName]
	for _, raw := range commandArgumentPattern.FindAllString(commandLine, -1) {
		candidate := strings.Trim(strings.TrimSpace(raw), `"'`)
		lower := strings.ToLower(candidate)
		for _, extension := range extensions {
			if !strings.HasSuffix(lower, extension) {
				continue
			}
			if !filepath.IsAbs(candidate) && cwd != "" {
				candidate = filepath.Join(cwd, candidate)
			}
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
				absolute, _ := filepath.Abs(candidate)
				return filepath.Clean(absolute)
			}
		}
	}
	if info, err := os.Stat(executable); err == nil && !info.IsDir() {
		return filepath.Clean(executable)
	}
	return ""
}

func deploymentArtifactFingerprint(path string) (string, *time.Time) {
	if path == "" {
		return "", nil
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return "", nil
	}
	modified := info.ModTime().UTC()
	basis := fmt.Sprintf("%s\x00%d\x00%d", strings.ToLower(filepath.Clean(path)), info.Size(), modified.UnixNano())
	hash := sha256.Sum256([]byte(basis))
	return hex.EncodeToString(hash[:]), &modified
}

var iisAppPoolPattern = regexp.MustCompile(`(?i)(?:^|\s)-ap\s+(?:"([^"]+)"|([^\s]+))`)

func extractIISAppPool(commandLine string) string {
	match := iisAppPoolPattern.FindStringSubmatch(commandLine)
	if len(match) < 3 {
		return ""
	}
	if match[1] != "" {
		return strings.TrimSpace(match[1])
	}
	return strings.TrimSpace(match[2])
}

func classifyRuntime(name, exe, cmdline string) string {
	text := strings.ToLower(name + " " + exe + " " + cmdline)
	switch {
	case strings.Contains(text, "w3wp.exe"):
		return "iis"
	case strings.Contains(text, "dotnet.exe"):
		return "dotnet"
	case strings.Contains(text, "java.exe") || strings.Contains(text, "javaw.exe"):
		return "java"
	case strings.Contains(text, "node.exe"):
		return "node"
	case strings.Contains(text, "python.exe") || strings.Contains(text, "pythonw.exe"):
		return "python"
	default:
		return ""
	}
}

func first(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return strings.TrimSpace(value)
}

func normalizedAPMBind(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || value == "::1" {
		return "127.0.0.1"
	}
	return value
}

func quoteYAML(value string) string { return strconv.Quote(value) }
func netJoin(host string, port int) string {
	if strings.Contains(host, ":") {
		return fmt.Sprintf("[%s]:%d", host, port)
	}
	return fmt.Sprintf("%s:%d", host, port)
}
func portListening(host string, port int) bool {
	connection, err := net.DialTimeout("tcp", netJoin(host, port), 250*time.Millisecond)
	if err != nil {
		return false
	}
	_ = connection.Close()
	return true
}
func truncate(value string, max int) string {
	if len(value) <= max {
		return value
	}
	return value[:max]
}

func MarshalStatus(status *model.AgentAPMHeartbeat) []byte {
	data, _ := json.Marshal(status)
	return data
}
