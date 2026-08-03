package config

import "testing"

// The published MSI may carry a controller URL, but enrollment tokens are
// supplied only at deployment time and are never linked into release binaries.

func TestDefaultUsesEmbeddedControllerWithoutToken(t *testing.T) {
	oldURL := embeddedControllerURL
	defer func() { embeddedControllerURL = oldURL }()

	embeddedControllerURL = "192.168.8.221"
	cfg := Default()
	if cfg.ControllerURL != "http://192.168.8.221" {
		t.Fatalf("embedded controller URL not normalized into defaults: %q", cfg.ControllerURL)
	}
	if cfg.EnrollmentToken != "" {
		t.Fatalf("generic build must not contain an enrollment token: %q", cfg.EnrollmentToken)
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
	if cfg.EnrollmentToken != "" {
		t.Fatalf("expected no token, got %q", cfg.EnrollmentToken)
	}
}

func TestEnrollmentTokenIsOnlyTrimmed(t *testing.T) {
	if got := NormalizeEnrollmentToken("  zpa_enr_realtoken  "); got != "zpa_enr_realtoken" {
		t.Fatalf("real token was not preserved: %q", got)
	}
	if got := NormalizeEnrollmentToken("  "); got != "" {
		t.Fatalf("blank token was not discarded: %q", got)
	}
}

// An operator changing controller_url in agent.yaml must win over the value
// baked into the binary.
func TestOnDiskConfigOverridesEmbedded(t *testing.T) {
	oldURL := embeddedControllerURL
	defer func() { embeddedControllerURL = oldURL }()
	embeddedControllerURL = "http://192.168.8.221"

	path := t.TempDir() + "/agent.yaml"
	if err := Save(path, func() Config { c := Default(); c.ControllerURL = "http://controller.example"; return c }()); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ControllerURL != "http://controller.example" {
		t.Fatalf("on-disk controller_url did not override the embedded default: %q", cfg.ControllerURL)
	}
}
