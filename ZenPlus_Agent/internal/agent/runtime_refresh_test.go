package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/configpoller"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/model"
	agentruntime "zenplus-agent/internal/runtime"
	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

func TestControllerAndProfileSettingsReloadWithoutRestart(t *testing.T) {
	controller := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/enroll" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(model.EnrollmentResponse{AgentID: "00000000-0000-4000-8000-000000000001", AuthorizationState: "pending"})
	}))
	defer controller.Close()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	paths := agentruntime.NewPaths(cfg.DataDir)
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(cfg.DataDir, "config", "agent.yaml")
	localHash := localSettingsFingerprint(cfg)
	next := cfg
	if err := config.ApplyProfile(&next, "apm"); err != nil {
		t.Fatal(err)
	}
	if localHash == localSettingsFingerprint(next) {
		t.Fatal("profile change with APM still enabled was missed")
	}
	next.ControllerURL = controller.URL
	if err := config.Save(configPath, next); err != nil {
		t.Fatal(err)
	}
	store, err := spool.Open(paths.SpoolDB)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	var up *uploader.Uploader
	var poller *configpoller.Poller
	enrollment := enroll.Result{}
	status := model.Status{}
	authStamp := authFileStamp(paths)
	if !refreshLocalRuntime(context.Background(), configPath, &cfg, paths, store, &enrollment, &up, &poller, &status, &localHash, &authStamp, func(string, ...any) {}) {
		t.Fatalf("refresh failed: %s", status.LastConfigError)
	}
	if cfg.ControllerURL != controller.URL || status.ControllerURL != controller.URL || cfg.APM.Profile != "apm" || cfg.Collectors.CPU.Enabled {
		t.Fatal("running configuration did not adopt controller/profile")
	}
}

func TestFailedLocalEnrollmentRefreshConsumesAuthFileChange(t *testing.T) {
	var requests atomic.Int32
	controller := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/enroll" {
			t.Fatalf("unexpected request path %q", r.URL.Path)
		}
		requests.Add(1)
		http.Error(w, `{"detail":"Pending registration belongs to another agent installation"}`, http.StatusConflict)
	}))
	defer controller.Close()

	dataDir := t.TempDir()
	configPath := filepath.Join(dataDir, "config", "agent.yaml")
	cfg := config.Default()
	cfg.ControllerURL = controller.URL
	cfg.DataDir = dataDir
	if err := config.Save(configPath, cfg); err != nil {
		t.Fatal(err)
	}
	paths := agentruntime.NewPaths(dataDir)
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	store, err := spool.Open(paths.SpoolDB)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	var up *uploader.Uploader
	var poller *configpoller.Poller
	enrollment := enroll.Result{}
	status := model.Status{}
	localHash := "force-settings-refresh"
	authStamp := authFileStamp(paths)
	refresh := func() bool {
		return refreshLocalRuntime(
			context.Background(), configPath, &cfg, paths, store,
			&enrollment, &up, &poller, &status, &localHash, &authStamp,
			func(string, ...any) {},
		)
	}

	if refresh() {
		t.Fatal("failed enrollment unexpectedly rebuilt the runtime client")
	}
	if requests.Load() != 1 {
		t.Fatalf("enrollment requests = %d, want 1", requests.Load())
	}
	if authStamp != authFileStamp(paths) {
		t.Fatal("failed enrollment did not consume its pending-secret file change")
	}
	if refresh() {
		t.Fatal("unchanged auth state unexpectedly rebuilt the runtime client")
	}
	if requests.Load() != 1 {
		t.Fatalf("five-second file watcher bypassed enrollment backoff; requests=%d", requests.Load())
	}
}
