package apm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// InstrumentationRequest is the controller-to-agent contract for reversible
// runtime instrumentation. P1 supports IIS application pools; P2 adds Windows
// services running .NET, Java, or Node.js. P3 keeps database telemetry
// privacy-first and advertises profiling only when a real profiler is shipped.
// Application files are never edited.
type InstrumentationRequest struct {
	Enabled     bool
	Runtime     string
	ProcessKey  string
	TargetKind  string
	TargetName  string
	ServiceName string
	Environment string
	Restart     bool
}

type InstrumentationResult struct {
	State       string `json:"instrumentation_state"`
	TargetKind  string `json:"target_kind"`
	TargetName  string `json:"target_name"`
	ServiceName string `json:"service_name"`
	Restarted   bool   `json:"restarted"`
	Rollback    bool   `json:"rollback_available"`
	Message     string `json:"message"`
}

type instrumentationState struct {
	Version int                              `json:"version"`
	Targets map[string]instrumentationTarget `json:"targets"`
}

type instrumentationTarget struct {
	TargetKind  string `json:"target_kind"`
	TargetName  string `json:"target_name"`
	Runtime     string `json:"runtime,omitempty"`
	ProcessKey  string `json:"process_key,omitempty"`
	ServiceName string `json:"service_name"`
	Environment string `json:"environment"`
	Enabled     bool   `json:"enabled"`
	// PendingRestart means the managed environment has been removed but a
	// still-running target may have the profiler/agent loaded. Runtime files
	// must remain available until that target stops or restarts.
	PendingRestart bool               `json:"pending_restart,omitempty"`
	AppliedAt      time.Time          `json:"applied_at"`
	RestartedAt    *time.Time         `json:"restarted_at,omitempty"`
	Previous       map[string]*string `json:"previous,omitempty"`
	Managed        map[string]string  `json:"managed,omitempty"`
	LastError      string             `json:"last_error,omitempty"`
	LastPID        int32              `json:"last_pid,omitempty"`
	Restarts       []time.Time        `json:"restart_history,omitempty"`
}

func (m *Manager) Instrument(ctx context.Context, request InstrumentationRequest) (InstrumentationResult, error) {
	request.Runtime = strings.ToLower(strings.TrimSpace(request.Runtime))
	request.TargetKind = strings.ToLower(strings.TrimSpace(request.TargetKind))
	request.TargetName = strings.TrimSpace(request.TargetName)
	request.ProcessKey = strings.TrimSpace(request.ProcessKey)
	request.ServiceName = strings.TrimSpace(request.ServiceName)
	request.Environment = strings.TrimSpace(request.Environment)
	supported := (request.Runtime == "iis" && request.TargetKind == "iis_app_pool") ||
		(request.TargetKind == "windows_service" && (request.Runtime == "dotnet" || request.Runtime == "dotnet_framework" || request.Runtime == "java" || request.Runtime == "node"))
	if !supported {
		return InstrumentationResult{}, fmt.Errorf("managed instrumentation supports IIS, .NET, Java, and Node.js Windows services only")
	}
	if request.TargetName == "" || len(request.TargetName) > 255 || strings.ContainsAny(request.TargetName, "\r\n\x00") ||
		(request.TargetKind == "windows_service" && strings.ContainsAny(request.TargetName, "\\/")) {
		return InstrumentationResult{}, fmt.Errorf("invalid instrumentation target name")
	}
	if request.ServiceName == "" {
		request.ServiceName = request.TargetName
	}
	if len(request.ServiceName) > 255 || strings.ContainsAny(request.ServiceName, "\r\n\x00") {
		return InstrumentationResult{}, fmt.Errorf("invalid service name")
	}
	if request.Environment == "" {
		request.Environment = "prod"
	}
	if len(request.Environment) > 64 || strings.ContainsAny(request.Environment, "\r\n,=\x00") {
		return InstrumentationResult{}, fmt.Errorf("invalid deployment environment")
	}

	bundleRoot := instrumentationBundlePath(m.installDir, request.Runtime)
	if request.Enabled {
		if err := validateRuntimeBundle(request.Runtime, bundleRoot); err != nil {
			return InstrumentationResult{}, err
		}
	}
	var result InstrumentationResult
	var err error
	if request.TargetKind == "iis_app_pool" {
		result, err = applyIISInstrumentation(ctx, m.paths.APMInstrumentationState, bundleRoot, request)
	} else {
		result, err = applyWindowsServiceInstrumentation(ctx, m.paths.APMInstrumentationState, bundleRoot, request)
	}
	if err != nil {
		m.recordInstrumentationFailure(request, err)
		return InstrumentationResult{}, err
	}
	m.logf("APM instrumentation kind=%s runtime=%s target=%q enabled=%t restarted=%t state=%s", request.TargetKind, request.Runtime, request.TargetName, request.Enabled, result.Restarted, result.State)
	// Command results are sent immediately; allow the next 10-second reconcile
	// to also refresh the controller inventory instead of waiting a minute.
	m.mu.Lock()
	m.lastDiscovery = time.Time{}
	m.mu.Unlock()
	return result, nil
}

func instrumentationBundlePath(installDir, runtimeName string) string {
	switch runtimeName {
	case "iis", "dotnet", "dotnet_framework":
		return filepath.Join(installDir, "apm", "instrumentation", "dotnet")
	case "java":
		return filepath.Join(installDir, "apm", "instrumentation", "java", "opentelemetry-javaagent.jar")
	case "node":
		return filepath.Join(installDir, "apm", "instrumentation", "node")
	default:
		return ""
	}
}

func validateRuntimeBundle(runtimeName, path string) error {
	switch runtimeName {
	case "iis", "dotnet", "dotnet_framework":
		return validateDotnetBundle(path)
	case "java":
		if info, err := os.Stat(path); err != nil || info.IsDir() {
			return fmt.Errorf("offline Java auto-instrumentation bundle is incomplete: %s", path)
		}
	case "node":
		required := []string{
			filepath.Join(path, "bootstrap.js"),
			filepath.Join(path, "node_modules", "@opentelemetry", "auto-instrumentations-node", "package.json"),
		}
		for _, item := range required {
			if info, err := os.Stat(item); err != nil || info.IsDir() {
				return fmt.Errorf("offline Node.js auto-instrumentation bundle is incomplete: %s", item)
			}
		}
	}
	return nil
}

func validateDotnetBundle(root string) error {
	required := []string{
		filepath.Join(root, "net", "OpenTelemetry.AutoInstrumentation.StartupHook.dll"),
		filepath.Join(root, "win-x64", "OpenTelemetry.AutoInstrumentation.Native.dll"),
	}
	for _, path := range required {
		if info, err := os.Stat(path); err != nil || info.IsDir() {
			return fmt.Errorf("offline .NET auto-instrumentation bundle is incomplete: %s", path)
		}
	}
	return nil
}

func (m *Manager) loadInstrumentationState() instrumentationState {
	state := instrumentationState{Version: 1, Targets: map[string]instrumentationTarget{}}
	data, err := os.ReadFile(m.paths.APMInstrumentationState)
	if err != nil {
		return state
	}
	if json.Unmarshal(data, &state) != nil || state.Targets == nil {
		return instrumentationState{Version: 1, Targets: map[string]instrumentationTarget{}}
	}
	return state
}

func (m *Manager) recordInstrumentationFailure(request InstrumentationRequest, cause error) {
	state := m.loadInstrumentationState()
	key := instrumentationTargetKey(request.TargetKind, request.TargetName)
	target := state.Targets[key]
	target.TargetKind = request.TargetKind
	target.TargetName = request.TargetName
	target.Runtime = request.Runtime
	target.ProcessKey = request.ProcessKey
	target.ServiceName = request.ServiceName
	target.Environment = request.Environment
	target.LastError = cause.Error()
	state.Targets[key] = target
	_ = writeInstrumentationState(m.paths.APMInstrumentationState, state)
}

func instrumentationTargetKey(kind, name string) string {
	return strings.ToLower(strings.TrimSpace(kind)) + ":" + strings.ToLower(strings.TrimSpace(name))
}

func writeInstrumentationState(path string, state instrumentationState) error {
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".new"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// ManagedIISTargets returns the enabled IIS application pools whose runtime
// files are owned by this installation. Installers use it to stop only the
// affected pools during an upgrade instead of disrupting the whole IIS host.
func ManagedIISTargets(statePath string) ([]string, error) {
	state, err := readInstrumentationState(statePath)
	if err != nil {
		return nil, err
	}
	targets := make([]string, 0)
	for _, target := range state.Targets {
		if (target.Enabled || target.PendingRestart) && target.TargetKind == "iis_app_pool" && strings.TrimSpace(target.TargetName) != "" {
			targets = append(targets, target.TargetName)
		}
	}
	sort.Strings(targets)
	return targets, nil
}

// ManagedWindowsServiceTargets returns enabled application services that load
// runtime files from this installation and therefore must be stopped briefly
// during an in-place agent upgrade.
func ManagedWindowsServiceTargets(statePath string) ([]string, error) {
	state, err := readInstrumentationState(statePath)
	if err != nil {
		return nil, err
	}
	targets := make([]string, 0)
	for _, target := range state.Targets {
		if (target.Enabled || target.PendingRestart) && target.TargetKind == "windows_service" && strings.TrimSpace(target.TargetName) != "" {
			targets = append(targets, target.TargetName)
		}
	}
	sort.Strings(targets)
	return targets, nil
}

// ManagedInstrumentationCount reports how many application targets still
// depend on the local APM gateway and bundled runtime files.
func ManagedInstrumentationCount(statePath string) (int, error) {
	state, err := readInstrumentationState(statePath)
	if err != nil {
		return 0, err
	}
	count := 0
	for _, target := range state.Targets {
		if target.Enabled || target.PendingRestart {
			count++
		}
	}
	return count, nil
}

// RollbackAll restores every application target currently managed by
// ZenPlus. It must run before uninstall removes the bundled profiler/runtime
// files; otherwise monitored applications retain references to missing files.
func RollbackAll(ctx context.Context, statePath, installDir string, restart bool) error {
	state, err := readInstrumentationState(statePath)
	if err != nil {
		return err
	}
	keys := make([]string, 0, len(state.Targets))
	for key, target := range state.Targets {
		if target.Enabled || target.PendingRestart {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	var rollbackErrors []error
	for _, key := range keys {
		target := state.Targets[key]
		runtimeName := target.Runtime
		if runtimeName == "" && target.TargetKind == "iis_app_pool" {
			runtimeName = "iis"
		}
		request := InstrumentationRequest{
			Enabled:     false,
			Runtime:     runtimeName,
			ProcessKey:  target.ProcessKey,
			TargetKind:  target.TargetKind,
			TargetName:  target.TargetName,
			ServiceName: target.ServiceName,
			Environment: target.Environment,
			Restart:     restart,
		}
		bundlePath := instrumentationBundlePath(installDir, runtimeName)
		if target.TargetKind == "iis_app_pool" {
			_, err = applyIISInstrumentation(ctx, statePath, bundlePath, request)
		} else if target.TargetKind == "windows_service" {
			_, err = applyWindowsServiceInstrumentation(ctx, statePath, bundlePath, request)
		} else {
			err = fmt.Errorf("unsupported managed target kind %q", target.TargetKind)
		}
		if err != nil {
			rollbackErrors = append(rollbackErrors, fmt.Errorf("restore %s %q: %w", target.TargetKind, target.TargetName, err))
		}
	}
	return errors.Join(rollbackErrors...)
}

func readInstrumentationState(path string) (instrumentationState, error) {
	state := instrumentationState{Version: 1, Targets: map[string]instrumentationTarget{}}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return state, nil
	}
	if err != nil {
		return state, fmt.Errorf("read instrumentation state: %w", err)
	}
	if err := json.Unmarshal(data, &state); err != nil {
		return instrumentationState{}, fmt.Errorf("parse instrumentation state: %w", err)
	}
	if state.Targets == nil {
		state.Targets = map[string]instrumentationTarget{}
	}
	return state, nil
}
