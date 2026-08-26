package config

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestNormalizeControllerURLAddsHTTPS(t *testing.T) {
	got, err := NormalizeControllerURL("192.168.8.152")
	if err != nil {
		t.Fatal(err)
	}
	if got != "https://192.168.8.152" {
		t.Fatalf("got %q", got)
	}
}

func TestDefaultUsesSystemControllerTrust(t *testing.T) {
	cfg := Default()
	if !cfg.VerifyTLS || cfg.Security.ControllerCAFile != "" || cfg.Security.AllowInsecureTransport {
		t.Fatalf("default TLS policy changed: verify=%v ca=%q insecure=%v", cfg.VerifyTLS, cfg.Security.ControllerCAFile, cfg.Security.AllowInsecureTransport)
	}
}

func TestSetControllerURLPreservesRelativeDataDir(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	input := []byte("version: 1\ncontroller_url: https://old.example\nverify_tls: true\ndata_dir: ../data\nheartbeat_interval_seconds: 30\nupload_interval_seconds: 30\nconfig_interval_seconds: 120\ncollect_interval_seconds: 60\ncollector_timeout_seconds: 20\nspool:\n  max_bytes: 1024\n  max_age_hours: 24\nsecurity:\n  require_signed_config: false\nlimits:\n  max_process_count: 10\n  max_payload_bytes: 1024\n")
	if err := os.WriteFile(path, input, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := SetControllerURL(path, "192.168.8.152")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ControllerURL != "https://192.168.8.152" {
		t.Fatalf("got controller URL %q", cfg.ControllerURL)
	}
	out, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(out)
	if !strings.Contains(text, "controller_url: https://192.168.8.152") {
		t.Fatalf("saved config missing controller_url:\n%s", text)
	}
	if !strings.Contains(text, "data_dir: ../data") {
		t.Fatalf("saved config did not preserve relative data_dir:\n%s", text)
	}
}

func TestLoadMigratesLegacyHTTPControllerToHTTPS(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	body, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	body = []byte(strings.Replace(string(body), cfg.ControllerURL, "http://192.168.8.221", 1))
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}

	got, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.ControllerURL != "https://192.168.8.221" || !got.VerifyTLS {
		t.Fatalf("legacy controller was not migrated securely: url=%q verify_tls=%v", got.ControllerURL, got.VerifyTLS)
	}
}

func TestLoadPreservesExplicitlyAllowedHTTPController(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	cfg.ControllerURL = "http://192.0.2.20"
	cfg.Security.AllowInsecureTransport = true
	if err := Save(path, cfg); err != nil {
		t.Fatal(err)
	}

	got, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.ControllerURL != cfg.ControllerURL {
		t.Fatalf("explicit insecure transport was overwritten: %q", got.ControllerURL)
	}
}

func TestLoadResolvesRelativeControllerCAFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config", "agent.yaml")
	cfg := Default()
	cfg.Security.ControllerCAFile = filepath.Join("..", "trust", "controller-ca.pem")
	if err := Save(path, cfg); err != nil {
		t.Fatal(err)
	}

	loaded, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(dir, "trust", "controller-ca.pem")
	if loaded.Security.ControllerCAFile != want {
		t.Fatalf("controller CA path = %q, want %q", loaded.Security.ControllerCAFile, want)
	}
	edited, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if edited.Security.ControllerCAFile != cfg.Security.ControllerCAFile {
		t.Fatalf("editable config lost its relative CA path: %q", edited.Security.ControllerCAFile)
	}
}

func TestValidateRejectsRemoteHTTPByDefault(t *testing.T) {
	cfg := Default()
	cfg.ControllerURL = "http://192.168.8.221"
	if err := cfg.Validate(); err == nil || !strings.Contains(err.Error(), "must use https") {
		t.Fatalf("expected HTTPS enforcement error, got %v", err)
	}
}

func TestValidateAllowsLoopbackHTTP(t *testing.T) {
	cfg := Default()
	cfg.ControllerURL = "http://127.0.0.1:8080"
	if err := cfg.Validate(); err != nil {
		t.Fatalf("loopback development endpoint should be allowed: %v", err)
	}
}

func TestValidateAllowsExplicitInsecureBreakGlass(t *testing.T) {
	cfg := Default()
	cfg.ControllerURL = "http://192.168.8.221"
	cfg.Security.AllowInsecureTransport = true
	if err := cfg.Validate(); err != nil {
		t.Fatalf("explicit break-glass setting should be allowed: %v", err)
	}
}

func TestValidateRejectsDisabledTLSVerificationByDefault(t *testing.T) {
	cfg := Default()
	cfg.VerifyTLS = false
	if err := cfg.Validate(); err == nil || !strings.Contains(err.Error(), "verify_tls cannot be disabled") {
		t.Fatalf("expected TLS verification error, got %v", err)
	}
}

func TestValidateRejectsCABundleWithDisabledVerification(t *testing.T) {
	cfg := Default()
	cfg.VerifyTLS = false
	cfg.Security.AllowInsecureTransport = true
	cfg.Security.ControllerCAFile = `C:\ProgramData\ZenPlus\Agent\trust\controller-ca.pem`
	if err := cfg.Validate(); err == nil || !strings.Contains(err.Error(), "controller_ca_file requires verify_tls") {
		t.Fatalf("expected contradictory CA/verification error, got %v", err)
	}
}

func TestValidateNormalizesLegacyIPv6APMBindToIPv4Loopback(t *testing.T) {
	cfg := Default()
	cfg.APM.BindAddress = "::1"
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	if cfg.APM.BindAddress != "127.0.0.1" {
		t.Fatalf("legacy APM bind normalized to %q", cfg.APM.BindAddress)
	}
}

func TestLoadForEditNormalizesLegacyIPv6APMBind(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	cfg.APM.BindAddress = "::1"
	b, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, b, 0o600); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.APM.BindAddress != "127.0.0.1" {
		t.Fatalf("loaded legacy APM bind normalized to %q", loaded.APM.BindAddress)
	}
}

func TestApplyProfileRestoresInfrastructureAfterAPMOnly(t *testing.T) {
	cfg := Default()
	if err := ApplyProfile(&cfg, "apm"); err != nil {
		t.Fatal(err)
	}
	if !InfrastructureCollectorsDisabled(cfg) {
		t.Fatal("APM-only profile did not disable infrastructure collectors")
	}
	if err := ApplyProfile(&cfg, "combined"); err != nil {
		t.Fatal(err)
	}
	if InfrastructureCollectorsDisabled(cfg) || !cfg.Collectors.CPU.Enabled || !cfg.Collectors.Processes.Enabled {
		t.Fatal("combined profile did not restore infrastructure collectors")
	}
	if !cfg.APM.Enabled || cfg.APM.Profile != "combined" {
		t.Fatalf("unexpected APM state: enabled=%v profile=%q", cfg.APM.Enabled, cfg.APM.Profile)
	}
}

func TestApplyProfileRepairsLegacyCombinedConfig(t *testing.T) {
	cfg := Default()
	cfg.APM.Profile = "combined"
	setInfrastructureCollectors(&cfg, false)
	if err := ApplyProfile(&cfg, "combined"); err != nil {
		t.Fatal(err)
	}
	if InfrastructureCollectorsDisabled(cfg) {
		t.Fatal("legacy all-disabled combined config was not repaired")
	}
}

func TestApplyProfilePreservesIndividualCollectorChoice(t *testing.T) {
	cfg := Default()
	cfg.Collectors.CPU.Enabled = false
	if err := ApplyProfile(&cfg, "combined"); err != nil {
		t.Fatal(err)
	}
	if cfg.Collectors.CPU.Enabled {
		t.Fatal("same-profile application overwrote an individual collector choice")
	}
}

func TestEventLogFiltersRoundTripExactly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	want := []EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}, IDs: []int{41, 6008}},
		{Channel: "Microsoft-Windows-PowerShell/Operational", Levels: []string{"Information"}, IDs: []int{4103, 4104}},
	}
	cfg.Collectors.EventLog.Filters = &want
	cfg.Collectors.EventLog.Channels = nil
	cfg.Collectors.EventLog.Levels = nil
	if err := Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Collectors.EventLog.Filters == nil || !reflect.DeepEqual(*loaded.Collectors.EventLog.Filters, want) {
		t.Fatalf("filters after round trip = %#v, want %#v", loaded.Collectors.EventLog.Filters, want)
	}
}

func TestExplicitEmptyEventLogFiltersRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	empty := []EventLogFilter{}
	cfg.Collectors.EventLog.Filters = &empty
	cfg.Collectors.EventLog.Channels = nil
	cfg.Collectors.EventLog.Levels = nil
	if err := Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Collectors.EventLog.Filters == nil || len(*loaded.Collectors.EventLog.Filters) != 0 {
		t.Fatalf("explicit empty filters were not preserved: %#v", loaded.Collectors.EventLog.Filters)
	}
}

func TestLegacyEventLogChannelsAndLevelsRemainSupported(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	cfg := Default()
	cfg.Collectors.EventLog.Filters = nil
	cfg.Collectors.EventLog.Channels = []string{"System", "Application"}
	cfg.Collectors.EventLog.Levels = []string{"Error", "Warning"}
	b, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "filters:") {
		t.Fatalf("legacy config unexpectedly contains filters:\n%s", b)
	}
	if err := os.WriteFile(path, b, 0o600); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Collectors.EventLog.Filters != nil {
		t.Fatalf("legacy config acquired authoritative filters: %#v", loaded.Collectors.EventLog.Filters)
	}
	if !reflect.DeepEqual(loaded.Collectors.EventLog.Channels, cfg.Collectors.EventLog.Channels) ||
		!reflect.DeepEqual(loaded.Collectors.EventLog.Levels, cfg.Collectors.EventLog.Levels) {
		t.Fatalf("legacy event-log policy changed: %#v", loaded.Collectors.EventLog)
	}
}
