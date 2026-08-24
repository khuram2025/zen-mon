package apm

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
	"zenplus-agent/internal/client"
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
	fingerprint, changed, err := m.ensureConfig(cfg, "agent-id", "server-id")
	if err != nil {
		t.Fatal(err)
	}
	if !changed || len(fingerprint) != 64 {
		t.Fatalf("first config write changed=%v fingerprint=%q", changed, fingerprint)
	}
	b, err := os.ReadFile(paths.APMConfig)
	if err != nil {
		t.Fatal(err)
	}
	text := string(b)
	for _, required := range []string{
		"127.0.0.1:4317", "127.0.0.1:4318", "file_storage", "memory_limiter",
		"${env:ZENPLUS_APM_KEY}", "https://controller.example", "resource/zenplus",
		"zenplus.agent_id", "zenplus.server_id", "sizer: items", "port: 18888",
	} {
		if !strings.Contains(text, required) {
			t.Fatalf("collector config missing %q: %s", required, text)
		}
	}
	if strings.Contains(text, "zpi_") {
		t.Fatal("collector config must not contain a plaintext ingest credential")
	}
	for _, legacy := range []string{"attributes/zenplus", "zenplus.agent.id", "zenplus.server.id"} {
		if strings.Contains(text, legacy) {
			t.Fatalf("collector config contains legacy non-resource correlation key %q", legacy)
		}
	}
	var document struct {
		Processors map[string]struct {
			Attributes []struct {
				Key    string `yaml:"key"`
				Value  string `yaml:"value"`
				Action string `yaml:"action"`
			} `yaml:"attributes"`
		} `yaml:"processors"`
		Service struct {
			Pipelines map[string]struct {
				Processors []string `yaml:"processors"`
			} `yaml:"pipelines"`
		} `yaml:"service"`
	}
	if err := yaml.Unmarshal(b, &document); err != nil {
		t.Fatalf("parse generated collector config: %v", err)
	}
	resourceProcessor, ok := document.Processors["resource/zenplus"]
	if !ok {
		t.Fatal("generated config does not define the ZenPlus resource processor")
	}
	resourceValues := map[string]string{}
	for _, attribute := range resourceProcessor.Attributes {
		if attribute.Action != "upsert" {
			t.Fatalf("resource attribute %q action=%q want upsert", attribute.Key, attribute.Action)
		}
		resourceValues[attribute.Key] = attribute.Value
	}
	if resourceValues["zenplus.agent_id"] != "agent-id" || resourceValues["zenplus.server_id"] != "server-id" {
		t.Fatalf("generated resource correlation attributes=%v", resourceValues)
	}
	if !containsString(document.Service.Pipelines["traces"].Processors, "resource/zenplus") {
		t.Fatalf("trace pipeline does not use resource/zenplus: %v", document.Service.Pipelines["traces"].Processors)
	}
	secondFingerprint, changed, err := m.ensureConfig(cfg, "agent-id", "server-id")
	if err != nil {
		t.Fatal(err)
	}
	if changed || secondFingerprint != fingerprint {
		t.Fatalf("unchanged config changed=%v fingerprint=%q want %q", changed, secondFingerprint, fingerprint)
	}
	cfg.ControllerURL = "https://replacement.example"
	replacementFingerprint, changed, err := m.ensureConfig(cfg, "agent-id", "server-id")
	if err != nil {
		t.Fatal(err)
	}
	if !changed || replacementFingerprint == fingerprint {
		t.Fatalf("replacement config changed=%v fingerprint=%q", changed, replacementFingerprint)
	}
}

func TestCollectorDistributionContainsEveryRuntimeComponent(t *testing.T) {
	builderPath := filepath.Join("..", "..", "packaging", "apm-collector-builder.yaml")
	builder, err := os.ReadFile(builderPath)
	if err != nil {
		t.Fatalf("read collector builder specification: %v", err)
	}
	text := string(builder)
	for _, required := range []string{
		"version: " + gatewayVersion,
		"receiver/otlpreceiver",
		"processor/memorylimiterprocessor",
		"processor/resourceprocessor",
		"processor/batchprocessor",
		"exporter/otlphttpexporter",
		"extension/healthcheckextension",
		"extension/storage/filestorage",
		"confmap/provider/envprovider",
		"confmap/provider/fileprovider",
		"confmap/provider/yamlprovider",
	} {
		if !strings.Contains(text, required) {
			t.Fatalf("collector builder specification is missing %q", required)
		}
	}
	if strings.Contains(text, "processor/attributesprocessor") {
		t.Fatal("collector builder still includes the obsolete attributes processor instead of the required resource processor")
	}
}

func TestCredentialMetadataReusesOnlyExactBinding(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	server, calls := newAPMEnrollServer(t)
	defer server.Close()
	api, err := client.New(server.URL, "", true, "agent-a", "agent-api-key")
	if err != nil {
		t.Fatal(err)
	}
	binding := newCredentialBinding(server.URL, "agent-a", "server-a", "prod")
	m := New(paths, func(string, ...any) {})

	first, changed, err := m.ensureCredential(context.Background(), api, binding)
	if err != nil {
		t.Fatal(err)
	}
	if !changed || calls.Load() != 1 {
		t.Fatalf("first enrollment changed=%v calls=%d", changed, calls.Load())
	}
	meta, err := readCredentialMetadata(paths.APMCredentialMeta)
	if err != nil {
		t.Fatal(err)
	}
	if meta.ControllerURL != binding.ControllerURL || meta.AgentID != binding.AgentID ||
		meta.ServerID != binding.ServerID || meta.Environment != binding.Environment ||
		meta.KeyID != first.KeyID || !meta.matches(binding, first.Key) {
		t.Fatalf("credential metadata does not match binding: %#v", meta)
	}
	metadataJSON, err := os.ReadFile(paths.APMCredentialMeta)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(metadataJSON), first.Key) {
		t.Fatal("credential metadata contains the plaintext APM key")
	}

	second, changed, err := m.ensureCredential(context.Background(), api, binding)
	if err != nil {
		t.Fatal(err)
	}
	if changed || calls.Load() != 1 {
		t.Fatalf("exact binding should reuse credential, changed=%v calls=%d", changed, calls.Load())
	}
	if second != first {
		t.Fatalf("reused credential=%#v want %#v", second, first)
	}
}

func TestCredentialBindingMismatchReenrolls(t *testing.T) {
	tests := map[string]func(credentialBinding) credentialBinding{
		"controller": func(binding credentialBinding) credentialBinding {
			binding.ControllerURL += "/replacement"
			return binding
		},
		"agent": func(binding credentialBinding) credentialBinding {
			binding.AgentID = "agent-b"
			return binding
		},
		"server": func(binding credentialBinding) credentialBinding {
			binding.ServerID = "server-b"
			return binding
		},
		"environment": func(binding credentialBinding) credentialBinding {
			binding.Environment = "stage"
			return binding
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			paths := agentruntime.NewPaths(t.TempDir())
			if err := paths.Ensure(); err != nil {
				t.Fatal(err)
			}
			server, calls := newAPMEnrollServer(t)
			defer server.Close()
			api, err := client.New(server.URL, "", true, "agent-a", "agent-api-key")
			if err != nil {
				t.Fatal(err)
			}
			m := New(paths, func(string, ...any) {})
			binding := newCredentialBinding(server.URL, "agent-a", "server-a", "prod")
			first, _, err := m.ensureCredential(context.Background(), api, binding)
			if err != nil {
				t.Fatal(err)
			}
			second, changed, err := m.ensureCredential(context.Background(), api, mutate(binding))
			if err != nil {
				t.Fatal(err)
			}
			if !changed || calls.Load() != 2 || second.Key == first.Key || second.KeyID == first.KeyID {
				t.Fatalf("mismatch did not rotate: changed=%v calls=%d first=%#v second=%#v", changed, calls.Load(), first, second)
			}
		})
	}
}

func TestLegacyCredentialWithoutBindingMetadataIsReenrolled(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	if err := protectCredentialAtomic(paths.APMCredential, []byte("zpi_legacy_unbound_key")); err != nil {
		t.Fatal(err)
	}
	server, calls := newAPMEnrollServer(t)
	defer server.Close()
	api, err := client.New(server.URL, "", true, "agent-a", "agent-api-key")
	if err != nil {
		t.Fatal(err)
	}
	m := New(paths, func(string, ...any) {})
	binding := newCredentialBinding(server.URL, "agent-a", "server-a", "prod")
	credential, changed, err := m.ensureCredential(context.Background(), api, binding)
	if err != nil {
		t.Fatal(err)
	}
	if !changed || calls.Load() != 1 || credential.Key == "zpi_legacy_unbound_key" {
		t.Fatalf("legacy credential was reused: changed=%v calls=%d credential=%#v", changed, calls.Load(), credential)
	}
	meta, err := readCredentialMetadata(paths.APMCredentialMeta)
	if err != nil || !meta.matches(binding, credential.Key) {
		t.Fatalf("replacement metadata is invalid: meta=%#v err=%v", meta, err)
	}
}

func TestCredentialKeyIDTamperingForcesReenrollment(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	server, calls := newAPMEnrollServer(t)
	defer server.Close()
	api, err := client.New(server.URL, "", true, "agent-a", "agent-api-key")
	if err != nil {
		t.Fatal(err)
	}
	m := New(paths, func(string, ...any) {})
	binding := newCredentialBinding(server.URL, "agent-a", "server-a", "prod")
	if _, _, err := m.ensureCredential(context.Background(), api, binding); err != nil {
		t.Fatal(err)
	}
	meta, err := readCredentialMetadata(paths.APMCredentialMeta)
	if err != nil {
		t.Fatal(err)
	}
	meta.KeyID = "tampered-key-id"
	if err := writeCredentialMetadataAtomic(paths.APMCredentialMeta, meta); err != nil {
		t.Fatal(err)
	}
	if _, changed, err := m.ensureCredential(context.Background(), api, binding); err != nil {
		t.Fatal(err)
	} else if !changed || calls.Load() != 2 {
		t.Fatalf("tampered key ID reused: changed=%v calls=%d", changed, calls.Load())
	}
	leftovers, err := filepath.Glob(filepath.Join(paths.StateDir, ".apm-credential.json.tmp-*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(leftovers) != 0 {
		t.Fatalf("atomic metadata write left temporary files: %v", leftovers)
	}
}

func TestRunningGatewayRestartsForCredentialOrConfigChanges(t *testing.T) {
	tests := []struct {
		name              string
		credential        string
		configuration     string
		credentialChanged bool
		configChanged     bool
		wantRestart       bool
	}{
		{name: "unchanged", credential: "credential-a", configuration: "config-a"},
		{name: "credential fingerprint", credential: "credential-b", configuration: "config-a", wantRestart: true},
		{name: "configuration fingerprint", credential: "credential-a", configuration: "config-b", wantRestart: true},
		{name: "rewritten credential", credential: "credential-a", configuration: "config-a", credentialChanged: true, wantRestart: true},
		{name: "rewritten configuration", credential: "credential-a", configuration: "config-a", configChanged: true, wantRestart: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := agentruntime.NewPaths(t.TempDir())
			if err := paths.Ensure(); err != nil {
				t.Fatal(err)
			}
			m := New(paths, func(string, ...any) {})
			m.installDir = t.TempDir()
			cmd, done := startGatewayHelperProcess(t)
			m.cmd = cmd
			m.cmdDone = done
			m.activeCredentialFingerprint = "credential-a"
			m.activeConfigFingerprint = "config-a"

			err := m.ensureStarted(context.Background(), "zpi_test", tt.credential, tt.configuration, tt.credentialChanged, tt.configChanged)
			if tt.wantRestart {
				if err == nil || !strings.Contains(err.Error(), "managed telemetry gateway is not installed") {
					t.Fatalf("restart should continue to a fresh start attempt, got %v", err)
				}
				select {
				case <-done:
				case <-time.After(3 * time.Second):
					t.Fatal("old gateway process was not stopped before restart")
				}
				return
			}
			if err != nil {
				t.Fatalf("unchanged gateway should be reused: %v", err)
			}
			select {
			case <-done:
				t.Fatal("unchanged gateway process was unexpectedly stopped")
			default:
			}
			if err := m.stopAndWait("test_cleanup", 3*time.Second); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestGatewayHelperProcess(t *testing.T) {
	if os.Getenv("ZENPLUS_APM_GATEWAY_HELPER") != "1" {
		return
	}
	for {
		time.Sleep(time.Second)
	}
}

func startGatewayHelperProcess(t *testing.T) (*exec.Cmd, chan struct{}) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestGatewayHelperProcess$")
	cmd.Env = append(os.Environ(), "ZENPLUS_APM_GATEWAY_HELPER=1")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(done)
	}()
	t.Cleanup(func() {
		select {
		case <-done:
			return
		default:
			_ = cmd.Process.Kill()
			select {
			case <-done:
			case <-time.After(3 * time.Second):
			}
		}
	})
	return cmd, done
}

func newAPMEnrollServer(t *testing.T) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/agents/apm/enroll" {
			http.NotFound(w, r)
			return
		}
		var body struct {
			Environment string `json:"environment"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		call := calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(enrollResponse{
			Key:         fmt.Sprintf("zpi_test_credential_%d", call),
			KeyID:       fmt.Sprintf("key-id-%d", call),
			Environment: body.Environment,
			TracesPath:  "/v1/traces",
		})
	}))
	return server, &calls
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
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

func TestDiscoveryCommandLineNeverExportsArgumentValues(t *testing.T) {
	argv := []string{
		`C:\Users\alice\private\worker.exe`,
		`C:\Customers\Acme\private-job.json`,
		`--port=8443`,
		`--token`,
		`super-secret-token`,
		`--unknown=private-value`,
		`https://alice:password@example.invalid/path`,
	}
	got := safeDiscoveryCommandLine("worker.exe", argv, "")
	for _, private := range []string{"alice", "Acme", "8443", "super-secret-token", "private-value", "password", "example.invalid"} {
		if strings.Contains(got, private) {
			t.Fatalf("discovery command shape leaked %q: %q", private, got)
		}
	}
	for _, want := range []string{"worker.exe", "[ARG]", "--port=[VALUE]", "--token=[REDACTED]", "[OPTION]"} {
		if !strings.Contains(got, want) {
			t.Fatalf("discovery command shape %q is missing %q", got, want)
		}
	}
}

func TestManagedJavaAndNodeInstrumentationDetectedFromEnvironment(t *testing.T) {
	cases := []struct {
		runtime     string
		environment []string
		want        bool
	}{
		{"java", []string{`JAVA_TOOL_OPTIONS=-Xmx512m -javaagent:"C:\Program Files\ZenPlus\apm\instrumentation\java\opentelemetry-javaagent.jar"`}, true},
		{"node", []string{`NODE_OPTIONS=--max-old-space-size=512 --require="D:\Observability\apm\instrumentation\node\bootstrap.js"`}, true},
		{"node", []string{`NODE_OPTIONS=--require="D:/Observability/apm/instrumentation/node/bootstrap.js"`}, true},
		{"node", []string{`NODE_OPTIONS=--require="C:\apps\bootstrap.js"`}, false},
		{"java", []string{`JAVA_TOOL_OPTIONS=-Xmx512m`}, false},
	}
	for _, test := range cases {
		if got := managedRuntimeDetected(test.runtime, test.environment); got != test.want {
			t.Fatalf("managedRuntimeDetected(%q, %v)=%t want %t", test.runtime, test.environment, got, test.want)
		}
	}
}

func TestGatewayLogClassificationDetectsExportAndAuthenticationFailures(t *testing.T) {
	tests := []struct {
		name       string
		log        string
		auth       bool
		exportFail bool
	}{
		{"healthy noise", `info\tEverything is ready`, false, false},
		{"transient export", `error\texportercallback\tExporting failed. Will retry the request.`, false, true},
		{"http unauthorized", `error\tPermanent error: HTTP status code: 401 Unauthorized`, true, true},
		{"grpc unauthenticated", `rpc error: code = Unauthenticated desc = invalid key`, true, true},
	}
	for _, test := range tests {
		auth, exportFail := classifyGatewayLogChunk(test.log)
		if auth != test.auth || exportFail != test.exportFail {
			t.Fatalf("%s: classifyGatewayLogChunk()=(%t,%t), want (%t,%t)", test.name, auth, exportFail, test.auth, test.exportFail)
		}
	}
}

func TestGatewayMetricsPopulateRollingCountersAndQueueState(t *testing.T) {
	metrics, ok := parseGatewayMetrics(`# HELP otelcol_exporter_sent_spans spans
otelcol_exporter_sent_spans_total{data_type="traces",exporter="otlp_http/zenplus"} 10
otelcol_exporter_send_failed_spans_total{data_type="traces",exporter="otlp_http/zenplus"} 1
otelcol_exporter_enqueue_failed_spans_total{data_type="traces",exporter="otlp_http/zenplus"} 2
otelcol_exporter_queue_size{data_type="traces",exporter="otlp_http/zenplus"} 3
otelcol_exporter_sent_spans_total{data_type="metrics",exporter="otlp_http/zenplus"} 999
otelcol_exporter_sent_spans_total{data_type="traces",exporter="another"} 999
`)
	if !ok || metrics.Sent != 10 || metrics.Failed != 1 || metrics.Dropped != 2 || metrics.QueueSize != 3 {
		t.Fatalf("parsed gateway metrics = %#v, ok=%t", metrics, ok)
	}

	m := New(agentruntime.NewPaths(t.TempDir()), func(string, ...any) {})
	started := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	m.applyGatewayMetrics(metrics, started, 4096)
	m.applyGatewayMetrics(gatewayMetricSnapshot{
		gatewayMetricTotals: gatewayMetricTotals{Sent: 14, Failed: 3, Dropped: 2, SentKnown: true, FailedKnown: true, DroppedKnown: true},
		QueueSize:           5, QueueSizeKnown: true,
	}, started.Add(30*time.Second), 8192)
	snapshot := m.Snapshot()
	if snapshot.SpansForwarded1M != 14 || snapshot.ExportErrors1M != 3 || snapshot.SpoolDepthSpans != 5 ||
		snapshot.SpoolBytes != 8192 || snapshot.DroppedSpansTotal != 2 {
		t.Fatalf("unexpected APM exporter status after two samples: %#v", snapshot)
	}

	// The first cumulative sample ages out, leaving only the newer delta.
	m.applyGatewayMetrics(gatewayMetricSnapshot{
		gatewayMetricTotals: gatewayMetricTotals{Sent: 20, Failed: 3, Dropped: 2, SentKnown: true, FailedKnown: true, DroppedKnown: true},
		QueueSize:           0, QueueSizeKnown: true,
	}, started.Add(89*time.Second), 0)
	snapshot = m.Snapshot()
	if snapshot.SpansForwarded1M != 10 || snapshot.ExportErrors1M != 2 || snapshot.SpoolDepthSpans != 0 {
		t.Fatalf("unexpected rolling APM exporter status: %#v", snapshot)
	}
}

func TestGatewayLogRotationIsBoundedAndPreservesRecentBackups(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "telemetry-gateway.log")
	for name, contents := range map[string]string{
		path:        "current-log",
		path + ".1": "previous-log",
		path + ".2": "oldest-log",
	} {
		if err := os.WriteFile(name, []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := rotateGatewayLog(path, 4, 2); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("active log still exists after rotation: %v", err)
	}
	for name, want := range map[string]string{
		path + ".1": "current-log",
		path + ".2": "previous-log",
	} {
		got, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Fatalf("%s = %q, want %q", name, got, want)
		}
	}

	if err := os.WriteFile(path, []byte("ok"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := rotateGatewayLog(path, 4, 2); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != "ok" {
		t.Fatalf("small active log was rotated: %q, %v", got, err)
	}

	runtimePath := filepath.Join(dir, "runtime.log")
	writer, err := newRotatingGatewayLog(runtimePath, 4, 2)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write([]byte("abcdefghij")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{
		runtimePath:        "ij",
		runtimePath + ".1": "efgh",
		runtimePath + ".2": "abcd",
	} {
		got, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Fatalf("runtime rotation %s = %q, want %q", name, got, want)
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

func TestCollectorConfigNormalizesLegacyIPv6Loopback(t *testing.T) {
	paths := agentruntime.NewPaths(t.TempDir())
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	m := New(paths, func(string, ...any) {})
	cfg := config.Default()
	cfg.APM.BindAddress = "::1"
	if _, _, err := m.ensureConfig(cfg, "agent-id", "server-id"); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(paths.APMConfig)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "::1") || !strings.Contains(string(b), "127.0.0.1:4318") {
		t.Fatalf("legacy IPv6 APM bind was not normalized:\n%s", b)
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
