//go:build windows

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"zenplus-agent/internal/config"
)

func withInstallerFlavor(t *testing.T, flavor string) {
	t.Helper()
	previous := installerFlavor
	installerFlavor = flavor
	t.Cleanup(func() { installerFlavor = previous })
}

func coreTestPayloads() []payloadFile {
	return []payloadFile{
		{Name: "zenplus-agent.exe", Data: []byte{1}},
		{Name: "zenplus-agentctl.exe", Data: []byte{1}},
		{Name: "zenplus-agent-app.exe", Data: []byte{1}},
		{Name: "zenplus-agent-user.exe", Data: []byte{1}},
		{Name: "ZenPlusAgentSetup.exe", Data: []byte("uninstaller")},
	}
}

func TestCorePayloadNeedsServerFilesAndRejectsAPMFiles(t *testing.T) {
	withInstallerFlavor(t, flavorCore)
	if err := validateEmbeddedPayloads(coreTestPayloads()); err != nil {
		t.Fatalf("complete core payload rejected: %v", err)
	}
	missing := coreTestPayloads()[1:]
	if err := validateEmbeddedPayloads(missing); err == nil || !strings.Contains(err.Error(), "zenplus-agent.exe") {
		t.Fatalf("core payload without the agent accepted: %v", err)
	}
	withAPM := append(coreTestPayloads(), payloadFile{Name: "apm/gateway/zenplus-telemetry-gateway.exe", Data: []byte{1}})
	if err := validateEmbeddedPayloads(withAPM); err == nil || !strings.Contains(err.Error(), "APM file") {
		t.Fatalf("core payload carrying APM files accepted: %v", err)
	}
}

func TestFullPayloadStillRequiresTheAPMBundle(t *testing.T) {
	withInstallerFlavor(t, flavorFull)
	if err := validateEmbeddedPayloads(coreTestPayloads()); err == nil || !strings.Contains(err.Error(), "apm/") {
		t.Fatalf("full payload without APM accepted: %v", err)
	}
	if err := validateEmbeddedPayloads(completeTestPayloads(t)); err != nil {
		t.Fatalf("complete full payload rejected: %v", err)
	}
}

func TestCoreInstallerRefusesToRemoveAPMInUse(t *testing.T) {
	withInstallerFlavor(t, flavorCore)
	infra := config.Default()
	if err := requireFlavorSupportsConfig(infra, 0); err != nil {
		t.Fatalf("infrastructure install refused: %v", err)
	}
	if err := requireFlavorSupportsConfig(infra, 2); err == nil || !strings.Contains(err.Error(), "instrumented") {
		t.Fatalf("core install over instrumented applications allowed: %v", err)
	}
	for _, profile := range []string{"combined", "apm"} {
		cfg := config.Default()
		if err := config.ApplyProfile(&cfg, profile); err != nil {
			t.Fatal(err)
		}
		if err := requireFlavorSupportsConfig(cfg, 0); err == nil || !strings.Contains(err.Error(), "needs APM") {
			t.Fatalf("core install with %s profile allowed: %v", profile, err)
		}
	}

	withInstallerFlavor(t, flavorFull)
	combined := config.Default()
	_ = config.ApplyProfile(&combined, "combined")
	if err := requireFlavorSupportsConfig(combined, 3); err != nil {
		t.Fatalf("full install refused: %v", err)
	}
}

func TestStagingUsesPayloadUninstallerInsteadOfCopyingSetup(t *testing.T) {
	root := t.TempDir()
	l := layout{InstallDir: filepath.Join(root, "ZenPlus", "Agent")}
	stage, err := stageInstallPayloads(l, coreTestPayloads())
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(stage)
	got, err := os.ReadFile(filepath.Join(stage, "ZenPlusAgentSetup.exe"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, []byte("uninstaller")) {
		t.Fatalf("staged uninstaller is %d bytes, want the payload uninstaller, not a copy of setup", len(got))
	}
}

func TestUserRunnerUsesDashboardBinaryUnlessLegacyRunnerExists(t *testing.T) {
	dir := t.TempDir()
	l := layout{InstallDir: dir, ConfigPath: `C:\Users\me\AppData\Local\ZenPlus\Agent\config\agent.yaml`}
	runner, args := userRunnerCommand(l)
	if filepath.Base(runner) != "zenplus-agent-app.exe" || args[0] != "--run-agent" {
		t.Fatalf("runner = %s %v, want the dashboard binary in --run-agent mode", runner, args)
	}
	if got := quoteArgs(args); got != `--run-agent --config "`+l.ConfigPath+`"` {
		t.Fatalf("shortcut arguments = %s", got)
	}
	// A rolled-back upgrade restores a pre-1.13 install with its own runner.
	if err := os.WriteFile(filepath.Join(dir, "zenplus-agent-user.exe"), []byte("legacy"), 0o755); err != nil {
		t.Fatal(err)
	}
	runner, args = userRunnerCommand(l)
	if filepath.Base(runner) != "zenplus-agent-user.exe" || args[0] != "--config" {
		t.Fatalf("legacy install runner = %s %v", runner, args)
	}
}

func TestStagingWritesAgentctlShim(t *testing.T) {
	root := t.TempDir()
	stage, err := stageInstallPayloads(layout{InstallDir: filepath.Join(root, "ZenPlus", "Agent")}, coreTestPayloads())
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(stage)
	got, err := os.ReadFile(filepath.Join(stage, agentctlShimName))
	if err != nil || !strings.Contains(string(got), `zenplus-agent.exe" %*`) {
		t.Fatalf("agentctl shim = %q, %v", got, err)
	}
}
