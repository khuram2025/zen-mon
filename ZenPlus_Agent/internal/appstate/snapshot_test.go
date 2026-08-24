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

func TestHealthTextReportsServerCollectorFailures(t *testing.T) {
	now := time.Now().UTC()
	base := Snapshot{
		Now:        now,
		Status:     &model.Status{AuthState: "ok", LastHeartbeat: &now},
		Service:    ServiceSnapshot{Installed: true, Running: true},
		Controller: ControllerSnapshot{Reachable: true},
		Config: ConfigSnapshot{
			MonitoringProfile:        "combined",
			HeartbeatIntervalSeconds: 30,
			UploadIntervalSeconds:    30,
			CollectIntervalSeconds:   30,
			CollectorEnabled:         map[string]bool{"cpu": true},
		},
	}

	base.Status.CollectorErrors = map[string]string{"memory": "counter unavailable"}
	if got, level := HealthText(base); got != "Collector degraded" || level != "bad" {
		t.Fatalf("collector failure HealthText() = (%q, %q)", got, level)
	}

	base.Status.CollectorErrors = nil
	base.Config.CollectorEnabled = map[string]bool{"inventory": true}
	if got, level := HealthText(base); got != "Server collectors disabled" || level != "bad" {
		t.Fatalf("disabled collectors HealthText() = (%q, %q)", got, level)
	}

	base.Config.MonitoringProfile = "apm"
	if got, level := HealthText(base); got != "Healthy" || level != "ok" {
		t.Fatalf("APM-only HealthText() = (%q, %q)", got, level)
	}
}

func TestHealthTextUsesPublishedSnapshotTimeForAgentLiveness(t *testing.T) {
	now := time.Now().UTC()
	base := Snapshot{
		Now:         now,
		PublishedAt: now.Add(-10 * time.Minute),
		Status:      &model.Status{AuthState: "ok"},
		Service:     ServiceSnapshot{Installed: true, Running: true},
		Controller:  ControllerSnapshot{Reachable: true},
		Config: ConfigSnapshot{
			HeartbeatIntervalSeconds: 30,
			UploadIntervalSeconds:    30,
			CollectIntervalSeconds:   30,
		},
	}

	if got, level := HealthText(base); got != "Agent stale" || level != "warn" {
		t.Fatalf("stale published snapshot HealthText() = (%q, %q)", got, level)
	}

	base.PublishedAt = now
	if got, level := HealthText(base); got != "Connecting" || level != "warn" {
		t.Fatalf("fresh pre-heartbeat snapshot HealthText() = (%q, %q)", got, level)
	}

	base.PublishedAt = time.Time{}
	base.Status.AuthState = "pending"
	if got, level := HealthText(base); got != "Pending authorization" || level != "warn" {
		t.Fatalf("non-published pending status HealthText() = (%q, %q)", got, level)
	}
}
