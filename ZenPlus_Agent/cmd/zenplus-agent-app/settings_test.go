package main

import (
	"bytes"
	"os"
	"path/filepath"

	"reflect"
	"runtime"
	"testing"
	"zenplus-agent/internal/config"
)

func TestSaveSettingsWithoutInstallerPreservesOtherFieldsAndState(t *testing.T) {
	previousInstalled := apmComponentsInstalled
	defer func() { apmComponentsInstalled = previousInstalled }()
	apmComponentsInstalled = func() bool { return true }
	root := t.TempDir()
	path := filepath.Join(root, "config", "agent.yaml")
	cfg := config.Default()
	cfg.DataDir = root
	cfg.ControllerURL = "https://old.example"
	cfg.ProxyURL = "http://proxy.example:8080"
	cfg.AgentID, cfg.ServerID = "existing-agent", "existing-server"
	if err := config.Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(root, "credential.dpapi")
	if err := os.WriteFile(state, []byte("opaque-test-state"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := saveSettingsFile(path, "10.11.40.32", "apm"); err != nil {
		t.Fatal(err)
	}
	got, err := config.LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.ControllerURL != "https://10.11.40.32" || got.APM.Profile != "apm" {
		t.Fatalf("settings not applied: %+v", got)
	}
	if got.DataDir != cfg.DataDir || got.ProxyURL != cfg.ProxyURL || got.AgentID != cfg.AgentID || got.ServerID != cfg.ServerID {
		t.Fatal("unrelated settings changed")
	}
	data, err := os.ReadFile(state)
	if err != nil || string(data) != "opaque-test-state" {
		t.Fatal("saved state changed")
	}
	leftovers, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".settings-*.yaml"))
	if len(leftovers) != 0 {
		t.Fatalf("temporary files remain: %v", leftovers)
	}
	before, _ := os.ReadFile(path)
	for _, values := range [][2]string{{"", "combined"}, {"http://10.11.40.31", "combined"}, {"10.11.40.31", "invalid"}} {
		if err := saveSettingsFile(path, values[0], values[1]); err == nil {
			t.Fatal("invalid change accepted")
		}
		after, _ := os.ReadFile(path)
		if !bytes.Equal(before, after) {
			t.Fatal("failed save modified configuration")
		}
	}
}

func TestSettingsHelperUsesOnlySettingsArguments(t *testing.T) {
	want := []string{"--apply-settings", "--config", `C:\ProgramData\ZenPlus\Agent\config\agent.yaml`, "--controller", "https://10.11.40.32", "--profile", "combined"}
	got := settingsHelperArgs(want[2], want[4], want[6])
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected helper arguments: %v", got)
	}
}

// Run explicitly for visual acceptance; Cancel must leave disk untouched.
func TestSettingsDialogVisualIntegration(t *testing.T) {
	if os.Getenv("ZENPLUS_TEST_SETTINGS_UI") != "1" {
		t.Skip("interactive visual test")
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	path := filepath.Join(t.TempDir(), "config", "agent.yaml")
	cfg := config.Default()
	cfg.DataDir = filepath.Join(filepath.Dir(path), "data")
	if err := config.Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	ui := &appUI{configPath: path, closing: true}
	if err := ui.create(false); err != nil {
		t.Fatal(err)
	}
	go ui.window.Synchronize(func() { ui.showSettings(); _ = ui.window.Close() })
	ui.window.Run()
	if ui.tray != nil {
		_ = ui.tray.Dispose()
	}
	after, err := config.LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if after.ControllerURL != cfg.ControllerURL {
		t.Fatal("cancel saved edited controller")
	}
}

func TestSaveSettingsRefusesAPMOnServerOnlyPackage(t *testing.T) {
	previousInstalled := apmComponentsInstalled
	defer func() { apmComponentsInstalled = previousInstalled }()
	apmComponentsInstalled = func() bool { return false }

	root := t.TempDir()
	path := filepath.Join(root, "config", "agent.yaml")
	cfg := config.Default()
	cfg.DataDir = root
	if err := config.Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	for _, profile := range []string{"combined", "apm"} {
		if err := saveSettingsFile(path, "10.11.40.32", profile); err == nil {
			t.Fatalf("%s profile saved on a package without APM components", profile)
		}
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("refused profile change modified the configuration")
	}
	if err := saveSettingsFile(path, "10.11.40.32", "infrastructure"); err != nil {
		t.Fatalf("server-only package could not save the infrastructure profile: %v", err)
	}
}
