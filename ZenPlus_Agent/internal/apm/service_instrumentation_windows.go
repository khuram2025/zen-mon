//go:build windows

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

	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

var protectedWindowsServices = map[string]struct{}{
	"cryptsvc": {}, "dcomlaunch": {}, "dhcp": {}, "dnscache": {},
	"eventlog": {}, "lanmanserver": {}, "lanmanworkstation": {},
	"lsm": {}, "mpssvc": {}, "netlogon": {}, "plugplay": {},
	"power": {}, "rpcss": {}, "samss": {}, "schedule": {},
	"securityhealthservice": {}, "sens": {}, "systemeventsbroker": {},
	"trustedinstaller": {}, "winmgmt": {}, "wuauserv": {},
}

func applyWindowsServiceInstrumentation(ctx context.Context, statePath, bundlePath string, request InstrumentationRequest) (InstrumentationResult, error) {
	serviceName := strings.TrimSpace(request.TargetName)
	if err := validateManagedWindowsService(serviceName); err != nil {
		return InstrumentationResult{}, err
	}
	manager, err := mgr.Connect()
	if err != nil {
		return InstrumentationResult{}, fmt.Errorf("connect to Windows Service Control Manager: %w", err)
	}
	defer manager.Disconnect()
	service, err := manager.OpenService(serviceName)
	if err != nil {
		return InstrumentationResult{}, fmt.Errorf("open Windows service %q: %w", serviceName, err)
	}
	defer service.Close()
	config, err := service.Config()
	if err != nil {
		return InstrumentationResult{}, fmt.Errorf("read Windows service %q configuration: %w", serviceName, err)
	}
	if strings.EqualFold(strings.TrimSpace(config.ServiceStartName), "LocalSystem") && isProtectedService(serviceName) {
		return InstrumentationResult{}, fmt.Errorf("Windows service %q is protected from managed instrumentation", serviceName)
	}

	state := instrumentationState{Version: 2, Targets: map[string]instrumentationTarget{}}
	if data, readErr := os.ReadFile(statePath); readErr == nil {
		_ = jsonUnmarshal(data, &state)
	}
	if state.Targets == nil {
		state.Targets = map[string]instrumentationTarget{}
	}
	if state.Version < 2 {
		state.Version = 2
	}
	key := instrumentationTargetKey(request.TargetKind, serviceName)
	priorTarget := state.Targets[key]
	entries, err := readWindowsServiceEnvironment(serviceName)
	if err != nil {
		return InstrumentationResult{}, err
	}

	managed := runtimeServiceEnvironment(request.Runtime, bundlePath, request.ServiceName, request.Environment, entries)
	if !request.Enabled && !priorTarget.Enabled && len(priorTarget.Previous) == 0 {
		return InstrumentationResult{
			State: "none", TargetKind: request.TargetKind, TargetName: serviceName,
			ServiceName: request.ServiceName, Message: "Windows service is not managed by ZenPlus",
		}, nil
	}
	previous := priorTarget.Previous
	if request.Enabled && (!priorTarget.Enabled || len(previous) == 0) {
		previous = captureEnvironmentValues(entries, managed)
	}
	if request.Enabled {
		entries = setEnvironmentValues(entries, managed)
	} else {
		entries = restoreEnvironmentValues(entries, previous)
	}
	if err := writeWindowsServiceEnvironment(serviceName, entries); err != nil {
		return InstrumentationResult{}, err
	}

	now := time.Now().UTC()
	target := instrumentationTarget{
		TargetKind: request.TargetKind, TargetName: serviceName, Runtime: request.Runtime,
		ProcessKey: request.ProcessKey, ServiceName: request.ServiceName,
		Environment: request.Environment, Enabled: request.Enabled, AppliedAt: now,
		Managed: managed,
	}
	if request.Enabled {
		target.Previous = previous
	}
	state.Targets[key] = target
	if err := writeInstrumentationState(statePath, state); err != nil {
		return InstrumentationResult{}, fmt.Errorf("persist Windows-service instrumentation state: %w", err)
	}

	restarted := false
	if request.Restart {
		restarted, err = restartWindowsService(ctx, service)
		if restarted {
			target.RestartedAt = &now
			state.Targets[key] = target
			_ = writeInstrumentationState(statePath, state)
		}
		if err != nil {
			return InstrumentationResult{}, fmt.Errorf("service environment was updated and rollback is available, but restart failed: %w", err)
		}
	}

	stateName := "none"
	message := "OpenTelemetry settings removed and the original service environment restored"
	if request.Enabled {
		stateName = "pending"
		message = "OpenTelemetry settings applied; restart the Windows service to activate tracing"
		if restarted {
			stateName = "active"
			message = "OpenTelemetry settings applied and the Windows service restarted"
		}
	}
	return InstrumentationResult{
		State: stateName, TargetKind: request.TargetKind, TargetName: serviceName,
		ServiceName: request.ServiceName, Restarted: restarted,
		Rollback: request.Enabled && len(previous) > 0, Message: message,
	}, nil
}

// jsonUnmarshal is replaceable in Windows unit tests alongside the existing
// filesystem probes in instrumentation_windows.go.
var jsonUnmarshal = func(data []byte, value any) error { return json.Unmarshal(data, value) }

func validateManagedWindowsService(name string) error {
	if name == "" || len(name) > 255 || strings.ContainsAny(name, "\\/\r\n\x00") {
		return fmt.Errorf("invalid Windows service name")
	}
	if isProtectedService(name) || strings.HasPrefix(strings.ToLower(name), "zenplus") {
		return fmt.Errorf("Windows service %q is protected from managed instrumentation", name)
	}
	return nil
}

func isProtectedService(name string) bool {
	_, found := protectedWindowsServices[strings.ToLower(strings.TrimSpace(name))]
	return found
}

func runtimeServiceEnvironment(runtimeName, bundlePath, serviceName, environment string, current []string) map[string]string {
	common := map[string]string{
		"OTEL_EXPORTER_OTLP_ENDPOINT":                                "http://127.0.0.1:4318",
		"OTEL_EXPORTER_OTLP_PROTOCOL":                                "http/protobuf",
		"OTEL_TRACES_EXPORTER":                                       "otlp",
		"OTEL_METRICS_EXPORTER":                                      "none",
		"OTEL_LOGS_EXPORTER":                                         "none",
		"OTEL_INSTRUMENTATION_COMMON_DB_STATEMENT_SANITIZER_ENABLED": "true",
		"OTEL_SERVICE_NAME":                                          serviceName,
		"OTEL_RESOURCE_ATTRIBUTES":                                   "deployment.environment=" + environment,
	}
	switch runtimeName {
	case "dotnet", "dotnet_framework":
		for key, value := range dotnetEnvironment(bundlePath, serviceName, environment) {
			common[key] = value
		}
	case "java":
		currentValue, _ := findEnvironmentValue(current, "JAVA_TOOL_OPTIONS")
		token := `-javaagent:"` + bundlePath + `"`
		common["JAVA_TOOL_OPTIONS"] = appendLaunchOption(currentValue, token)
	case "node":
		currentValue, _ := findEnvironmentValue(current, "NODE_OPTIONS")
		token := `--require="` + filepath.Join(bundlePath, "bootstrap.js") + `"`
		common["NODE_OPTIONS"] = appendLaunchOption(currentValue, token)
	}
	return common
}

func appendLaunchOption(current, token string) string {
	current = strings.TrimSpace(current)
	if strings.Contains(strings.ToLower(current), strings.ToLower(token)) {
		return current
	}
	if current == "" {
		return token
	}
	return current + " " + token
}

func serviceRegistryPath(serviceName string) string {
	return `SYSTEM\CurrentControlSet\Services\` + serviceName
}

func readWindowsServiceEnvironment(serviceName string) ([]string, error) {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, serviceRegistryPath(serviceName), registry.QUERY_VALUE)
	if err != nil {
		return nil, fmt.Errorf("open Windows service registry key %q: %w", serviceName, err)
	}
	defer key.Close()
	values, _, err := key.GetStringsValue("Environment")
	if errors.Is(err, registry.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read Windows service %q environment: %w", serviceName, err)
	}
	return values, nil
}

func writeWindowsServiceEnvironment(serviceName string, values []string) error {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, serviceRegistryPath(serviceName), registry.QUERY_VALUE|registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("open Windows service registry key %q for update: %w", serviceName, err)
	}
	defer key.Close()
	if len(values) == 0 {
		if err := key.DeleteValue("Environment"); err != nil && !errors.Is(err, registry.ErrNotExist) {
			return fmt.Errorf("remove empty Windows service %q environment: %w", serviceName, err)
		}
		return nil
	}
	if err := key.SetStringsValue("Environment", values); err != nil {
		return fmt.Errorf("write Windows service %q environment: %w", serviceName, err)
	}
	return nil
}

func findEnvironmentValue(entries []string, name string) (string, bool) {
	for _, entry := range entries {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) == 2 && strings.EqualFold(strings.TrimSpace(parts[0]), name) {
			return parts[1], true
		}
	}
	return "", false
}

func captureEnvironmentValues(entries []string, managed map[string]string) map[string]*string {
	previous := make(map[string]*string, len(managed))
	for name := range managed {
		if value, found := findEnvironmentValue(entries, name); found {
			copyValue := value
			previous[name] = &copyValue
		} else {
			previous[name] = nil
		}
	}
	return previous
}

func setEnvironmentValues(entries []string, values map[string]string) []string {
	result := append([]string(nil), entries...)
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, name := range keys {
		result = setEnvironmentValue(result, name, values[name])
	}
	return result
}

func restoreEnvironmentValues(entries []string, previous map[string]*string) []string {
	result := append([]string(nil), entries...)
	keys := make([]string, 0, len(previous))
	for key := range previous {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, name := range keys {
		if previous[name] == nil {
			result = removeEnvironmentValue(result, name)
		} else {
			result = setEnvironmentValue(result, name, *previous[name])
		}
	}
	return result
}

func setEnvironmentValue(entries []string, name, value string) []string {
	result := make([]string, 0, len(entries)+1)
	replaced := false
	for _, entry := range entries {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) == 2 && strings.EqualFold(strings.TrimSpace(parts[0]), name) {
			if !replaced {
				result = append(result, name+"="+value)
				replaced = true
			}
			continue
		}
		result = append(result, entry)
	}
	if !replaced {
		result = append(result, name+"="+value)
	}
	return result
}

func removeEnvironmentValue(entries []string, name string) []string {
	result := make([]string, 0, len(entries))
	for _, entry := range entries {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) == 2 && strings.EqualFold(strings.TrimSpace(parts[0]), name) {
			continue
		}
		result = append(result, entry)
	}
	return result
}

func restartWindowsService(ctx context.Context, service *mgr.Service) (bool, error) {
	status, err := service.Query()
	if err != nil {
		return false, err
	}
	if status.State == svc.Stopped {
		// Preserve the operator's stopped state. The new environment activates
		// on the next normal start rather than unexpectedly starting a service.
		return false, nil
	}
	if status.State != svc.Running {
		return false, fmt.Errorf("service is not in a restartable state (%d)", status.State)
	}
	if _, err := service.Control(svc.Stop); err != nil {
		return false, fmt.Errorf("request service stop: %w", err)
	}
	if err := waitForServiceState(ctx, service, svc.Stopped, 45*time.Second); err != nil {
		return false, err
	}
	if err := service.Start(); err != nil {
		return false, fmt.Errorf("start service: %w", err)
	}
	if err := waitForServiceState(ctx, service, svc.Running, 45*time.Second); err != nil {
		return false, err
	}
	return true, nil
}

func waitForServiceState(ctx context.Context, service *mgr.Service, wanted svc.State, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		status, err := service.Query()
		if err != nil {
			return err
		}
		if status.State == wanted {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
	return fmt.Errorf("timed out waiting for service state %d", wanted)
}
