//go:build windows

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"zenplus-agent/internal/config"
)

func TestMSIInvokesSetupForInstallAndRepair(t *testing.T) {
	wxsPath := filepath.Join("..", "..", "packaging", "ZenPlusAgent.wxs")
	contents, err := os.ReadFile(wxsPath)
	if err != nil {
		t.Fatalf("read %s: %v", wxsPath, err)
	}
	wxs := strings.ReplaceAll(string(contents), "\r\n", "\n")
	if !strings.Contains(wxs, `<Property Id="CONTROLLER_URL" Secure="yes" />`) {
		t.Fatal("MSI must expose a secure CONTROLLER_URL property")
	}
	if !strings.Contains(wxs, `<Property Id="CONTROLLER_CA_FILE" Secure="yes" />`) ||
		!strings.Contains(wxs, `CONTROLLER_CA_FILE=&quot;[CONTROLLER_CA_FILE]&quot;`) {
		t.Fatal("MSI must securely forward the optional controller CA bundle")
	}
	if !strings.Contains(wxs, `<Property Id="INSTALL_PROFILE" Secure="yes" />`) {
		t.Fatal("MSI must expose a secure INSTALL_PROFILE property")
	}
	if strings.Contains(wxs, `<Property Id="INSTALL_PROFILE" Value=`) {
		t.Fatal("MSI must not default INSTALL_PROFILE because repair must preserve the installed profile")
	}
	if !strings.Contains(wxs, `INSTALL_PROFILE=&quot;[INSTALL_PROFILE]&quot;`) {
		t.Fatal("MSI setup custom action does not forward INSTALL_PROFILE")
	}
	if !strings.Contains(wxs, `<Custom Action="InstallViaExe" After="InstallInitialize">NOT REMOVE</Custom>`) {
		t.Fatal("MSI setup custom action must run for both initial install and repair")
	}
	if !strings.Contains(wxs, `<MajorUpgrade Schedule="afterInstallExecute"`) {
		t.Fatal("MSI major upgrade must preserve the old product until the new setup action succeeds")
	}
	start := strings.Index(wxs, "<CustomAction\n      Id=\"UninstallViaExe\"")
	if start < 0 {
		t.Fatal("MSI uninstall custom action was not found")
	}
	end := strings.Index(wxs[start:], "/>")
	if end < 0 || !strings.Contains(wxs[start:start+end], "Return=\"check\"") {
		t.Fatal("MSI uninstall custom action must fail when the managed uninstaller fails")
	}
	if got := config.Default().APM.Profile; got != "combined" {
		t.Fatalf("fresh-install config profile = %q, want %q", got, "combined")
	}
}

func TestInstallerIncludesNuGetPlaceholderFilesAndVerifiesBuiltPayload(t *testing.T) {
	embedSource, err := os.ReadFile("payload_embed.go")
	if err != nil {
		t.Fatalf("read payload embed source: %v", err)
	}
	if !strings.Contains(strings.ReplaceAll(string(embedSource), "\r\n", "\n"), "//go:embed all:payload") {
		t.Fatal("installer embed must include NuGet `_._` placeholder files")
	}

	buildPath := filepath.Join("..", "..", "scripts", "build.ps1")
	buildSource, err := os.ReadFile(buildPath)
	if err != nil {
		t.Fatalf("read %s: %v", buildPath, err)
	}
	if !strings.Contains(strings.ReplaceAll(string(buildSource), "\r\n", "\n"), `@("/verify-payload", "/quiet")`) {
		t.Fatal("build must execute the finished setup's embedded-payload verification gate")
	}
}

func TestLegacyMSICleanupRunsOnlyAfterInstallCommit(t *testing.T) {
	mainSource, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	source := strings.ReplaceAll(string(mainSource), "\r\n", "\n")
	commit := strings.Index(source, "transactionComplete = true")
	cleanup := strings.Index(source, "removeLegacyMSIRegistrations(opts, logStep)")
	if commit < 0 || cleanup < 0 || cleanup < commit {
		t.Fatal("legacy MSI ownership must be removed only after the replacement install is committed")
	}
	if strings.Count(source, "removeLegacyMSIRegistrations(opts, logStep)") != 1 {
		t.Fatal("install should have exactly one committed legacy MSI cleanup point")
	}
}
