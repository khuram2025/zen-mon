//go:build windows

package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"golang.org/x/sys/windows/svc/mgr"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
)

func TestParseOptionsVerifyPayload(t *testing.T) {
	opts, err := parseOptions([]string{"/verify-payload", "/quiet"})
	if err != nil {
		t.Fatal(err)
	}
	if !opts.verifyPayload || !opts.quiet {
		t.Fatalf("verification options = %#v", opts)
	}
}

func TestParseOptionsControllerCAFile(t *testing.T) {
	want := `C:\ProgramData\ZenPlus\Agent\trust\controller-ca.pem`
	opts, err := parseOptions([]string{"/quiet", "CONTROLLER_CA_FILE=" + want})
	if err != nil {
		t.Fatal(err)
	}
	if opts.controllerCA != want {
		t.Fatalf("controller CA file = %q, want %q", opts.controllerCA, want)
	}
}

func TestStandaloneUpgradeRemovesEveryRelatedLegacyMSIOnce(t *testing.T) {
	oldEnumerate := enumerateRelatedMSIProducts
	oldUninstall := uninstallLegacyMSIProduct
	defer func() {
		enumerateRelatedMSIProducts = oldEnumerate
		uninstallLegacyMSIProduct = oldUninstall
	}()
	first := `{2F1EF7B7-1111-4222-8333-444444444444}`
	second := `{D0423DB6-AAAA-4BBB-8CCC-DDDDDDDDDDDD}`
	enumerateRelatedMSIProducts = func() ([]string, error) {
		return []string{second, strings.ToLower(first), first}, nil
	}
	var removed []string
	uninstallLegacyMSIProduct = func(product string) error {
		removed = append(removed, product)
		return nil
	}

	if err := removeLegacyMSIRegistrations(options{}, func(options, string, ...any) {}); err != nil {
		t.Fatal(err)
	}
	want := []string{first, second}
	if !reflect.DeepEqual(removed, want) {
		t.Fatalf("removed MSI products = %v, want %v", removed, want)
	}
}

func TestMSIManagedUpgradeDoesNotReenterWindowsInstaller(t *testing.T) {
	oldEnumerate := enumerateRelatedMSIProducts
	defer func() { enumerateRelatedMSIProducts = oldEnumerate }()
	called := false
	enumerateRelatedMSIProducts = func() ([]string, error) {
		called = true
		return nil, nil
	}

	if err := removeLegacyMSIRegistrations(options{managedByMSI: true}, func(options, string, ...any) {}); err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("MSI embedded setup recursively enumerated Windows Installer products")
	}
}

func TestLegacyMSIRemovalIsMarkedAsNonDestructiveUpgrade(t *testing.T) {
	product := `{09F731E5-1111-4222-8333-444444444444}`
	args := legacyMSIUninstallArgs(product)
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "/x "+product) ||
		!strings.Contains(joined, "UPGRADINGPRODUCTCODE="+agentMSIUpgradeCode) {
		t.Fatalf("unsafe legacy MSI removal command: %q", joined)
	}
	if strings.Contains(strings.ToUpper(joined), "PURGE") {
		t.Fatalf("legacy MSI removal would purge ProgramData: %q", joined)
	}
}

func TestExistingServiceIsReboundToCurrentPayloadDuringUpgrade(t *testing.T) {
	l := testLayout(t)
	l.InstallDir = filepath.Join(t.TempDir(), "Program Files", "ZenPlus", "Agent")
	before := agentServiceState{
		Exists:         true,
		ConfigCaptured: true,
		Config: mgr.Config{
			BinaryPathName: `C:\ProgramData\ZenPlus\Agent\bin\zenplus-agent.exe service --config old.yaml`,
			StartType:      mgr.StartManual,
		},
	}

	after := serviceStateForInstalledPayload(before, l)
	if !strings.Contains(after.Config.BinaryPathName, filepath.Join(l.InstallDir, "zenplus-agent.exe")) ||
		!strings.Contains(after.Config.BinaryPathName, l.ConfigPath) {
		t.Fatalf("upgraded service still targets a legacy payload: %q", after.Config.BinaryPathName)
	}
	if after.Config.StartType != mgr.StartAutomatic {
		t.Fatalf("upgraded service start type = %d, want automatic", after.Config.StartType)
	}
	if before.Config.StartType != mgr.StartManual || strings.Contains(before.Config.BinaryPathName, l.InstallDir) {
		t.Fatal("service-state migration mutated the rollback snapshot")
	}
}

func TestWriteInstalledConfigRestoresCollectorsWhenLeavingAPMProfile(t *testing.T) {
	for _, profile := range []string{"combined", "infrastructure"} {
		t.Run(profile, func(t *testing.T) {
			l := testLayout(t)
			cfg := config.Default()
			cfg.APM.Profile = "apm"
			disableInfrastructureCollectors(&cfg)
			if err := config.Save(l.ConfigPath, cfg); err != nil {
				t.Fatalf("save initial config: %v", err)
			}

			if err := writeInstalledConfig(l, options{profile: profile}); err != nil {
				t.Fatalf("write installed config: %v", err)
			}
			got, err := config.LoadForEdit(l.ConfigPath)
			if err != nil {
				t.Fatalf("load installed config: %v", err)
			}
			if got.APM.Profile != profile {
				t.Fatalf("profile = %q, want %q", got.APM.Profile, profile)
			}
			if got.APM.Enabled != (profile == "combined") {
				t.Fatalf("APM enabled = %v for profile %q", got.APM.Enabled, profile)
			}
			assertInfrastructureCollectorsEnabled(t, got)
		})
	}
}

func TestWriteInstalledConfigRepairsCombinedProfileLeftDisabledByOlderInstaller(t *testing.T) {
	l := testLayout(t)
	cfg := config.Default()
	cfg.APM.Profile = "combined"
	disableInfrastructureCollectors(&cfg)
	if err := config.Save(l.ConfigPath, cfg); err != nil {
		t.Fatalf("save initial config: %v", err)
	}

	if err := writeInstalledConfig(l, options{profile: "combined"}); err != nil {
		t.Fatalf("write installed config: %v", err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatalf("load installed config: %v", err)
	}
	assertInfrastructureCollectorsEnabled(t, got)
}

func TestWriteInstalledConfigAPMProfileDisablesInfrastructureCollectors(t *testing.T) {
	l := testLayout(t)
	if err := config.Save(l.ConfigPath, config.Default()); err != nil {
		t.Fatalf("save initial config: %v", err)
	}
	if err := writeInstalledConfig(l, options{profile: "apm"}); err != nil {
		t.Fatalf("write installed config: %v", err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatalf("load installed config: %v", err)
	}
	if !got.APM.Enabled || got.APM.Profile != "apm" {
		t.Fatalf("APM state = enabled:%v profile:%q", got.APM.Enabled, got.APM.Profile)
	}
	if got.Collectors.CPU.Enabled || got.Collectors.Memory.Enabled || got.Collectors.Filesystem.Enabled ||
		got.Collectors.DiskIO.Enabled || got.Collectors.Network.Enabled || got.Collectors.Processes.Enabled ||
		got.Collectors.Services.Enabled || got.Collectors.EventLog.Enabled {
		t.Fatal("APM-only profile left an infrastructure collector enabled")
	}
	if !got.Collectors.Inventory.Enabled {
		t.Fatal("APM-only profile must keep inventory enabled")
	}
}

func TestWriteInstalledConfigDoesNotOverrideCollectorChoiceWithinCombinedProfile(t *testing.T) {
	l := testLayout(t)
	cfg := config.Default()
	cfg.APM.Profile = "combined"
	cfg.Collectors.CPU.Enabled = false
	if err := config.Save(l.ConfigPath, cfg); err != nil {
		t.Fatalf("save initial config: %v", err)
	}
	if err := writeInstalledConfig(l, options{profile: "combined"}); err != nil {
		t.Fatalf("write installed config: %v", err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatalf("load installed config: %v", err)
	}
	if got.Collectors.CPU.Enabled {
		t.Fatal("same-profile upgrade overwrote an explicit collector choice")
	}
}

func TestWriteInstalledConfigPreservesExistingProfileWhenNoProfileWasSupplied(t *testing.T) {
	l := testLayout(t)
	cfg := config.Default()
	if err := config.ApplyProfile(&cfg, "infrastructure"); err != nil {
		t.Fatal(err)
	}
	cfg.Collectors.EventLog.Enabled = false
	if err := config.Save(l.ConfigPath, cfg); err != nil {
		t.Fatal(err)
	}
	if err := writeInstalledConfig(l, options{}); err != nil {
		t.Fatal(err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	if got.APM.Profile != "infrastructure" || got.APM.Enabled {
		t.Fatalf("upgrade changed existing profile: %#v", got.APM)
	}
	if got.Collectors.EventLog.Enabled {
		t.Fatal("upgrade overwrote an explicit collector choice")
	}
}

func TestWriteInstalledConfigRepairsBrokenCombinedProfileWithoutMSIOverride(t *testing.T) {
	l := testLayout(t)
	cfg := config.Default()
	disableInfrastructureCollectors(&cfg)
	if err := config.Save(l.ConfigPath, cfg); err != nil {
		t.Fatal(err)
	}
	if err := writeInstalledConfig(l, options{}); err != nil {
		t.Fatal(err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	assertInfrastructureCollectorsEnabled(t, got)
}

func TestWriteInstalledConfigRemovesLegacyRegistrationIDs(t *testing.T) {
	l := testLayout(t)
	cfg := config.Default()
	cfg.AgentID = "agt_stale"
	cfg.ServerID = "srv_stale"
	cfg.PolicyID = "policy_stale"
	if err := config.Save(l.ConfigPath, cfg); err != nil {
		t.Fatal(err)
	}

	if err := writeInstalledConfig(l, options{}); err != nil {
		t.Fatal(err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	if got.AgentID != "" || got.ServerID != "" || got.PolicyID != "" {
		t.Fatalf("legacy appliance registration fields survived upgrade: agent=%q server=%q policy=%q", got.AgentID, got.ServerID, got.PolicyID)
	}
}

func TestUpgradePersistsLegacyRegistrationIDsBeforeConfigSanitization(t *testing.T) {
	l := testLayout(t)
	cfg := config.Default()
	cfg.AgentID = "agt_legacy"
	cfg.ServerID = "srv_legacy"
	if err := config.Save(l.ConfigPath, cfg); err != nil {
		t.Fatal(err)
	}
	identityPath := filepath.Join(l.DataDir, "state", "identity.json")
	if err := os.MkdirAll(filepath.Dir(identityPath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := preserveLegacyConfigIdentity(l.ConfigPath, identityPath); err != nil {
		t.Fatal(err)
	}
	if err := writeInstalledConfig(l, options{}); err != nil {
		t.Fatal(err)
	}

	persisted, _, err := identity.LoadOrCreate(identityPath, "agt_stale", "srv_stale")
	if err != nil {
		t.Fatal(err)
	}
	if persisted.AgentID != cfg.AgentID || persisted.ServerID != cfg.ServerID {
		t.Fatalf("legacy registration was not preserved: agent=%q server=%q", persisted.AgentID, persisted.ServerID)
	}
	installed, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	if installed.AgentID != "" || installed.ServerID != "" {
		t.Fatalf("legacy config IDs survived sanitization: %#v", installed)
	}
}

func TestWriteInstalledConfigAppliesExplicitControllerCAFile(t *testing.T) {
	l := testLayout(t)
	caFile := filepath.Join(l.DataDir, "trust", "controller-ca.pem")
	if err := writeInstalledConfig(l, options{controllerCA: caFile}); err != nil {
		t.Fatal(err)
	}
	got, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	if got.Security.ControllerCAFile != caFile || !got.VerifyTLS {
		t.Fatalf("installed TLS trust config = ca:%q verify:%v", got.Security.ControllerCAFile, got.VerifyTLS)
	}
}

func TestValidateEmbeddedPayloadsRequiresCompleteServerAndAPMBundle(t *testing.T) {
	componentNames := []string{
		"zenplus-telemetry-gateway", "opentelemetry-dotnet-auto", "opentelemetry-javaagent",
		"opentelemetry-node-auto", "opentelemetry-python-auto",
	}
	components := make([]map[string]string, 0, len(componentNames))
	for _, name := range componentNames {
		components = append(components, map[string]string{"name": name})
	}
	payloads := []payloadFile{
		{Name: "zenplus-agent.exe", Data: []byte{1}},
		{Name: "zenplus-agentctl.exe", Data: []byte{1}},
		{Name: "zenplus-agent-app.exe", Data: []byte{1}},
		{Name: "zenplus-agent-user.exe", Data: []byte{1}},
		{Name: "apm/gateway/zenplus-telemetry-gateway.exe", Data: []byte{1}},
		{Name: "apm/instrumentation/dotnet/net/OpenTelemetry.AutoInstrumentation.StartupHook.dll", Data: []byte{1}},
		{Name: "apm/instrumentation/dotnet/win-x64/OpenTelemetry.AutoInstrumentation.Native.dll", Data: []byte{1}},
		{Name: "apm/instrumentation/java/opentelemetry-javaagent.jar", Data: []byte{1}},
		{Name: "apm/instrumentation/node/bootstrap.js", Data: []byte{1}},
		{Name: "apm/instrumentation/node/node_modules/@opentelemetry/auto-instrumentations-node/package.json", Data: []byte{1}},
		{Name: "apm/instrumentation/python/wheelhouse/opentelemetry_distro-0.65b0-py3-none-any.whl", Data: []byte{1}},
		{Name: "apm/instrumentation/python/wheelhouse/opentelemetry_instrumentation_flask-0.65b0-py3-none-any.whl", Data: []byte{1}},
		{Name: "apm/instrumentation/python/wheelhouse/opentelemetry_instrumentation_requests-0.65b0-py3-none-any.whl", Data: []byte{1}},
		{Name: "apm/instrumentation/python/Install-ZenPlusPythonTracing.ps1", Data: []byte{1}},
		{Name: "apm/instrumentation/python/constraints.txt", Data: []byte{1}},
		{Name: "apm/instrumentation/python/README.txt", Data: []byte{1}},
	}
	files := make([]map[string]any, 0, len(payloads))
	for _, payload := range payloads {
		name := filepath.ToSlash(payload.Name)
		if !strings.HasPrefix(strings.ToLower(name), "apm/") {
			continue
		}
		digest := sha256.Sum256(payload.Data)
		files = append(files, map[string]any{
			"path": strings.TrimPrefix(name, "apm/"), "size": len(payload.Data), "sha256": hex.EncodeToString(digest[:]),
		})
	}
	manifest, err := json.Marshal(map[string]any{"components": components, "files": files})
	if err != nil {
		t.Fatal(err)
	}
	payloads = append(payloads, payloadFile{Name: "apm/bundle-manifest.json", Data: manifest})
	if err := validateEmbeddedPayloads(payloads); err != nil {
		t.Fatalf("complete payload rejected: %v", err)
	}
	payloads = payloads[:len(payloads)-2]
	payloads = append(payloads, payloadFile{Name: "apm/bundle-manifest.json", Data: manifest})
	if err := validateEmbeddedPayloads(payloads); err == nil {
		t.Fatal("incomplete Python bundle was accepted")
	}
}

func TestValidateEmbeddedPayloadsRejectsUnlistedAndUnsafeAPMFiles(t *testing.T) {
	payloads := completeTestPayloads(t)
	payloads = append(payloads, payloadFile{Name: "apm/instrumentation/node/unlisted.js", Data: []byte("extra")})
	if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "not listed") {
		t.Fatalf("unlisted APM payload error = %v", err)
	}

	payloads = completeTestPayloads(t)
	payloads[0].Name = `..\outside.exe`
	if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "escapes") {
		t.Fatalf("traversing payload error = %v", err)
	}
}

func TestValidateEmbeddedPayloadsAcceptsManifestedZeroByteAPMPlaceholder(t *testing.T) {
	payloads := completeTestPayloads(t)
	manifestIndex := len(payloads) - 1
	manifest := decodeTestManifest(t, payloads[manifestIndex].Data)
	emptyDigest := sha256.Sum256(nil)
	manifest.Files = append(manifest.Files, map[string]any{
		"path":   "instrumentation/dotnet/netfx/net472/_._",
		"size":   0,
		"sha256": hex.EncodeToString(emptyDigest[:]),
	})
	payloads[manifestIndex].Data = encodeTestManifest(t, manifest)
	payloads = append(payloads, payloadFile{
		Name: "apm/instrumentation/dotnet/netfx/net472/_._",
	})

	if err := validateEmbeddedPayloads(payloads); err != nil {
		t.Fatalf("manifested zero-byte APM placeholder rejected: %v", err)
	}
}

func TestValidateEmbeddedPayloadsRejectsEmptyNonAPMPayload(t *testing.T) {
	payloads := completeTestPayloads(t)
	payloads[0].Data = nil
	if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "is empty") {
		t.Fatalf("empty non-APM payload error = %v", err)
	}
}

func TestValidateEmbeddedPayloadsRejectsTamperedAndAmbiguousManifests(t *testing.T) {
	t.Run("tampered payload", func(t *testing.T) {
		payloads := completeTestPayloads(t)
		payloads[4].Data = []byte{2}
		if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "SHA-256") {
			t.Fatalf("tampered payload error = %v", err)
		}
	})

	t.Run("duplicate embedded payload", func(t *testing.T) {
		payloads := completeTestPayloads(t)
		payloads = append(payloads, payloads[0])
		if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "duplicated") {
			t.Fatalf("duplicate payload error = %v", err)
		}
	})

	t.Run("duplicate manifest path", func(t *testing.T) {
		payloads := completeTestPayloads(t)
		manifest := decodeTestManifest(t, payloads[len(payloads)-1].Data)
		manifest.Files = append(manifest.Files, manifest.Files[0])
		payloads[len(payloads)-1].Data = encodeTestManifest(t, manifest)
		if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "duplicates file") {
			t.Fatalf("duplicate manifest path error = %v", err)
		}
	})

	t.Run("traversing manifest path", func(t *testing.T) {
		payloads := completeTestPayloads(t)
		manifest := decodeTestManifest(t, payloads[len(payloads)-1].Data)
		manifest.Files[0]["path"] = "../outside.exe"
		payloads[len(payloads)-1].Data = encodeTestManifest(t, manifest)
		if err := validateEmbeddedPayloads(payloads); err == nil || !strings.Contains(err.Error(), "escapes") {
			t.Fatalf("traversing manifest path error = %v", err)
		}
	})
}

func TestInstallDirectoryTransactionRestoresPriorPayload(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "Agent")
	if err := os.MkdirAll(installDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(installDir, "version.txt"), []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	stage, err := os.MkdirTemp(root, ".zenplus-agent-stage-")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "version.txt"), []byte("new"), 0o600); err != nil {
		t.Fatal(err)
	}

	transaction, err := activateStagedInstall(stage, installDir)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(filepath.Join(installDir, "version.txt")); err != nil || string(got) != "new" {
		t.Fatalf("activated payload = %q, err=%v", got, err)
	}
	if err := transaction.Rollback(); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(filepath.Join(installDir, "version.txt")); err != nil || string(got) != "old" {
		t.Fatalf("restored payload = %q, err=%v", got, err)
	}
}

func TestFileSnapshotRestoresAndRemovesConfiguration(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "config", "agent.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	snapshot, err := captureFileSnapshot(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("new"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := snapshot.Restore(); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != "old" {
		t.Fatalf("restored config = %q, err=%v", got, err)
	}

	newPath := filepath.Join(root, "config", "new.yaml")
	missing, err := captureFileSnapshot(newPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(newPath, []byte("created"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := missing.Restore(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(newPath); !os.IsNotExist(err) {
		t.Fatalf("new configuration remains after restore: %v", err)
	}
}

func completeTestPayloads(t *testing.T) []payloadFile {
	t.Helper()
	componentNames := []string{
		"zenplus-telemetry-gateway", "opentelemetry-dotnet-auto", "opentelemetry-javaagent",
		"opentelemetry-node-auto", "opentelemetry-python-auto",
	}
	components := make([]map[string]string, 0, len(componentNames))
	for _, name := range componentNames {
		components = append(components, map[string]string{"name": name})
	}
	payloads := []payloadFile{
		{Name: "zenplus-agent.exe", Data: []byte{1}},
		{Name: "zenplus-agentctl.exe", Data: []byte{1}},
		{Name: "zenplus-agent-app.exe", Data: []byte{1}},
		{Name: "zenplus-agent-user.exe", Data: []byte{1}},
		{Name: "apm/gateway/zenplus-telemetry-gateway.exe", Data: []byte{1}},
		{Name: "apm/instrumentation/dotnet/net/OpenTelemetry.AutoInstrumentation.StartupHook.dll", Data: []byte{1}},
		{Name: "apm/instrumentation/dotnet/win-x64/OpenTelemetry.AutoInstrumentation.Native.dll", Data: []byte{1}},
		{Name: "apm/instrumentation/java/opentelemetry-javaagent.jar", Data: []byte{1}},
		{Name: "apm/instrumentation/node/bootstrap.js", Data: []byte{1}},
		{Name: "apm/instrumentation/node/node_modules/@opentelemetry/auto-instrumentations-node/package.json", Data: []byte{1}},
		{Name: "apm/instrumentation/python/wheelhouse/opentelemetry_distro-0.65b0-py3-none-any.whl", Data: []byte{1}},
		{Name: "apm/instrumentation/python/wheelhouse/opentelemetry_instrumentation_flask-0.65b0-py3-none-any.whl", Data: []byte{1}},
		{Name: "apm/instrumentation/python/wheelhouse/opentelemetry_instrumentation_requests-0.65b0-py3-none-any.whl", Data: []byte{1}},
		{Name: "apm/instrumentation/python/Install-ZenPlusPythonTracing.ps1", Data: []byte{1}},
		{Name: "apm/instrumentation/python/constraints.txt", Data: []byte{1}},
		{Name: "apm/instrumentation/python/README.txt", Data: []byte{1}},
	}
	files := make([]map[string]any, 0, len(payloads))
	for _, payload := range payloads {
		name := filepath.ToSlash(payload.Name)
		if !strings.HasPrefix(strings.ToLower(name), "apm/") {
			continue
		}
		digest := sha256.Sum256(payload.Data)
		files = append(files, map[string]any{
			"path": strings.TrimPrefix(name, "apm/"), "size": len(payload.Data), "sha256": hex.EncodeToString(digest[:]),
		})
	}
	manifest, err := json.Marshal(map[string]any{"components": components, "files": files})
	if err != nil {
		t.Fatal(err)
	}
	return append(payloads, payloadFile{Name: "apm/bundle-manifest.json", Data: manifest})
}

type testBundleManifest struct {
	Components []map[string]string `json:"components"`
	Files      []map[string]any    `json:"files"`
}

func decodeTestManifest(t *testing.T, data []byte) testBundleManifest {
	t.Helper()
	var manifest testBundleManifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	return manifest
}

func encodeTestManifest(t *testing.T, manifest testBundleManifest) []byte {
	t.Helper()
	data, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func testLayout(t *testing.T) layout {
	t.Helper()
	root := t.TempDir()
	return layout{
		DataDir:    filepath.Join(root, "data"),
		ConfigPath: filepath.Join(root, "config", "agent.yaml"),
	}
}

func disableInfrastructureCollectors(cfg *config.Config) {
	cfg.Collectors.CPU.Enabled = false
	cfg.Collectors.Memory.Enabled = false
	cfg.Collectors.Filesystem.Enabled = false
	cfg.Collectors.DiskIO.Enabled = false
	cfg.Collectors.Network.Enabled = false
	cfg.Collectors.Processes.Enabled = false
	cfg.Collectors.Services.Enabled = false
	cfg.Collectors.EventLog.Enabled = false
	cfg.Collectors.Inventory.Enabled = true
}

func assertInfrastructureCollectorsEnabled(t *testing.T, cfg config.Config) {
	t.Helper()
	if !cfg.Collectors.CPU.Enabled || !cfg.Collectors.Memory.Enabled || !cfg.Collectors.Filesystem.Enabled ||
		!cfg.Collectors.DiskIO.Enabled || !cfg.Collectors.Network.Enabled || !cfg.Collectors.Processes.Enabled ||
		!cfg.Collectors.Services.Enabled || !cfg.Collectors.EventLog.Enabled || !cfg.Collectors.Inventory.Enabled {
		t.Fatal("an infrastructure collector was not restored")
	}
}
