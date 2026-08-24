package collectors

import (
	"context"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"zenplus-agent/internal/config"
)

func TestDecodeServicesAcceptsArray(t *testing.T) {
	services, err := decodeServices([]byte(`{
		"services": [
			{
				"service_name": "W3SVC",
				"display_name": "World Wide Web Publishing Service",
				"state": "Running",
				"start_mode": "Auto",
				"pid": 1234,
				"exit_code": 0,
				"description": "Web server"
			}
		]
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(services) != 1 {
		t.Fatalf("got %d services", len(services))
	}
	if services[0].ServiceName != "W3SVC" {
		t.Fatalf("unexpected service name %q", services[0].ServiceName)
	}
	if services[0].State != "running" {
		t.Fatalf("state was not normalized: %q", services[0].State)
	}
	if services[0].StartMode != "auto" {
		t.Fatalf("start_mode was not normalized: %q", services[0].StartMode)
	}
}

func TestDecodeServicesAcceptsSingleObject(t *testing.T) {
	services, err := decodeServices([]byte(`{
		"services": {
			"service_name": "Spooler",
			"display_name": "Print Spooler",
			"state": "Stopped",
			"start_mode": "Manual",
			"pid": 0,
			"exit_code": 0
		}
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(services) != 1 {
		t.Fatalf("got %d services", len(services))
	}
	if services[0].ServiceName != "Spooler" || services[0].State != "stopped" || services[0].StartMode != "manual" {
		t.Fatalf("unexpected service decode: %+v", services[0])
	}
}

func TestAppendMissingWatchedServices(t *testing.T) {
	services := appendMissingWatchedServices([]serviceInfo{{ServiceName: "W3SVC"}}, []string{"W3SVC", "MSSQLSERVER"})
	if len(services) != 2 {
		t.Fatalf("got %d services", len(services))
	}
	if services[1].ServiceName != "MSSQLSERVER" {
		t.Fatalf("missing service not appended: %+v", services)
	}
	if services[1].State != "not_found" {
		t.Fatalf("missing service state = %q", services[1].State)
	}
}

func TestCollectIncludesWindowsServiceInventoryAndState(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows service collector")
	}
	resetServiceTestState()
	t.Cleanup(resetServiceTestState)

	cfg := config.Default()
	cfg.Collectors.CPU.Enabled = false
	cfg.Collectors.Memory.Enabled = false
	cfg.Collectors.Filesystem.Enabled = false
	cfg.Collectors.DiskIO.Enabled = false
	cfg.Collectors.Network.Enabled = false
	cfg.Collectors.Processes.Enabled = false
	cfg.Collectors.EventLog.Enabled = false
	cfg.Collectors.Inventory.Enabled = false
	cfg.Collectors.Services.Enabled = true
	cfg.Collectors.Services.Watchlist = nil

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	result := Collect(ctx, cfg)
	if err := result.Errors["service_state"]; err != "" {
		t.Fatalf("service_state error: %s", err)
	}
	services, ok := result.Inventory["services"].([]map[string]any)
	if !ok || len(services) == 0 {
		t.Fatalf("service inventory missing: %#v", result.Inventory["services"])
	}
	serviceSamples := 0
	for _, metric := range result.Metrics {
		if metric.Kind == "service_state" {
			serviceSamples++
		}
	}
	if serviceSamples == 0 {
		t.Fatalf("expected service_state samples, got %d metrics", len(result.Metrics))
	}
}

func resetServiceTestState() {
	rateState.Lock()
	defer rateState.Unlock()
	rateState.services = map[string]string{}
}

func TestEventLogScriptKeepsControllerFilterPairingsAndIDs(t *testing.T) {
	filters := []config.EventLogFilter{
		{Channel: "System", Levels: []string{"Error"}, IDs: []int{41, 6008}},
		{Channel: "Application", Levels: []string{"Warning", "Critical"}, IDs: []int{1000}},
	}
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = &filters
	// These legacy fields must not broaden an authoritative filter list.
	cfg.Channels = []string{"Security"}
	cfg.Levels = []string{"Verbose"}

	script, err := eventLogScript(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"[pscustomobject]@{ channel='System'; levels=@('Error'); ids=@(41,6008) }",
		"[pscustomobject]@{ channel='Application'; levels=@('Warning','Critical'); ids=@(1000) }",
		"$query['Id'] = @($eventIds | ForEach-Object { [int]$_ })",
		"Get-WinEvent -FilterHashtable $query",
	} {
		if !strings.Contains(script, want) {
			t.Errorf("event-log script missing %q:\n%s", want, script)
		}
	}
	for _, forbidden := range []string{"channel='Security'", "levels=@('Verbose')", "$channels =", "$levels ="} {
		if strings.Contains(script, forbidden) {
			t.Errorf("event-log script contains broadened legacy policy %q:\n%s", forbidden, script)
		}
	}
}

func TestEventLogFiltersFallBackToLegacyChannelsAndLevels(t *testing.T) {
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = nil
	cfg.Channels = []string{"System", "Application"}
	cfg.Levels = []string{"Error", "Warning"}

	want := []config.EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}},
		{Channel: "Application", Levels: []string{"Error", "Warning"}},
	}
	if got := effectiveEventLogFilters(cfg); !reflect.DeepEqual(got, want) {
		t.Fatalf("effective filters = %#v, want %#v", got, want)
	}
	script, err := eventLogScript(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, wantLiteral := range []string{
		"channel='System'; levels=@('Error','Warning'); ids=@()",
		"channel='Application'; levels=@('Error','Warning'); ids=@()",
	} {
		if !strings.Contains(script, wantLiteral) {
			t.Errorf("legacy filter missing from script: %q", wantLiteral)
		}
	}
}

func TestExplicitEmptyEventLogFiltersDisableQueries(t *testing.T) {
	filters := []config.EventLogFilter{}
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = &filters
	if got := effectiveEventLogFilters(cfg); len(got) != 0 {
		t.Fatalf("explicit empty filters produced queries: %#v", got)
	}
	script, err := eventLogScript(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if script != "" {
		t.Fatalf("explicit empty filters generated a PowerShell command:\n%s", script)
	}
}

func TestEventLogPolicyRejectsInvalidOrOversizedFilters(t *testing.T) {
	tests := []struct {
		name    string
		filters []config.EventLogFilter
		wantErr string
	}{
		{
			name:    "empty levels",
			filters: []config.EventLogFilter{{Channel: "System"}},
			wantErr: "has no levels",
		},
		{
			name:    "non-positive event id",
			filters: []config.EventLogFilter{{Channel: "System", Levels: []string{"Error"}, IDs: []int{0}}},
			wantErr: "must be between 1",
		},
		{
			name:    "unsupported level",
			filters: []config.EventLogFilter{{Channel: "System", Levels: []string{"AuditFailure"}}},
			wantErr: "is unsupported",
		},
		{
			name:    "channel whitespace",
			filters: []config.EventLogFilter{{Channel: " System", Levels: []string{"Error"}}},
			wantErr: "leading or trailing whitespace",
		},
		{
			name: "too many filters",
			filters: func() []config.EventLogFilter {
				filters := make([]config.EventLogFilter, maxEventLogFilters+1)
				for i := range filters {
					filters[i] = config.EventLogFilter{Channel: "System", Levels: []string{"Error"}}
				}
				return filters
			}(),
			wantErr: "maximum is 32",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := config.Default().Collectors.EventLog
			cfg.Filters = &tt.filters
			if _, err := eventLogScript(cfg); err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("error = %v, want containing %q", err, tt.wantErr)
			}
		})
	}
}
