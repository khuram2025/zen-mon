package config

import (
	"os"
	"testing"
)

// The published MSI may carry only a controller URL. Authorization is issued
// later by the appliance and is not an endpoint configuration value.

func TestDefaultUsesEmbeddedControllerWithoutToken(t *testing.T) {
	oldURL := embeddedControllerURL
	defer func() { embeddedControllerURL = oldURL }()

	embeddedControllerURL = "192.168.8.221"
	cfg := Default()
	if cfg.ControllerURL != "https://192.168.8.221" {
		t.Fatalf("embedded controller URL not normalized into defaults: %q", cfg.ControllerURL)
	}
}

func TestDefaultFallsBackWhenNothingEmbedded(t *testing.T) {
	oldURL := embeddedControllerURL
	defer func() { embeddedControllerURL = oldURL }()

	embeddedControllerURL = ""
	cfg := Default()
	if cfg.ControllerURL != DefaultControllerURL {
		t.Fatalf("expected fallback %q, got %q", DefaultControllerURL, cfg.ControllerURL)
	}
}

// An operator changing controller_url in agent.yaml must win over the value
// baked into the binary.
func TestOnDiskConfigOverridesEmbedded(t *testing.T) {
	oldURL := embeddedControllerURL
	defer func() { embeddedControllerURL = oldURL }()
	embeddedControllerURL = "https://192.168.8.221"

	path := t.TempDir() + "/agent.yaml"
	if err := Save(path, func() Config { c := Default(); c.ControllerURL = "https://controller.example"; return c }()); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ControllerURL != "https://controller.example" {
		t.Fatalf("on-disk controller_url did not override the embedded default: %q", cfg.ControllerURL)
	}
}

func TestAPMEnableSettingIsPreserved(t *testing.T) {
	path := t.TempDir() + "/agent.yaml"
	legacy := `version: 1
controller_url: https://192.168.8.221
heartbeat_interval_seconds: 30
upload_interval_seconds: 60
config_interval_seconds: 60
collect_interval_seconds: 60
collector_timeout_seconds: 20
data_dir: data
spool:
  max_bytes: 1024
  max_age_hours: 24
apm:
  enabled: false
`
	if err := os.WriteFile(path, []byte(legacy), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.APM.Enabled {
		t.Fatal("expected apm.enabled=false to be preserved")
	}
}
