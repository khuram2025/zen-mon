package configpoller

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

func TestCachedControllerPolicySurvivesAgentRestart(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/config" {
			t.Fatalf("unexpected request path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("ETag", "cfg-v7")
		_, _ = w.Write([]byte(`{
			"config_version":7,
			"etag":"cfg-v7",
			"metric_interval_s":17,
			"upload_interval_s":19,
			"feature_flags":{"cpu":false,"memory":true},
			"update_ring":"stable"
		}`))
	}))
	defer server.Close()

	current := config.Default()
	current.ControllerURL = server.URL
	current.DataDir = t.TempDir()
	api, err := client.New(server.URL, "", true, "agent-1", "key-1")
	if err != nil {
		t.Fatal(err)
	}
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	if err := os.MkdirAll(filepath.Dir(cachePath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cachePath, []byte(`{"config_version":6,"metric_interval_s":60}`), 0o600); err != nil {
		t.Fatal(err)
	}
	next, changed, _, err := New(api, cachePath, "machine-1", "agent-1", "server-1").Poll(context.Background(), current)
	if err != nil {
		t.Fatal(err)
	}
	if !changed || next.CollectIntervalSeconds != 17 || next.Collectors.CPU.Enabled {
		t.Fatalf("live policy was not applied: %+v", next)
	}

	restored, ok, err := Restore(cachePath, current, "machine-1", "agent-1", "server-1")
	if err != nil {
		t.Fatal(err)
	}
	if !ok {
		t.Fatal("cached policy was not restored")
	}
	if restored.ConfigVersion != 7 || restored.ConfigETag != "cfg-v7" ||
		restored.CollectIntervalSeconds != 17 || restored.UploadIntervalSeconds != 19 ||
		restored.Collectors.CPU.Enabled || !restored.Collectors.Memory.Enabled {
		t.Fatalf("restored policy differs from live policy: %+v", restored)
	}
	if restored.ControllerURL != current.ControllerURL || restored.DataDir != current.DataDir {
		t.Fatalf("restore changed local connection/state settings: %+v", restored)
	}
	var persisted cacheEnvelope
	cacheBytes, err := os.ReadFile(cachePath)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(cacheBytes, &persisted); err != nil {
		t.Fatal(err)
	}
	if persisted.Version != configCacheVersion || persisted.AgentUID != "machine-1" || len(persisted.Response) == 0 {
		t.Fatalf("live poll did not persist a machine-bound v2 cache: %+v", persisted)
	}
}

func TestPollDoesNotCommitETagFromRejectedResponse(t *testing.T) {
	var ifNoneMatch []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ifNoneMatch = append(ifNoneMatch, r.Header.Get("If-None-Match"))
		w.Header().Set("ETag", "cfg-rejected")
		_, _ = w.Write([]byte(`{"config_version":`))
	}))
	defer server.Close()

	current := config.Default()
	current.ControllerURL = server.URL
	current.ConfigETag = "cfg-current"
	api, err := client.New(server.URL, "", true, "agent-1", "key-1")
	if err != nil {
		t.Fatal(err)
	}
	poller := New(api, "", "machine-1", "agent-1", "server-1")
	for i := 0; i < 2; i++ {
		if _, changed, _, err := poller.Poll(context.Background(), current); err == nil || changed {
			t.Fatalf("poll %d accepted an invalid controller response: changed=%v err=%v", i+1, changed, err)
		}
	}
	if want := []string{"cfg-current", "cfg-current"}; !reflect.DeepEqual(ifNoneMatch, want) {
		t.Fatalf("conditional request ETags = %v, want %v", ifNoneMatch, want)
	}
	if poller.etag != "cfg-current" {
		t.Fatalf("rejected response advanced poller ETag to %q", poller.etag)
	}
}

func TestPollCacheFailureDoesNotCommitPolicyOrETag(t *testing.T) {
	var ifNoneMatch []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ifNoneMatch = append(ifNoneMatch, r.Header.Get("If-None-Match"))
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("ETag", "cfg-new")
		_, _ = w.Write([]byte(`{"config_version":2,"etag":"cfg-new","metric_interval_s":17}`))
	}))
	defer server.Close()

	current := config.Default()
	current.ControllerURL = server.URL
	current.DataDir = t.TempDir()
	current.ConfigETag = "cfg-current"
	api, err := client.New(server.URL, "", true, "agent-1", "key-1")
	if err != nil {
		t.Fatal(err)
	}
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	originalReplace := replaceCacheFile
	replaceCacheFile = func(_, _ string) error { return errors.New("injected replace failure") }
	t.Cleanup(func() { replaceCacheFile = originalReplace })

	poller := New(api, cachePath, "machine-1", "agent-1", "server-1")
	for i := 0; i < 2; i++ {
		next, changed, _, err := poller.Poll(context.Background(), current)
		if err == nil || changed {
			t.Fatalf("poll %d ignored cache failure: changed=%v err=%v", i+1, changed, err)
		}
		if !reflect.DeepEqual(next, current) {
			t.Fatalf("poll %d applied policy that could not be cached", i+1)
		}
	}
	if want := []string{"cfg-current", "cfg-current"}; !reflect.DeepEqual(ifNoneMatch, want) {
		t.Fatalf("conditional request ETags = %v, want %v", ifNoneMatch, want)
	}
	if poller.etag != "cfg-current" {
		t.Fatalf("failed cache write advanced poller ETag to %q", poller.etag)
	}
}

func TestCachedControllerPolicyIsBoundToItsAppliance(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance-a.example"
	current.DataDir = t.TempDir()
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	response := []byte(`{"config_version":2,"etag":"cfg-v2","metric_interval_s":30}`)
	if err := writeCache(cachePath, current.ControllerURL, "machine-1", "agent-1", "server-1", "cfg-v2", response); err != nil {
		t.Fatal(err)
	}

	other := current
	other.ControllerURL = "https://appliance-b.example"
	if _, ok, err := Restore(cachePath, other, "machine-1", "agent-1", "server-1"); err == nil || ok {
		t.Fatalf("cache from another appliance was accepted: ok=%v err=%v", ok, err)
	}
}

func TestCachedControllerPolicyIsBoundToItsMachineIdentity(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance.example"
	current.DataDir = t.TempDir()
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	response := []byte(`{"config_version":2,"etag":"cfg-v2","metric_interval_s":30}`)
	if err := writeCache(cachePath, current.ControllerURL, "machine-1", "agent-1", "server-1", "cfg-v2", response); err != nil {
		t.Fatal(err)
	}

	if _, ok, err := Restore(cachePath, current, "machine-2", "agent-2", "server-2"); err == nil || ok {
		t.Fatalf("cache from another agent identity was accepted: ok=%v err=%v", ok, err)
	}
}

func TestCachedControllerPolicySurvivesApplianceIDRotation(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance.example"
	current.DataDir = t.TempDir()
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	response := []byte(`{"config_version":2,"etag":"cfg-v2","metric_interval_s":23}`)
	if err := writeCache(cachePath, current.ControllerURL, "machine-1", "agent-old", "server-old", "cfg-v2", response); err != nil {
		t.Fatal(err)
	}

	restored, ok, err := Restore(cachePath, current, "machine-1", "agent-new", "server-new")
	if err != nil || !ok {
		t.Fatalf("same machine could not restore after appliance ID rotation: ok=%v err=%v", ok, err)
	}
	if restored.CollectIntervalSeconds != 23 {
		t.Fatalf("restored interval = %d, want 23", restored.CollectIntervalSeconds)
	}
}

func TestRestoreReadsLegacyV1BoundEnvelope(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance.example"
	current.DataDir = t.TempDir()
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	legacy, err := json.Marshal(cacheEnvelope{
		Version: legacyCacheEnvelopeVersion, ControllerURL: current.ControllerURL,
		AgentID: "agent-1", ServerID: "server-1", ETag: "cfg-v1",
		Response: []byte(`{"config_version":1,"metric_interval_s":29}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(cachePath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cachePath, legacy, 0o600); err != nil {
		t.Fatal(err)
	}

	restored, ok, err := Restore(cachePath, current, "machine-1", "agent-1", "server-1")
	if err != nil || !ok || restored.CollectIntervalSeconds != 29 {
		t.Fatalf("v1 envelope was not restored: ok=%v err=%v config=%+v", ok, err, restored)
	}
}

func TestRestoreLegacyRawCacheDuringControllerOutage(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance.example"
	current.DataDir = t.TempDir()
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	raw := []byte(`{
		"config_version":12,
		"etag":"cfg-v12",
		"metric_interval_s":17,
		"upload_interval_s":19,
		"feature_flags":{"cpu":false,"memory":true}
	}`)
	if err := os.MkdirAll(filepath.Dir(cachePath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cachePath, raw, 0o600); err != nil {
		t.Fatal(err)
	}

	restored, ok, err := Restore(cachePath, current, "machine-1", "agent-new", "server-new")
	if err != nil || !ok {
		t.Fatalf("legacy raw policy was not restored: ok=%v err=%v", ok, err)
	}
	if restored.ConfigVersion != 12 || restored.ConfigETag != "cfg-v12" ||
		restored.CollectIntervalSeconds != 17 || restored.UploadIntervalSeconds != 19 ||
		restored.Collectors.CPU.Enabled || !restored.Collectors.Memory.Enabled {
		t.Fatalf("legacy raw policy changed during restore: %+v", restored)
	}
	unchanged, err := os.ReadFile(cachePath)
	if err != nil || !reflect.DeepEqual(unchanged, raw) {
		t.Fatalf("legacy cache was rewritten before a successful poll: err=%v", err)
	}
}

func TestRestoreRejectsUnsignedLegacyRawCacheWhenSignaturesRequired(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance.example"
	current.DataDir = t.TempDir()
	current.Security.RequireSignedConfig = true
	current.Security.ConfigPublicKey = base64.StdEncoding.EncodeToString(make([]byte, ed25519.PublicKeySize))
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	if err := os.MkdirAll(filepath.Dir(cachePath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cachePath, []byte(`{"config_version":2,"metric_interval_s":15}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := Restore(cachePath, current, "machine-1", "agent-1", "server-1"); err == nil || ok {
		t.Fatalf("unsigned legacy cache was accepted: ok=%v err=%v", ok, err)
	}
}

func TestRestoreLegacyFullConfigCannotChangeLocalBindings(t *testing.T) {
	for name, mutate := range map[string]func(*config.Config){
		"controller": func(cfg *config.Config) { cfg.ControllerURL = "https://other.example" },
		"data_dir":   func(cfg *config.Config) { cfg.DataDir = filepath.Join(t.TempDir(), "other") },
	} {
		t.Run(name, func(t *testing.T) {
			current := config.Default()
			current.ControllerURL = "https://appliance.example"
			current.DataDir = t.TempDir()
			legacy := current
			mutate(&legacy)
			body, err := json.Marshal(legacy)
			if err != nil {
				t.Fatal(err)
			}
			cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
			if err := os.MkdirAll(filepath.Dir(cachePath), 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(cachePath, body, 0o600); err != nil {
				t.Fatal(err)
			}
			if _, ok, err := Restore(cachePath, current, "machine-1", "agent-1", "server-1"); err == nil || ok {
				t.Fatalf("legacy cache changed %s binding: ok=%v err=%v", name, ok, err)
			}
		})
	}
}

func TestMalformedVersionedEnvelopeNeverFallsBackToLegacy(t *testing.T) {
	current := config.Default()
	current.ControllerURL = "https://appliance.example"
	current.DataDir = t.TempDir()
	cachePath := filepath.Join(current.DataDir, "config", "last-known-good.yaml")
	body := []byte(`{"version":2,"controller_url":"https://appliance.example","agent_uid":"machine-1","agent_id":"agent-1","server_id":"server-1"}`)
	if err := os.MkdirAll(filepath.Dir(cachePath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cachePath, body, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := Restore(cachePath, current, "machine-1", "agent-1", "server-1"); err == nil || ok {
		t.Fatalf("malformed envelope fell back to legacy parsing: ok=%v err=%v", ok, err)
	}
}

func TestWriteCacheFailurePreservesPreviousPolicy(t *testing.T) {
	dir := t.TempDir()
	cachePath := filepath.Join(dir, "last-known-good.yaml")
	previous := []byte(`{"config_version":1,"metric_interval_s":31}`)
	if err := os.WriteFile(cachePath, previous, 0o600); err != nil {
		t.Fatal(err)
	}
	originalReplace := replaceCacheFile
	replaceCacheFile = func(_, _ string) error { return errors.New("injected replace failure") }
	t.Cleanup(func() { replaceCacheFile = originalReplace })

	err := writeCache(cachePath, "https://appliance.example", "machine-1", "agent-1", "server-1", "cfg-v2", []byte(`{"config_version":2}`))
	if err == nil {
		t.Fatal("cache replacement unexpectedly succeeded")
	}
	got, readErr := os.ReadFile(cachePath)
	if readErr != nil || !reflect.DeepEqual(got, previous) {
		t.Fatalf("failed replacement lost the prior cache: data=%q err=%v", got, readErr)
	}
}

func TestWriteCacheAtomicallyReplacesExistingPolicy(t *testing.T) {
	cachePath := filepath.Join(t.TempDir(), "last-known-good.yaml")
	if err := os.WriteFile(cachePath, []byte(`{"legacy":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	response := []byte(`{"config_version":2,"metric_interval_s":37}`)
	if err := writeCache(cachePath, "https://appliance.example", "machine-1", "agent-1", "server-1", "cfg-v2", response); err != nil {
		t.Fatal(err)
	}
	var cached cacheEnvelope
	body, err := os.ReadFile(cachePath)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(body, &cached); err != nil {
		t.Fatal(err)
	}
	if cached.Version != configCacheVersion || cached.AgentUID != "machine-1" || !reflect.DeepEqual([]byte(cached.Response), response) {
		t.Fatalf("existing cache was not atomically replaced: %+v", cached)
	}
}

func TestApplyControllerConfigPropagatesMetricInterval(t *testing.T) {
	current := config.Default()
	inventoryInterval := current.Collectors.Inventory.IntervalSeconds

	next := applyControllerConfig(current, model.AgentConfigResponse{
		MetricIntervalS: 30,
	})

	if next.CollectIntervalSeconds != 30 {
		t.Fatalf("collect interval = %d, want 30", next.CollectIntervalSeconds)
	}
	intervals := map[string]int{
		"cpu":        next.Collectors.CPU.IntervalSeconds,
		"memory":     next.Collectors.Memory.IntervalSeconds,
		"filesystem": next.Collectors.Filesystem.IntervalSeconds,
		"disk_io":    next.Collectors.DiskIO.IntervalSeconds,
		"network":    next.Collectors.Network.IntervalSeconds,
		"processes":  next.Collectors.Processes.IntervalSeconds,
		"services":   next.Collectors.Services.IntervalSeconds,
		"event_log":  next.Collectors.EventLog.IntervalSeconds,
	}
	for name, interval := range intervals {
		if interval != 30 {
			t.Errorf("%s interval = %d, want 30", name, interval)
		}
	}
	if next.Collectors.Inventory.IntervalSeconds != inventoryInterval {
		t.Fatalf("inventory interval = %d, want unchanged %d", next.Collectors.Inventory.IntervalSeconds, inventoryInterval)
	}
}

func TestApplyControllerConfigExplicitEmptyListsClearLocalValues(t *testing.T) {
	current := config.Default()
	current.Collectors.Processes.Watchlist = []string{"legacy.exe"}
	current.Collectors.Services.Watchlist = []string{"LegacyService"}
	current.Collectors.EventLog.Channels = []string{"System"}
	current.Collectors.EventLog.Levels = []string{"Error"}
	legacyFilters := []config.EventLogFilter{{Channel: "Legacy", Levels: []string{"Warning"}, IDs: []int{7}}}
	current.Collectors.EventLog.Filters = &legacyFilters
	current.DiskIgnore = []string{"legacy-volume"}
	current.NetworkIgnore = []string{"legacy-nic"}

	next := applyControllerConfig(current, model.AgentConfigResponse{
		ServiceWatchlist: []string{},
		ProcessWatchlist: []string{},
		EventLogFilters:  []model.EventLogFilter{},
		DiskIgnore:       []string{},
		NetworkIgnore:    []string{},
	})

	if len(next.Collectors.Services.Watchlist) != 0 {
		t.Errorf("service watchlist was not cleared: %v", next.Collectors.Services.Watchlist)
	}
	if len(next.Collectors.Processes.Watchlist) != 0 {
		t.Errorf("process watchlist was not cleared: %v", next.Collectors.Processes.Watchlist)
	}
	if len(next.Collectors.EventLog.Channels) != 0 || len(next.Collectors.EventLog.Levels) != 0 {
		t.Errorf("event-log filters were not cleared: channels=%v levels=%v", next.Collectors.EventLog.Channels, next.Collectors.EventLog.Levels)
	}
	if next.Collectors.EventLog.Filters == nil || len(*next.Collectors.EventLog.Filters) != 0 {
		t.Errorf("authoritative event-log filter list was not explicitly cleared: %#v", next.Collectors.EventLog.Filters)
	}
	if len(next.DiskIgnore) != 0 {
		t.Errorf("disk ignore list was not cleared: %v", next.DiskIgnore)
	}
	if len(next.NetworkIgnore) != 0 {
		t.Errorf("network ignore list was not cleared: %v", next.NetworkIgnore)
	}
}

func TestApplyControllerConfigAbsentListsPreserveLocalValues(t *testing.T) {
	current := config.Default()
	current.Collectors.Processes.Watchlist = []string{"local.exe"}
	current.Collectors.Services.Watchlist = []string{"LocalService"}
	current.Collectors.EventLog.Channels = []string{"System"}
	current.Collectors.EventLog.Levels = []string{"Error"}
	localFilters := []config.EventLogFilter{{Channel: "Security", Levels: []string{"Critical"}, IDs: []int{4625}}}
	current.Collectors.EventLog.Filters = &localFilters
	current.DiskIgnore = []string{"local-volume"}
	current.NetworkIgnore = []string{"local-nic"}

	next := applyControllerConfig(current, model.AgentConfigResponse{})

	if !reflect.DeepEqual(next.Collectors.Processes.Watchlist, current.Collectors.Processes.Watchlist) ||
		!reflect.DeepEqual(next.Collectors.Services.Watchlist, current.Collectors.Services.Watchlist) ||
		!reflect.DeepEqual(next.Collectors.EventLog.Filters, current.Collectors.EventLog.Filters) ||
		!reflect.DeepEqual(next.Collectors.EventLog.Channels, current.Collectors.EventLog.Channels) ||
		!reflect.DeepEqual(next.Collectors.EventLog.Levels, current.Collectors.EventLog.Levels) ||
		!reflect.DeepEqual(next.DiskIgnore, current.DiskIgnore) ||
		!reflect.DeepEqual(next.NetworkIgnore, current.NetworkIgnore) {
		t.Fatalf("absent controller lists changed local values: %#v", next)
	}
}

func TestApplyControllerConfigPreservesEventFiltersExactly(t *testing.T) {
	current := config.Default()
	remote := model.AgentConfigResponse{EventLogFilters: []model.EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}, IDs: []int{41, 6008}},
		{Channel: "Application", Levels: []string{"Warning", "Critical"}, IDs: []int{1000}},
		{Channel: "System", Levels: []string{"Error"}},
	}}
	next := applyControllerConfig(current, remote)

	want := []config.EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}, IDs: []int{41, 6008}},
		{Channel: "Application", Levels: []string{"Warning", "Critical"}, IDs: []int{1000}},
		{Channel: "System", Levels: []string{"Error"}},
	}
	if next.Collectors.EventLog.Filters == nil || !reflect.DeepEqual(*next.Collectors.EventLog.Filters, want) {
		t.Fatalf("filters = %#v, want %#v", next.Collectors.EventLog.Filters, want)
	}
	if next.Collectors.EventLog.Channels != nil || next.Collectors.EventLog.Levels != nil {
		t.Fatalf("legacy filter projection was retained: channels=%v levels=%v", next.Collectors.EventLog.Channels, next.Collectors.EventLog.Levels)
	}

	remote.EventLogFilters[0].Levels[0] = "Critical"
	remote.EventLogFilters[0].IDs[0] = 999
	if !reflect.DeepEqual(*next.Collectors.EventLog.Filters, want) {
		t.Fatal("local filters alias the controller response")
	}
}
