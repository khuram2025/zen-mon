package agent

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"zenplus-agent/internal/client"
)

func writeAttempt(t *testing.T, dir string, a updateAttempt) {
	t.Helper()
	tr := &updateTracker{statePath: filepath.Join(dir, "state", "update-attempt.json")}
	tr.save(a)
}

func TestPreviousAttemptOutcomeAfterRestart(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "logs", "update-1.15.0.log")
	if err := os.MkdirAll(filepath.Dir(logPath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(logPath, []byte("Installing ZenPlus Agent 1.15.0\r\nSetup failed: service did not start\r\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	// The new version is running: the attempt succeeded and is cleared.
	writeAttempt(t, dir, updateAttempt{TargetVersion: "1.15.0", FromVersion: "1.14.0", StartedAt: time.Now(), SetupLaunched: true})
	tr := newUpdateTracker(filepath.Join(dir, "state"), filepath.Join(dir, "logs"), "1.15.0")
	if s := tr.Status(); s == nil || s.State != "succeeded" || s.FromVersion != "1.14.0" {
		t.Fatalf("status after successful upgrade = %+v", s)
	}
	if _, ok := tr.load(); ok {
		t.Fatal("successful attempt record was not removed")
	}

	// Still the old version after Setup ran: it rolled back.
	writeAttempt(t, dir, updateAttempt{TargetVersion: "1.15.0", FromVersion: "1.14.0", StartedAt: time.Now(), SetupLaunched: true, LogPath: logPath})
	tr = newUpdateTracker(filepath.Join(dir, "state"), filepath.Join(dir, "logs"), "1.14.0")
	s := tr.Status()
	if s == nil || s.State != "failed" || !strings.Contains(s.Error, "service did not start") {
		t.Fatalf("rolled-back upgrade status = %+v", s)
	}
}

func TestRequestRefusesDowngradeAndRespectsCooldown(t *testing.T) {
	dir := t.TempDir()
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		http.Error(w, `{"detail":"No full package published"}`, http.StatusNotFound)
	}))
	defer server.Close()
	api, err := client.New(server.URL, "", true, "agent", "key")
	if err != nil {
		t.Fatal(err)
	}
	tr := newUpdateTracker(filepath.Join(dir, "state"), filepath.Join(dir, "logs"), "1.14.0")
	logf := func(string, ...any) {}

	tr.Request(context.Background(), api, "stable", "1.13.0", logf)
	if s := tr.Status(); s == nil || s.State != "failed" || !strings.Contains(s.Error, "downgrade") {
		t.Fatalf("downgrade status = %+v", s)
	}

	tr.Request(context.Background(), api, "stable", "9.9.9", logf)
	waitFor(t, func() bool { s := tr.Status(); return s != nil && s.State == "failed" && s.TargetVersion == "9.9.9" })
	if !strings.Contains(tr.Status().Error, "not published") {
		t.Fatalf("unpublished version error = %q", tr.Status().Error)
	}
	first := requests.Load()

	// The same target within the cooldown is not retried on every heartbeat.
	tr.Request(context.Background(), api, "stable", "9.9.9", logf)
	time.Sleep(100 * time.Millisecond)
	if requests.Load() != first {
		t.Fatal("failed upgrade was retried within the cooldown")
	}

	// It is retried once the cooldown has passed.
	tr.now = func() time.Time { return time.Now().UTC().Add(updateRetryCooldown + time.Minute) }
	tr.Request(context.Background(), api, "stable", "9.9.9", logf)
	waitFor(t, func() bool { return requests.Load() > first })
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}
