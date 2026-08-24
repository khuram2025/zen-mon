package collectors

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSafeProcessCommandLineNeverExportsArgumentValues(t *testing.T) {
	argv := []string{
		`C:\Users\alice\private\worker.exe`,
		`C:\Customers\Acme\private-job.json`,
		`--port=8443`,
		`--token`,
		`super-secret-token`,
		`--unknown=private-value`,
		`https://alice:password@example.invalid/path`,
	}
	got := safeProcessCommandLine("worker.exe", argv)
	for _, private := range []string{"alice", "Acme", "8443", "super-secret-token", "private-value", "password", "example.invalid"} {
		if strings.Contains(got, private) {
			t.Fatalf("command shape leaked %q: %q", private, got)
		}
	}
	for _, want := range []string{"worker.exe", "[ARG]", "--port=[VALUE]", "--token=[REDACTED]", "[OPTION]"} {
		if !strings.Contains(got, want) {
			t.Fatalf("command shape %q is missing %q", got, want)
		}
	}
}

func TestSafeProcessCommandLineIsBounded(t *testing.T) {
	argv := []string{"worker.exe"}
	for i := 0; i < maxProcessCommandArgs+50; i++ {
		argv = append(argv, strings.Repeat("private", 100))
	}
	got := safeProcessCommandLine("worker.exe", argv)
	if len(got) > maxProcessCommandLineBytes {
		t.Fatalf("command shape length = %d, max %d", len(got), maxProcessCommandLineBytes)
	}
	if count := strings.Count(got, "[ARG]"); count != maxProcessCommandArgs {
		t.Fatalf("argument marker count = %d, want %d", count, maxProcessCommandArgs)
	}
}

func TestMissingWatchedProcessesIsNormalizedAndDeterministic(t *testing.T) {
	samples := []processSample{{Name: "SQLSERVR.EXE"}, {Name: "w3wp.exe"}}
	watchlist := []string{" Redis.EXE ", "sqlservr.exe", "api.exe", "redis.exe", "W3WP.EXE"}
	got := missingWatchedProcesses(watchlist, samples)
	want := []string{"api.exe", "redis.exe"}
	if len(got) != len(want) {
		t.Fatalf("missing watchlist = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("missing watchlist = %v, want %v", got, want)
		}
	}
}

func TestProcessStartedAtUsesUTC(t *testing.T) {
	const createdMs int64 = 1_725_000_000_123
	got := processStartedAt(createdMs)
	want := time.UnixMilli(createdMs).UTC().Format(time.RFC3339Nano)
	if got != want {
		t.Fatalf("started_at = %q, want %q", got, want)
	}
	if got := processStartedAt(0); got != "" {
		t.Fatalf("unknown start time = %q, want empty", got)
	}
}

func TestCollectProcessesEmitsAbsentWatchlistState(t *testing.T) {
	procState.Lock()
	previousAt, previousCPU := procState.at, procState.cpu
	procState.at, procState.cpu = time.Time{}, map[procCPUKey]float64{}
	procState.Unlock()
	t.Cleanup(func() {
		procState.Lock()
		procState.at, procState.cpu = previousAt, previousCPU
		procState.Unlock()
	})

	const absent = "zenplus-process-that-must-not-exist-8ec848bb.exe"
	executable, err := os.Executable()
	if err != nil {
		t.Fatalf("resolve test executable: %v", err)
	}
	currentName := filepath.Base(executable)
	var metrics []map[string]any
	errs := map[string]string{}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	collectProcesses(ctx, 1, []string{currentName, absent}, func(kind string, data map[string]any) {
		if kind == "process" {
			metrics = append(metrics, data)
		}
	}, errs)
	if err := errs["process"]; err != "" {
		t.Fatalf("process collection failed: %s", err)
	}
	foundAbsent := false
	foundCurrent := false
	for _, metric := range metrics {
		if metric["process_name"] == absent {
			if metric["state"] != "not_running" || metric["running"] != false || metric["pid"] != 0 {
				t.Fatalf("absent process state is not deterministic: %#v", metric)
			}
			foundAbsent = true
		}
		if metric["pid"] == os.Getpid() {
			if metric["started_at"] == nil {
				t.Fatalf("current process start time missing: %#v", metric)
			}
			commandShape, ok := metric["cmdline"].(string)
			if !ok || !strings.HasPrefix(strings.ToLower(commandShape), strings.ToLower(currentName)) {
				t.Fatalf("current process command shape missing: %#v", metric)
			}
			foundCurrent = true
		}
	}
	if !foundAbsent {
		t.Fatalf("absent watchlist state was not emitted: %#v", metrics)
	}
	if !foundCurrent {
		t.Fatalf("current watchlisted process was not enriched: %#v", metrics)
	}
}
