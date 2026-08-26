package identity

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeTestIdentity(t *testing.T, path string, id Identity) {
	t.Helper()
	body, err := json.Marshal(id)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestLoadOrCreatePreservesPersistedApplianceIDs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	stored := Identity{
		AgentUID:  "win-persisted",
		AgentID:   "agt_appliance",
		ServerID:  "srv_appliance",
		Hostname:  "host-a",
		Platform:  "windows",
		CreatedAt: time.Now().UTC(),
	}
	writeTestIdentity(t, path, stored)

	got, cloned, err := LoadOrCreate(path, "agt_stale_config", "srv_stale_config")
	if err != nil {
		t.Fatal(err)
	}
	if cloned {
		t.Fatal("identity without a stored machine GUID must not be classified as a clone")
	}
	if got.AgentID != stored.AgentID || got.ServerID != stored.ServerID {
		t.Fatalf("persisted IDs were overwritten: agent=%q server=%q", got.AgentID, got.ServerID)
	}
}

func TestLoadOrCreateUsesConfiguredIDsOnlyWhenPersistedIDsAreMissing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	writeTestIdentity(t, path, Identity{
		AgentUID:  "win-legacy",
		Hostname:  "host-a",
		Platform:  "windows",
		CreatedAt: time.Now().UTC(),
	})

	got, cloned, err := LoadOrCreate(path, "agt_bootstrap", "srv_bootstrap")
	if err != nil {
		t.Fatal(err)
	}
	if cloned {
		t.Fatal("identity without a stored machine GUID must not be classified as a clone")
	}
	if got.AgentID != "agt_bootstrap" || got.ServerID != "srv_bootstrap" {
		t.Fatalf("missing IDs were not filled from first-install hints: agent=%q server=%q", got.AgentID, got.ServerID)
	}
}
