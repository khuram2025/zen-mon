package config

import "testing"

// The published MSI is built with embeddedControllerURL/embeddedEnrollmentToken
// set via -ldflags so a host installs and enrols with zero operator input.
// These cover the resolution those linker values feed into.

func TestDefaultUsesEmbeddedControllerAndToken(t *testing.T) {
	oldURL, oldToken := embeddedControllerURL, embeddedEnrollmentToken
	defer func() { embeddedControllerURL, embeddedEnrollmentToken = oldURL, oldToken }()

	embeddedControllerURL = "192.168.8.221"
	embeddedEnrollmentToken = "zpa_enr_example"
	cfg := Default()
	if cfg.ControllerURL != "http://192.168.8.221" {
		t.Fatalf("embedded controller URL not normalized into defaults: %q", cfg.ControllerURL)
	}
	if cfg.EnrollmentToken != "zpa_enr_example" {
		t.Fatalf("embedded enrollment token not applied: %q", cfg.EnrollmentToken)
	}
}

func TestDefaultFallsBackWhenNothingEmbedded(t *testing.T) {
	oldURL, oldToken := embeddedControllerURL, embeddedEnrollmentToken
	defer func() { embeddedControllerURL, embeddedEnrollmentToken = oldURL, oldToken }()

	embeddedControllerURL, embeddedEnrollmentToken = "", ""
	cfg := Default()
	if cfg.ControllerURL != DefaultControllerURL {
		t.Fatalf("expected fallback %q, got %q", DefaultControllerURL, cfg.ControllerURL)
	}
	if cfg.EnrollmentToken != "" {
		t.Fatalf("expected no token, got %q", cfg.EnrollmentToken)
	}
}

// A package downloaded outside the controller's download flow still carries
// the un-substituted MSI placeholder; it must be treated as "no token" rather
// than sent to the controller and rejected.
func TestPlaceholderTokenIsTreatedAsAbsent(t *testing.T) {
	if got := NormalizeEnrollmentToken(PlaceholderEnrollmentToken); got != "" {
		t.Fatalf("placeholder token was not discarded: %q", got)
	}
	if got := NormalizeEnrollmentToken("  zpa_enr_realtoken  "); got != "zpa_enr_realtoken" {
		t.Fatalf("real token was not preserved: %q", got)
	}

	old := embeddedEnrollmentToken
	defer func() { embeddedEnrollmentToken = old }()
	embeddedEnrollmentToken = PlaceholderEnrollmentToken
	if Default().EnrollmentToken != "" {
		t.Fatal("placeholder leaked into defaults")
	}
	if HasEmbeddedEnrollmentToken() {
		t.Fatal("placeholder must not count as an embedded token")
	}
}

// The placeholder the controller rewrites must stay exactly as long as a real
// token, or the in-place patch would corrupt the MSI.
func TestPlaceholderMatchesRealTokenWidth(t *testing.T) {
	// "zpa_enr_" + base64url(24 bytes) = 8 + 32.
	const realTokenLen = 8 + 32
	if len(PlaceholderEnrollmentToken) != realTokenLen {
		t.Fatalf("placeholder is %d chars, real tokens are %d; the MSI patch would corrupt the package",
			len(PlaceholderEnrollmentToken), realTokenLen)
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
