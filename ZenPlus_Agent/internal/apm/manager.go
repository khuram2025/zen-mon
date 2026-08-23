package apm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
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
	"zenplus-agent/internal/secrets"
)

const gatewayVersion = "0.158.0-zp1"

type Manager struct {
	mu            sync.Mutex
	paths         agentruntime.Paths
	installDir    string
	cmd           *exec.Cmd
	closeJob      func()
	status        model.AgentAPMHeartbeat
	lastDiscovery time.Time
	logf          func(string, ...any)
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
		m.setFailure("waiting_authorization", "Waiting for appliance authorization before starting APM")
		return
	}

	key, err := m.ensureCredential(ctx, api, environment)
	if err != nil {
		m.setFailure("credential_error", err.Error())
		return
	}
	if err := m.ensureConfig(cfg, agentID, serverID); err != nil {
		m.setFailure("configuration_error", err.Error())
		return
	}
	if err := m.ensureStarted(ctx, key); err != nil {
		m.setFailure("failed", err.Error())
		return
	}
	m.refreshHealth()
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

func (m *Manager) Close() { m.stop("stopped") }

func (m *Manager) ensureCredential(ctx context.Context, api *client.Client, environment string) (string, error) {
	if protected, err := secrets.UnprotectFromFile(m.paths.APMCredential); err == nil && len(protected) > 0 {
		return string(protected), nil
	}
	var response enrollResponse
	_, _, err := api.PostJSON(ctx, "/api/v1/agents/apm/enroll", map[string]string{"environment": environment}, &response)
	if err != nil {
		return "", fmt.Errorf("obtain appliance-managed APM credential: %w", err)
	}
	if !strings.HasPrefix(response.Key, "zpi_") {
		return "", fmt.Errorf("controller returned an invalid APM credential")
	}
	if err := secrets.ProtectToFile(m.paths.APMCredential, []byte(response.Key)); err != nil {
		return "", fmt.Errorf("protect APM credential: %w", err)
	}
	return response.Key, nil
}

func (m *Manager) ensureConfig(cfg config.Config, agentID, serverID string) error {
	bind := first(cfg.APM.BindAddress, "127.0.0.1")
	if bind != "127.0.0.1" && bind != "::1" {
		return fmt.Errorf("refusing non-loopback OTLP bind address %q", bind)
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
  attributes/zenplus:
    actions:
      - key: zenplus.agent.id
        value: %s
        action: upsert
      - key: zenplus.server.id
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
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, attributes/zenplus, batch]
      exporters: [otlp_http/zenplus]
`, quoteYAML(netJoin(bind, 13133)), quoteYAML(m.paths.APMStorage), quoteYAML(netJoin(bind, 4317)),
		quoteYAML(netJoin(bind, 4318)), quoteYAML(agentID), quoteYAML(serverID), quoteYAML(endpoint), !cfg.VerifyTLS)

	if existing, err := os.ReadFile(m.paths.APMConfig); err == nil && string(existing) == content {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(m.paths.APMConfig), 0o700); err != nil {
		return err
	}
	tmp := m.paths.APMConfig + ".new"
	if err := os.WriteFile(tmp, []byte(content), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, m.paths.APMConfig)
}

func (m *Manager) ensureStarted(ctx context.Context, key string) error {
	m.mu.Lock()
	if m.cmd != nil && m.cmd.Process != nil && m.cmd.ProcessState == nil {
		m.mu.Unlock()
		return nil
	}
	m.mu.Unlock()

	binary := filepath.Join(m.installDir, "apm", "gateway", "zenplus-telemetry-gateway.exe")
	if goruntime.GOOS != "windows" {
		binary = filepath.Join(m.installDir, "apm", "gateway", "zenplus-telemetry-gateway")
	}
	if _, err := os.Stat(binary); err != nil {
		return fmt.Errorf("managed telemetry gateway is not installed: %s", binary)
	}
	logFile, err := os.OpenFile(m.paths.APMLog, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, binary, "--config", m.paths.APMConfig)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.Env = append(os.Environ(), "ZENPLUS_APM_KEY="+key, "GOMEMLIMIT=160MiB")
	setHiddenProcess(cmd)
	if err := cmd.Start(); err != nil {
		_ = logFile.Close()
		return fmt.Errorf("start managed telemetry gateway: %w", err)
	}
	closeJob, err := attachProcessLifecycle(cmd)
	if err != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		_ = logFile.Close()
		return fmt.Errorf("protect telemetry gateway lifecycle: %w", err)
	}
	m.mu.Lock()
	m.cmd = cmd
	m.closeJob = closeJob
	m.status.State = "starting"
	m.status.LastError = ""
	m.status.Gateway.Version = gatewayVersion
	m.status.Gateway.Managed = true
	m.status.Bundles = m.detectBundles()
	m.mu.Unlock()
	m.logf("APM telemetry gateway %s started pid=%d", gatewayVersion, cmd.Process.Pid)
	go func() {
		err := cmd.Wait()
		_ = logFile.Close()
		m.mu.Lock()
		if m.cmd == cmd {
			m.cmd = nil
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

func (m *Manager) refreshHealth() {
	client := &http.Client{Timeout: 750 * time.Millisecond}
	resp, err := client.Get("http://127.0.0.1:13133/")
	healthy := err == nil && resp.StatusCode >= 200 && resp.StatusCode < 300
	if resp != nil {
		_ = resp.Body.Close()
	}
	listening := portListening(4317) || portListening(4318)
	m.mu.Lock()
	m.status.Gateway.Listening = listening
	m.status.Gateway.Healthy = healthy
	m.status.CheckedAt = time.Now().UTC()
	if healthy && listening {
		m.status.State = "active"
		m.status.LastError = ""
	} else if m.status.State != "failed" {
		m.status.State = "starting"
	}
	m.mu.Unlock()
}

func (m *Manager) stop(state string) {
	m.mu.Lock()
	cmd := m.cmd
	closeJob := m.closeJob
	m.cmd = nil
	m.closeJob = nil
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
		detected := strings.Contains(lower, "opentelemetry") || strings.Contains(lower, "otel_") || strings.Contains(lower, "javaagent") || moduleFacts.OTelDetected
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
			Cmdline: truncate(cmdline, 16384), Runtime: runtimeName, ServiceNameGuess: truncate(serviceName, 255),
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

func quoteYAML(value string) string { return strconv.Quote(value) }
func netJoin(host string, port int) string {
	if strings.Contains(host, ":") {
		return fmt.Sprintf("[%s]:%d", host, port)
	}
	return fmt.Sprintf("%s:%d", host, port)
}
func portListening(port int) bool {
	connection, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 250*time.Millisecond)
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
