package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNormalizeControllerURLAddsHTTP(t *testing.T) {
	got, err := NormalizeControllerURL("192.168.8.152")
	if err != nil {
		t.Fatal(err)
	}
	if got != "http://192.168.8.152" {
		t.Fatalf("got %q", got)
	}
}

func TestSetControllerURLPreservesRelativeDataDir(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.yaml")
	input := []byte("version: 1\ncontroller_url: http://old.example\nverify_tls: true\ndata_dir: ../data\nheartbeat_interval_seconds: 30\nupload_interval_seconds: 30\nconfig_interval_seconds: 120\ncollect_interval_seconds: 60\ncollector_timeout_seconds: 20\nspool:\n  max_bytes: 1024\n  max_age_hours: 24\nsecurity:\n  require_signed_config: false\nlimits:\n  max_process_count: 10\n  max_payload_bytes: 1024\n")
	if err := os.WriteFile(path, input, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := SetControllerURL(path, "192.168.8.152")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ControllerURL != "http://192.168.8.152" {
		t.Fatalf("got controller URL %q", cfg.ControllerURL)
	}
	out, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(out)
	if !strings.Contains(text, "controller_url: http://192.168.8.152") {
		t.Fatalf("saved config missing controller_url:\n%s", text)
	}
	if !strings.Contains(text, "data_dir: ../data") {
		t.Fatalf("saved config did not preserve relative data_dir:\n%s", text)
	}
}
