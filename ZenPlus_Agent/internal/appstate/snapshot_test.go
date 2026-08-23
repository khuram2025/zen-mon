package appstate

import (
	"testing"
	"time"

	"zenplus-agent/internal/model"
)

func TestHealthTextDoesNotTreatReachabilityAsAuthorization(t *testing.T) {
	now := time.Now().UTC()
	tests := []struct {
		name   string
		status model.Status
		want   string
		level  string
	}{
		{
			name:   "pending approval",
			status: model.Status{AuthState: "pending"},
			want:   "Pending authorization",
			level:  "warn",
		},
		{
			name:   "reachable but no heartbeat",
			status: model.Status{AuthState: "ok"},
			want:   "Connecting",
			level:  "warn",
		},
		{
			name:   "authorized and reporting",
			status: model.Status{AuthState: "ok", LastHeartbeat: &now},
			want:   "Healthy",
			level:  "ok",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			snapshot := Snapshot{
				Now:        now,
				Status:     &tt.status,
				Service:    ServiceSnapshot{Installed: true, Running: true},
				Controller: ControllerSnapshot{Reachable: true},
				Config: ConfigSnapshot{
					HeartbeatIntervalSeconds: 30,
					UploadIntervalSeconds:    30,
					CollectIntervalSeconds:   30,
				},
			}
			got, level := HealthText(snapshot)
			if got != tt.want || level != tt.level {
				t.Fatalf("HealthText() = (%q, %q), want (%q, %q)", got, level, tt.want, tt.level)
			}
		})
	}
}
