package agent

import (
	"context"
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
