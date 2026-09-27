//go:build windows

package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLastSetupFailureReadsTheElevatedSetupLog(t *testing.T) {
	log := filepath.Join(t.TempDir(), "setup.log")
	content := "Installing ZenPlus Agent 1.14.3 (machine)\r\nSetup failed: validate controller URL: missing host\r\n"
	if err := os.WriteFile(log, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := lastSetupFailure(log); got != "validate controller URL: missing host" {
		t.Fatalf("reason = %q", got)
	}
	if got := lastSetupFailure(filepath.Join(t.TempDir(), "missing.log")); got != "" {
		t.Fatalf("missing log gave %q", got)
	}
}

func TestHasArgMatchesSwitchesOnly(t *testing.T) {
	args := []string{"/quiet", "-Uninstall", "CONTROLLER_URL=https://x", "/from-temp"}
	for _, name := range []string{"quiet", "uninstall", "from-temp"} {
		if !hasArg(args, name) {
			t.Errorf("%s not found", name)
		}
	}
	if hasArg(args, "purge") || hasArg(args, "controller") {
		t.Error("matched a switch that is not present")
	}
}

func TestCapitalizeReasons(t *testing.T) {
	if got := capitalize("administrator approval was cancelled."); got != "Administrator approval was cancelled" {
		t.Fatalf("capitalize = %q", got)
	}
}
