//go:build linux

package collectors

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func withSystemdSeams(t *testing.T, booted bool, run commandRunner) {
	t.Helper()
	prevBooted, prevRun, prevLookup := systemdBooted, runSystemctl, lookupExecPath
	systemdBooted = func() bool { return booted }
	runSystemctl = run
	lookupExecPath = func(string) (string, error) { return "/usr/bin/systemctl", nil }
	t.Cleanup(func() { systemdBooted, runSystemctl, lookupExecPath = prevBooted, prevRun, prevLookup })
	rateState.Lock()
	rateState.services = map[string]string{}
	rateState.Unlock()
}

func TestHostWithoutSystemdSendsNoServicesAndNoError(t *testing.T) {
	withSystemdSeams(t, false, func(context.Context, string, ...string) ([]byte, error) {
		t.Fatal("systemctl must not run on a host without systemd")
		return nil, nil
	})
	var added []string
	errs, inv := map[string]string{}, map[string]any{}
	collectServices(context.Background(), nil, func(kind string, _ map[string]any) { added = append(added, kind) }, errs, inv)
	if len(added) != 0 || len(errs) != 0 || inv["services"] != nil {
		t.Fatalf("added=%v errs=%v inv=%v", added, errs, inv)
	}
}

func TestLinuxServicesReachInventoryAndServiceState(t *testing.T) {
	withSystemdSeams(t, true, func(_ context.Context, _ string, args ...string) ([]byte, error) {
		if args[0] == "list-units" {
			return []byte(listUnitsFixture), nil
		}
		return []byte(showFixture), nil
	})
	rows := map[string]map[string]any{}
	errs, inv := map[string]string{}, map[string]any{}
	collectServices(context.Background(), []string{"nginx", "redis"}, func(kind string, data map[string]any) {
		if kind == "service_state" {
			rows[data["service_name"].(string)] = data
		}
	}, errs, inv)
	if len(errs) != 0 {
		t.Fatalf("errs = %v", errs)
	}
	if rows["nginx"]["state"] != "failed" || rows["cron"]["state"] != "running" || rows["cron"]["start_mode"] != "auto" {
		t.Fatalf("rows = %v", rows)
	}
	// A watched service that systemd does not know is reported as missing.
	if rows["redis"]["state"] != "not_found" {
		t.Fatalf("watched missing service = %v", rows["redis"])
	}
	if items, ok := inv["services"].([]map[string]any); !ok || len(items) != 7 {
		t.Fatalf("inventory = %v", inv["services"])
	}
}

func TestSystemctlErrorIsReported(t *testing.T) {
	withSystemdSeams(t, true, func(context.Context, string, ...string) ([]byte, error) {
		return nil, errors.New("Failed to connect to bus")
	})
	errs := map[string]string{}
	collectServices(context.Background(), nil, func(string, map[string]any) {}, errs, map[string]any{})
	if errs["service_state"] == "" {
		t.Fatalf("errs = %v", errs)
	}
}

func TestReadBootInfoFromProc(t *testing.T) {
	dir := t.TempDir()
	stat, bootID := filepath.Join(dir, "stat"), filepath.Join(dir, "boot_id")
	_ = os.WriteFile(stat, []byte("cpu 1 2 3\nbtime 1795083707\n"), 0o600)
	_ = os.WriteFile(bootID, []byte("2f4c1f9e-0000-4000-8000-000000000001\n"), 0o600)
	prevStat, prevID := procStat, procBootID
	procStat, procBootID = stat, bootID
	defer func() { procStat, procBootID = prevStat, prevID }()
	info, err := readBootInfo()
	if err != nil || info.Time != 1795083707 || info.ID != "2f4c1f9e-0000-4000-8000-000000000001" {
		t.Fatalf("info=%+v err=%v", info, err)
	}
}
