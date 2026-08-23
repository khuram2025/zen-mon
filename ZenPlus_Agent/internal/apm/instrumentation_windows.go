//go:build windows

package apm

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const dotnetProfilerID = "{918728DD-259F-4A6A-AC2B-B85E1B658318}"

type iisScriptPayload struct {
	Mode     string             `json:"mode"`
	Target   string             `json:"target"`
	Managed  map[string]string  `json:"managed,omitempty"`
	Previous map[string]*string `json:"previous,omitempty"`
	Restart  bool               `json:"restart"`
}

type iisScriptResult struct {
	Previous     map[string]*string `json:"previous"`
	Restarted    bool               `json:"restarted"`
	RestartError string             `json:"restart_error"`
}

func applyIISInstrumentation(ctx context.Context, statePath, bundleRoot string, request InstrumentationRequest) (InstrumentationResult, error) {
	state := instrumentationState{Version: 1, Targets: map[string]instrumentationTarget{}}
	if data, err := osReadFile(statePath); err == nil {
		_ = json.Unmarshal(data, &state)
	}
	if state.Targets == nil {
		state.Targets = map[string]instrumentationTarget{}
	}
	key := instrumentationTargetKey(request.TargetKind, request.TargetName)
	priorTarget := state.Targets[key]
	payload := iisScriptPayload{Target: request.TargetName, Restart: request.Restart}
	managed := dotnetEnvironment(bundleRoot, request.ServiceName, request.Environment)
	if request.Enabled {
		payload.Mode = "enable"
		payload.Managed = managed
	} else {
		payload.Mode = "disable"
		payload.Previous = priorTarget.Previous
		if !priorTarget.Enabled && len(priorTarget.Previous) == 0 {
			return InstrumentationResult{
				State: "none", TargetKind: request.TargetKind, TargetName: request.TargetName,
				ServiceName: request.ServiceName, Message: "IIS application pool is not managed by ZenPlus",
			}, nil
		}
	}
	result, err := runIISConfigurationScript(ctx, payload)
	if err != nil {
		return InstrumentationResult{}, err
	}
	now := time.Now().UTC()
	target := instrumentationTarget{
		TargetKind: request.TargetKind, TargetName: request.TargetName, ProcessKey: request.ProcessKey,
		Runtime:     request.Runtime,
		ServiceName: request.ServiceName, Environment: request.Environment, Enabled: request.Enabled,
		AppliedAt: now, Managed: managed,
	}
	if request.Enabled {
		if priorTarget.Enabled && len(priorTarget.Previous) > 0 {
			target.Previous = priorTarget.Previous
		} else {
			target.Previous = result.Previous
		}
	} else {
		target.Previous = nil
		target.Managed = nil
	}
	if result.Restarted {
		target.RestartedAt = &now
	}
	state.Targets[key] = target
	if err := writeInstrumentationState(statePath, state); err != nil {
		return InstrumentationResult{}, fmt.Errorf("persist IIS instrumentation state: %w", err)
	}
	if result.RestartError != "" {
		return InstrumentationResult{}, fmt.Errorf("IIS environment was updated but the application pool could not be recycled: %s", result.RestartError)
	}
	stateName := "none"
	message := "OpenTelemetry settings removed and the original application-pool environment restored"
	if request.Enabled {
		stateName = "pending"
		message = "OpenTelemetry settings applied; recycle the application pool to activate tracing"
		if result.Restarted {
			stateName = "active"
			message = "OpenTelemetry settings applied and the application pool recycled"
		}
	}
	return InstrumentationResult{
		State: stateName, TargetKind: request.TargetKind, TargetName: request.TargetName,
		ServiceName: request.ServiceName, Restarted: result.Restarted,
		Rollback: request.Enabled && len(target.Previous) > 0, Message: message,
	}, nil
}

func dotnetEnvironment(root, serviceName, environment string) map[string]string {
	native64 := filepath.Join(root, "win-x64", "OpenTelemetry.AutoInstrumentation.Native.dll")
	native32 := filepath.Join(root, "win-x86", "OpenTelemetry.AutoInstrumentation.Native.dll")
	values := map[string]string{
		"COR_ENABLE_PROFILING":            "1",
		"COR_PROFILER":                    dotnetProfilerID,
		"COR_PROFILER_PATH_64":            native64,
		"CORECLR_ENABLE_PROFILING":        "1",
		"CORECLR_PROFILER":                dotnetProfilerID,
		"CORECLR_PROFILER_PATH_64":        native64,
		"DOTNET_ADDITIONAL_DEPS":          filepath.Join(root, "AdditionalDeps"),
		"DOTNET_SHARED_STORE":             filepath.Join(root, "store"),
		"DOTNET_STARTUP_HOOKS":            filepath.Join(root, "net", "OpenTelemetry.AutoInstrumentation.StartupHook.dll"),
		"OTEL_DOTNET_AUTO_HOME":           root,
		"OTEL_DOTNET_AUTO_TRACES_ENABLED": "true",
		"OTEL_EXPORTER_OTLP_ENDPOINT":     "http://127.0.0.1:4318",
		"OTEL_EXPORTER_OTLP_PROTOCOL":     "http/protobuf",
		"OTEL_TRACES_EXPORTER":            "otlp",
		"OTEL_METRICS_EXPORTER":           "none",
		"OTEL_LOGS_EXPORTER":              "none",
		// Privacy-first defaults: query parameters and literals are never
		// required for database RED/digest insights.
		"OTEL_INSTRUMENTATION_COMMON_DB_STATEMENT_SANITIZER_ENABLED": "true",
		"OTEL_DOTNET_AUTO_SQLCLIENT_SET_DBSTATEMENT_FOR_TEXT":        "false",
		"OTEL_SERVICE_NAME":        serviceName,
		"OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=" + environment,
	}
	if _, err := osStat(native32); err == nil {
		values["COR_PROFILER_PATH_32"] = native32
		values["CORECLR_PROFILER_PATH_32"] = native32
	}
	return values
}

// These variables make the filesystem probes replaceable in unit tests
// without introducing a broad platform abstraction.
var osReadFile = func(path string) ([]byte, error) { return os.ReadFile(path) }
var osStat = os.Stat

func runIISConfigurationScript(ctx context.Context, payload iisScriptPayload) (iisScriptResult, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return iisScriptResult{}, err
	}
	payload64 := base64.StdEncoding.EncodeToString(data)
	script := strings.ReplaceAll(iisConfigurationScript, "__PAYLOAD__", payload64)
	encoded := base64.StdEncoding.EncodeToString([]byte(stringsToUTF16LE(script)))
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded)
	setHiddenProcess(cmd)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return iisScriptResult{}, fmt.Errorf("configure IIS application pool: %w: %s", err, strings.TrimSpace(string(output)))
	}
	var result iisScriptResult
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(output))), &result); err != nil {
		return iisScriptResult{}, fmt.Errorf("decode IIS configuration result: %w", err)
	}
	return result, nil
}

func stringsToUTF16LE(value string) string {
	runes := []rune(value)
	bytes := make([]byte, 0, len(runes)*2)
	for _, r := range runes {
		if r <= 0xffff {
			bytes = append(bytes, byte(r), byte(r>>8))
		} else {
			r -= 0x10000
			hi := uint16(0xd800 + (r >> 10))
			lo := uint16(0xdc00 + (r & 0x3ff))
			bytes = append(bytes, byte(hi), byte(hi>>8), byte(lo), byte(lo>>8))
		}
	}
	return string(bytes)
}

const iisConfigurationScript = `$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$payloadJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))
$payload = $payloadJson | ConvertFrom-Json
$administrationAssembly = Join-Path $env:windir 'System32\inetsrv\Microsoft.Web.Administration.dll'
if (Test-Path -LiteralPath $administrationAssembly) {
  Add-Type -Path $administrationAssembly | Out-Null
} else {
  Add-Type -AssemblyName 'Microsoft.Web.Administration' | Out-Null
}
$manager = New-Object Microsoft.Web.Administration.ServerManager
try {
  $config = $manager.GetApplicationHostConfiguration()
  $section = $config.GetSection('system.applicationHost/applicationPools')
  $pools = $section.GetCollection()
  $poolElement = $null
  foreach ($candidate in $pools) {
    if ([string]$candidate['name'] -eq [string]$payload.target) { $poolElement = $candidate; break }
  }
  if ($null -eq $poolElement) { throw "IIS application pool '$($payload.target)' was not found" }
  $environmentVariables = $poolElement.GetCollection('environmentVariables')
  $previous = @{}
  if ($payload.mode -eq 'enable') {
    foreach ($property in $payload.managed.PSObject.Properties) {
      $name = [string]$property.Name
      $entry = $null
      foreach ($candidate in $environmentVariables) {
        if ([string]$candidate['name'] -eq $name) { $entry = $candidate; break }
      }
      if ($null -eq $entry) {
        $previous[$name] = $null
        $entry = $environmentVariables.CreateElement('add')
        $entry['name'] = $name
        $entry['value'] = [string]$property.Value
        $environmentVariables.Add($entry)
      } else {
        $previous[$name] = [string]$entry['value']
        $entry['value'] = [string]$property.Value
      }
    }
  } elseif ($payload.mode -eq 'disable') {
    foreach ($property in $payload.previous.PSObject.Properties) {
      $name = [string]$property.Name
      $entry = $null
      foreach ($candidate in $environmentVariables) {
        if ([string]$candidate['name'] -eq $name) { $entry = $candidate; break }
      }
      if ($null -eq $property.Value) {
        if ($null -ne $entry) { $environmentVariables.Remove($entry) }
      } elseif ($null -eq $entry) {
        $entry = $environmentVariables.CreateElement('add')
        $entry['name'] = $name
        $entry['value'] = [string]$property.Value
        $environmentVariables.Add($entry)
      } else {
        $entry['value'] = [string]$property.Value
      }
    }
  } else { throw "Unsupported IIS instrumentation mode '$($payload.mode)'" }
  $manager.CommitChanges()
  $restarted = $false
  $restartError = ''
  if ([bool]$payload.restart) {
    try {
      $manager.Dispose()
      $manager = New-Object Microsoft.Web.Administration.ServerManager
      $pool = $manager.ApplicationPools[[string]$payload.target]
      if ($null -eq $pool) { throw "IIS application pool disappeared before recycle" }
      [void]$pool.Recycle()
      $restarted = $true
    } catch {
      $restartError = $_.Exception.Message
    }
  }
  @{ previous = $previous; restarted = $restarted; restart_error = $restartError } | ConvertTo-Json -Compress -Depth 5
} finally {
  if ($null -ne $manager) { $manager.Dispose() }
}`
