package apm

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"zenplus-agent/internal/config"
	agentruntime "zenplus-agent/internal/runtime"
)

func TestIISConfigurationScriptLoadsAdministrationAssemblyFromIIS(t *testing.T) {
	for _, required := range []string{
		`System32\inetsrv\Microsoft.Web.Administration.dll`,
		`Add-Type -Path $administrationAssembly`,
	} {
		if !strings.Contains(iisConfigurationScript, required) {
			t.Fatalf("IIS configuration script is missing %q", required)
		}
	}
}

func TestCollectorConfigUsesLoopbackAndProtectedEnvironmentCredential(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	m := New(paths, func(string, ...any) {})
	cfg := config.Default()
	cfg.ControllerURL = "https://controller.example"
	if err := m.ensureConfig(cfg, "agent-id", "server-id"); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(paths.APMConfig)
	if err != nil {
		t.Fatal(err)
	}
	text := string(b)
	for _, required := range []string{
		"127.0.0.1:4317", "127.0.0.1:4318", "file_storage", "memory_limiter",
		"${env:ZENPLUS_APM_KEY}", "https://controller.example", "zenplus.agent.id",
	} {
		if !strings.Contains(text, required) {
			t.Fatalf("collector config missing %q: %s", required, text)
		}
	}
	if strings.Contains(text, "zpi_") {
		t.Fatal("collector config must not contain a plaintext ingest credential")
	}
}

func TestRuntimeClassification(t *testing.T) {
	cases := map[string]string{
		`C:\\Windows\\System32\\inetsrv\\w3wp.exe`:          "iis",
		`C:\\Program Files\\dotnet\\dotnet.exe service.dll`: "dotnet",
		`java.exe -jar app.jar`:                             "java",
		`node.exe server.js`:                                "node",
		`python.exe app.py`:                                 "python",
	}
	for command, expected := range cases {
		if got := classifyRuntime(command, command, command); got != expected {
			t.Fatalf("classifyRuntime(%q)=%q want %q", command, got, expected)
		}
	}
}

func TestExtractIISApplicationPool(t *testing.T) {
	cases := map[string]string{
		`c:\windows\system32\inetsrv\w3wp.exe -ap "DefaultAppPool" -v "v4.0"`: "DefaultAppPool",
		`w3wp.exe -ap InternalApiPool`:                                        "InternalApiPool",
		`w3wp.exe -v v4.0`:                                                    "",
	}
	for command, expected := range cases {
		if got := extractIISAppPool(command); got != expected {
			t.Fatalf("extractIISAppPool(%q)=%q want %q", command, got, expected)
		}
	}
}

func TestInstrumentationRequiresOfflineRuntimeBundleBeforeMutation(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	manager := New(paths, func(string, ...any) {})
	_, err := manager.Instrument(context.Background(), InstrumentationRequest{
		Enabled: true, Runtime: "java", TargetKind: "windows_service", TargetName: "example",
	})
	if err == nil || !strings.Contains(err.Error(), "offline Java") {
		t.Fatalf("expected an offline Java bundle error, got %v", err)
	}
	if _, statErr := os.Stat(paths.APMInstrumentationState); !os.IsNotExist(statErr) {
		t.Fatalf("unsupported request must not create state, stat error=%v", statErr)
	}
}

func TestInstrumentationRejectsUnsupportedPythonMutation(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	manager := New(paths, func(string, ...any) {})
	_, err := manager.Instrument(context.Background(), InstrumentationRequest{
		Enabled: true, Runtime: "python", TargetKind: "windows_service", TargetName: "example",
	})
	if err == nil || !strings.Contains(err.Error(), "supports IIS, .NET, Java, and Node.js") {
		t.Fatalf("expected the P2 compatibility gate, got %v", err)
	}
}

func TestConfigRejectsRemoteAPMBind(t *testing.T) {
	cfg := config.Default()
	cfg.APM.BindAddress = "0.0.0.0"
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected non-loopback APM receiver address to be rejected")
	}
}

func TestDeploymentArtifactSelectsHostedApplication(t *testing.T) {
	dir := t.TempDir()
	app := filepath.Join(dir, "orders.dll")
	if err := os.WriteFile(app, []byte("test-build"), 0o600); err != nil {
		t.Fatal(err)
	}
	got := deploymentArtifactPath("dotnet", `C:\Program Files\dotnet\dotnet.exe`, `dotnet.exe "orders.dll"`, dir)
	if got != app {
		t.Fatalf("artifact path=%q want %q", got, app)
	}
	fingerprint, modified := deploymentArtifactFingerprint(got)
	if len(fingerprint) != 64 || modified == nil {
		t.Fatalf("invalid deployment fingerprint %q modified=%v", fingerprint, modified)
	}
}
