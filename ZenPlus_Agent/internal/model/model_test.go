package model

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestAgentVersionAndCapabilitiesContract(t *testing.T) {
	if AgentVersion != "1.3.1" {
		t.Fatalf("unexpected agent version %q", AgentVersion)
	}
	want := []string{"network_capture_v1", "capture_stop_v1", "interface_traffic_v1"}
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
