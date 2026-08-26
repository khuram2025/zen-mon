package runtime

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
)

func TestNewDashboardSnapshotExcludesSensitiveIdentityAndRedactsErrors(t *testing.T) {
	cfg := config.Default()
	cfg.DataDir = `C:\ProgramData\ZenPlus\Agent`
	cfg.ControllerURL = "https://dashboard-user:dashboard-password@controller.example/zenplus?api_key=config-secret#private"
	id := identity.Identity{
		AgentID:     "agent-1",
		ServerID:    "server-1",
		Hostname:    "srv01",
		MachineGUID: "private-machine-guid",
	}
	status := model.Status{
		AgentID:            id.AgentID,
		ServerID:           id.ServerID,
		ControllerURL:      cfg.ControllerURL,
		LastHeartbeatError: "Bearer secret-token",
		LastUploadError:    "GET " + cfg.ControllerURL + " failed",
		CollectorErrors:    map[string]string{"processes": `{"api_key":"json-secret","access_token":"access-secret"}`},
		LocalAPM:           &model.AgentAPMHeartbeat{Bundles: map[string]string{"node": "1.0.0"}},
	}

	snapshot := NewDashboardSnapshot(cfg, id, status)
	encoded := mustMarshalDashboardSnapshot(t, snapshot)
	for _, private := range []string{id.MachineGUID, "secret-token", "dashboard-user", "dashboard-password", "config-secret", "json-secret", "access-secret"} {
		if strings.Contains(encoded, private) {
			t.Fatalf("published dashboard snapshot contains private material %q: %s", private, encoded)
		}
	}
	if snapshot.Config.ControllerURL != "https://controller.example/zenplus" || snapshot.Status.ControllerURL != snapshot.Config.ControllerURL {
		t.Fatalf("published controller URLs were not sanitized consistently: config=%q status=%q", snapshot.Config.ControllerURL, snapshot.Status.ControllerURL)
	}
	if !strings.Contains(encoded, "REDACTED") {
		t.Fatalf("published dashboard snapshot did not retain a useful redaction marker: %s", encoded)
	}
	if snapshot.Status.CollectorErrors["processes"] == status.CollectorErrors["processes"] {
		t.Fatal("collector error was not redacted")
	}
	if status.CollectorErrors["processes"] != `{"api_key":"json-secret","access_token":"access-secret"}` {
		t.Fatal("snapshot construction mutated the live status map")
	}
	status.LocalAPM.Bundles["node"] = "changed"
	if snapshot.Status.LocalAPM.Bundles["node"] != "1.0.0" {
		t.Fatal("snapshot retained a mutable reference to the live APM bundle map")
	}
	if strings.Contains(encoded, id.MachineGUID) {
		t.Fatalf("published dashboard snapshot contains private material: %s", encoded)
	}
}

func TestMachineDashboardPathsAreLimitedToCanonicalMachineLocations(t *testing.T) {
	oldProgramData := os.Getenv("ProgramData")
	t.Cleanup(func() { _ = os.Setenv("ProgramData", oldProgramData) })
	programData := filepath.Join(t.TempDir(), "ProgramData")
	if err := os.Setenv("ProgramData", programData); err != nil {
		t.Fatal(err)
	}
	dataDir := filepath.Join(programData, "ZenPlus", "Agent")
	configPath := filepath.Join(dataDir, "config", "agent.yaml")
	want := filepath.Join(programData, "ZenPlus", "AgentDashboard", "snapshot.json")
	if got, ok := MachineDashboardPathForDataDir(dataDir); !ok || got != want {
		t.Fatalf("MachineDashboardPathForDataDir() = %q, %v; want %q, true", got, ok, want)
	}
	if got, ok := MachineDashboardPathForConfig(configPath); !ok || got != want {
		t.Fatalf("MachineDashboardPathForConfig() = %q, %v; want %q, true", got, ok, want)
	}
	if _, ok := MachineDashboardPathForDataDir(filepath.Join(programData, "Other")); ok {
		t.Fatal("non-canonical data directory was treated as the public machine snapshot source")
	}
	if _, ok := MachineDashboardPathForConfig(filepath.Join(dataDir, "config", "other.yaml")); ok {
		t.Fatal("non-canonical config path was treated as the public machine snapshot consumer")
	}
}

func mustMarshalDashboardSnapshot(t *testing.T, snapshot DashboardSnapshot) string {
	t.Helper()
	contents, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	return string(contents)
}

func TestReplaceDashboardFileReplacesExistingSnapshot(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "snapshot.json")
	source := filepath.Join(directory, "snapshot.next.json")
	if err := os.WriteFile(target, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(source, []byte("new"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := replaceDashboardFile(source, target); err != nil {
		t.Fatalf("replaceDashboardFile: %v", err)
	}
	contents, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(contents) != "new" {
		t.Fatalf("replacement contents = %q, want new", contents)
	}
	if _, err := os.Stat(source); !os.IsNotExist(err) {
		t.Fatalf("replacement source still exists: %v", err)
	}
}
