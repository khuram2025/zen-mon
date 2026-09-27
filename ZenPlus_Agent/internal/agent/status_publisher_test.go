package agent

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
)

func TestStatusPublisherWritesOnChangeAndRefreshesPeriodically(t *testing.T) {
	dir := t.TempDir()
	paths := runtime.NewPaths(dir)
	if err := os.MkdirAll(filepath.Dir(paths.StatusFile), 0o700); err != nil {
		t.Fatal(err)
	}
	clock := time.Date(2026, 9, 27, 10, 0, 0, 0, time.UTC)
	p := &statusPublisher{now: func() time.Time { return clock }}
	cfg := config.Default()
	cfg.DataDir = dir
	status := model.Status{AgentVersion: "1.13.0"}
	writes := func() time.Time {
		info, err := os.Stat(paths.StatusFile)
		if err != nil {
			t.Fatal(err)
		}
		return info.ModTime()
	}

	p.publish(paths, cfg, identity.Identity{}, status)
	first := writes()
	if err := os.Chtimes(paths.StatusFile, first.Add(-time.Hour), first.Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	marker := writes()

	clock = clock.Add(5 * time.Second)
	p.publish(paths, cfg, identity.Identity{}, status)
	if !writes().Equal(marker) {
		t.Fatal("unchanged status was rewritten within the refresh interval")
	}

	status.LastUploadError = "controller unreachable"
	p.publish(paths, cfg, identity.Identity{}, status)
	if writes().Equal(marker) {
		t.Fatal("changed status was not written")
	}
	if err := os.Chtimes(paths.StatusFile, marker, marker); err != nil {
		t.Fatal(err)
	}

	clock = clock.Add(statusRefreshInterval)
	p.publish(paths, cfg, identity.Identity{}, status)
	if writes().Equal(marker) {
		t.Fatal("unchanged status was not refreshed after the refresh interval")
	}
}
