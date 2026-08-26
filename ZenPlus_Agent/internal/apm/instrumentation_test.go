package apm

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestManagedTargetsIncludePendingRestartDependencies(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	state := instrumentationState{Version: 2, Targets: map[string]instrumentationTarget{
		"iis_app_pool:zeta":   {TargetKind: "iis_app_pool", TargetName: "Zeta", Enabled: true},
		"iis_app_pool:alpha":  {TargetKind: "iis_app_pool", TargetName: "Alpha", Enabled: true},
		"iis_app_pool:old":    {TargetKind: "iis_app_pool", TargetName: "Old", Enabled: false},
		"iis_app_pool:loaded": {TargetKind: "iis_app_pool", TargetName: "Loaded", PendingRestart: true},
		"windows_service:api": {TargetKind: "windows_service", TargetName: "Api", Enabled: true},
		"windows_service:old": {TargetKind: "windows_service", TargetName: "OldApi", PendingRestart: true},
	}}
	if err := writeInstrumentationState(path, state); err != nil {
		t.Fatal(err)
	}
	targets, err := ManagedIISTargets(path)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"Alpha", "Loaded", "Zeta"}; !reflect.DeepEqual(targets, want) {
		t.Fatalf("ManagedIISTargets() = %v, want %v", targets, want)
	}
	count, err := ManagedInstrumentationCount(path)
	if err != nil {
		t.Fatal(err)
	}
	if count != 5 {
		t.Fatalf("ManagedInstrumentationCount() = %d, want 5", count)
	}
	services, err := ManagedWindowsServiceTargets(path)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"Api", "OldApi"}; !reflect.DeepEqual(services, want) {
		t.Fatalf("ManagedWindowsServiceTargets() = %v, want %v", services, want)
	}
}

func TestRollbackAllNoStateIsSafe(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "missing.json")
	if err := RollbackAll(context.Background(), statePath, t.TempDir(), true); err != nil {
		t.Fatalf("RollbackAll() with no managed state: %v", err)
	}
}

func TestManagedIISTargetsRejectsCorruptState(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte("not-json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ManagedIISTargets(path); err == nil {
		t.Fatal("expected corrupt instrumentation state to be rejected")
	}
}
