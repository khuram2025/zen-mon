package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSetProfileInFileKeepsOtherSettings(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	cfg.ControllerURL = "https://appliance.example"
	if err := ApplyProfile(&cfg, "combined"); err != nil {
		t.Fatal(err)
	}
	if err := Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	if err := SetProfileInFile(path, "infrastructure"); err != nil {
		t.Fatal(err)
	}
	got, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.APM.Profile != "infrastructure" || got.APM.Enabled {
		t.Fatalf("profile not applied: %+v", got.APM)
	}
	if got.ControllerURL != "https://appliance.example" {
		t.Fatalf("controller changed to %q", got.ControllerURL)
	}
	entries, _ := os.ReadDir(filepath.Dir(path))
	if len(entries) != 1 {
		t.Fatalf("temporary files left behind: %d entries", len(entries))
	}
	if err := SetProfileInFile(path, "everything"); err == nil {
		t.Fatal("invalid profile accepted")
	}
}
