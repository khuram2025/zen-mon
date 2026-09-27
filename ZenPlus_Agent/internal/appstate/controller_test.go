package appstate

import (
	"testing"
	"time"

	"zenplus-agent/internal/model"
)

func TestControllerFromStatusUsesTheAgentsHeartbeat(t *testing.T) {
	now := time.Date(2026, 9, 27, 10, 0, 0, 0, time.UTC)
	recent := now.Add(-10 * time.Second)
	old := now.Add(-10 * time.Minute)
	for _, tc := range []struct {
		name      string
		status    *model.Status
		known     bool
		reachable bool
	}{
		{"no status yet", nil, false, false},
		{"no heartbeat attempted", &model.Status{}, false, false},
		{"recent heartbeat", &model.Status{LastHeartbeat: &recent}, true, true},
		{"heartbeat failing", &model.Status{LastHeartbeat: &old, LastHeartbeatError: "dial tcp: timeout"}, true, false},
		{"heartbeat stale", &model.Status{LastHeartbeat: &old}, true, false},
		{"first heartbeat failed", &model.Status{LastHeartbeatError: "x509: unknown authority"}, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := ControllerFromStatus("https://appliance", tc.status, 30, now)
			if got.Known != tc.known || got.Reachable != tc.reachable {
				t.Fatalf("known=%v reachable=%v, want %v %v (%+v)", got.Known, got.Reachable, tc.known, tc.reachable, got)
			}
			if !got.Reachable && got.Known && got.Message == "" {
				t.Fatal("unreachable controller has no explanation")
			}
		})
	}
}
