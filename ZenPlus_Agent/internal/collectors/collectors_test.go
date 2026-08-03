package collectors

import (
	"context"
	"runtime"
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
