package model

import (
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

func TestAgentVersionAndCapabilitiesContract(t *testing.T) {
	if AgentVersion != "1.12.0" {
		t.Fatalf("unexpected agent version %q", AgentVersion)
	}
	want := []string{
		"network_capture_v1", "capture_stop_v1", "interface_traffic_v1", "apm_status_v1",
		"apm_gateway_v1", "apm_runtime_discovery_v1",
		"apm_iis_instrumentation_v1", "apm_windows_service_instrumentation_v1",
		"apm_runtime_health_v1",
		"clone_identity_assignment_v1",
	}
	if !reflect.DeepEqual(AgentCapabilities, want) {
		t.Fatalf("capabilities changed: got %v want %v", AgentCapabilities, want)
	}
	b, err := json.Marshal(Heartbeat{Version: AgentVersion, Capabilities: AgentCapabilities})
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	capabilities, ok := raw["capabilities"].([]any)
	if !ok || len(capabilities) != len(want) {
		t.Fatalf("heartbeat capabilities missing from JSON: %s", b)
	}
}

func TestHeartbeatCarriesLocalAPMSwitchAndProbe(t *testing.T) {
	b, err := json.Marshal(Heartbeat{
		Version: AgentVersion,
		APM: &AgentAPMHeartbeat{
			Enabled:   false,
			Gateway:   APMGatewayStatus{Listening: false, GRPCPort: 4317, HTTPPort: 4318},
			CheckedAt: time.Date(2026, 8, 23, 10, 0, 0, 0, time.UTC),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	apm, ok := raw["apm"].(map[string]any)
	if !ok || apm["enabled"] != false {
		t.Fatalf("local APM switch missing from heartbeat: %s", b)
	}
}

func TestHeartbeatResponseCarriesReadOnlyApplianceAPMStatus(t *testing.T) {
	payload := []byte(`{"ok":true,"apm":{"available":true,"state":"active","managed_by":"appliance","ingest_path":"/v1/traces","queue_depth":2,"queue_capacity":512,"accepted_spans_total":42,"checked_at":"2026-08-23T10:00:00Z"}}`)
	var response HeartbeatResponse
	if err := json.Unmarshal(payload, &response); err != nil {
		t.Fatal(err)
	}
	if response.APM == nil || !response.APM.Available || response.APM.ManagedBy != "appliance" {
		t.Fatalf("unexpected APM status: %#v", response.APM)
	}
	if response.APM.AcceptedSpansTotal != 42 || response.APM.QueueDepth != 2 {
		t.Fatalf("APM insights were not decoded: %#v", response.APM)
	}
}

func TestBatchCarriesCollectorHealth(t *testing.T) {
	b, err := json.Marshal(Batch{Health: Health{
		Status:          "degraded",
		CollectorErrors: map[string]string{"memory": "counter unavailable"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	health, ok := raw["health"].(map[string]any)
	if !ok || health["status"] != "degraded" {
		t.Fatalf("batch health missing from JSON: %s", b)
	}
}

func TestNetworkFlowClassificationJSONContract(t *testing.T) {
	b, err := json.Marshal(NetworkFlow{
		Protocol: "udp", Kind: "endpoint", Direction: "local",
		LocalIP: "::", LocalPort: 5353,
	})
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	if raw["kind"] != "endpoint" || raw["direction"] != "local" {
		t.Fatalf("classification fields missing from JSON: %s", b)
	}
	if raw["remote_ip"] != "" || raw["remote_port"] != float64(0) {
		t.Fatalf("peerless endpoint must not invent a remote peer: %s", b)
	}
}
