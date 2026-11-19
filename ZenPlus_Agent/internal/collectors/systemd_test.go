package collectors

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const listUnitsFixture = `apparmor.service                loaded    active   exited  Load AppArmor profiles
cron.service                    loaded    active   running Regular background program processing daemon
● nginx.service                 loaded    failed   failed  A high performance web server
ghost.service                   not-found inactive dead    ghost.service
getty@tty1.service              loaded    active   running Getty on tty1
postgresql.service              loaded    inactive dead    PostgreSQL RDBMS
masked.service                  masked    inactive dead    masked.service
cron.service                    loaded    active   running duplicate line
snapd.socket                    loaded    active   listening Socket activation
`

const showFixture = `Id=apparmor.service
Description=Load AppArmor profiles
LoadState=loaded
ActiveState=active
SubState=exited
UnitFileState=enabled
MainPID=0
ExecMainStatus=0

Id=cron.service
Description=Regular background program processing daemon
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
MainPID=812
ExecMainStatus=0

Id=nginx.service
Description=A high performance web server and a reverse proxy server
LoadState=loaded
ActiveState=failed
SubState=failed
UnitFileState=enabled
MainPID=0
ExecMainStatus=1

Id=getty@tty1.service
Description=Getty on tty1
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=
MainPID=1022
ExecMainStatus=0

Id=postgresql.service
Description=PostgreSQL RDBMS
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=disabled
MainPID=0
ExecMainStatus=0

Id=masked.service
Description=masked.service
LoadState=masked
ActiveState=inactive
SubState=dead
UnitFileState=masked
MainPID=0
ExecMainStatus=0
`

func TestParseSystemdUnitListSkipsMarkersMissingUnitsAndOtherTypes(t *testing.T) {
	got := parseSystemdUnitList([]byte(listUnitsFixture))
	want := []string{"apparmor.service", "cron.service", "nginx.service", "getty@tty1.service", "postgresql.service", "masked.service"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("units = %v, want %v", got, want)
	}
}

func TestSystemdServicesMapToTheWindowsInventoryShape(t *testing.T) {
	var calls [][]string
	run := func(_ context.Context, name string, args ...string) ([]byte, error) {
		calls = append(calls, append([]string{name}, args...))
		if args[0] == "list-units" {
			return []byte(listUnitsFixture), nil
		}
		return []byte(showFixture), nil
	}
	services, err := listSystemdServices(context.Background(), run)
	if err != nil {
		t.Fatal(err)
	}
	if len(calls) != 2 || calls[1][0] != "systemctl" || calls[1][1] != "show" {
		t.Fatalf("unexpected systemctl calls: %v", calls)
	}
	if last := calls[1][len(calls[1])-6:]; !reflect.DeepEqual(last, []string{"apparmor.service", "cron.service", "nginx.service", "getty@tty1.service", "postgresql.service", "masked.service"}) {
		t.Fatalf("show was not asked for the listed units: %v", calls[1])
	}
	byName := map[string]serviceInfo{}
	for _, svc := range services {
		byName[svc.ServiceName] = svc
	}
	cases := map[string]serviceInfo{
		// A oneshot unit that finished and stays active is up, not stopped.
		"apparmor":   {ServiceName: "apparmor", DisplayName: "Load AppArmor profiles", State: "running", StartMode: "auto"},
		"cron":       {ServiceName: "cron", DisplayName: "Regular background program processing daemon", State: "running", StartMode: "auto", PID: 812},
		"nginx":      {ServiceName: "nginx", DisplayName: "A high performance web server and a reverse proxy server", State: "failed", StartMode: "auto", ExitCode: 1},
		"getty@tty1": {ServiceName: "getty@tty1", DisplayName: "Getty on tty1", State: "running", StartMode: "unknown", PID: 1022},
		"postgresql": {ServiceName: "postgresql", DisplayName: "PostgreSQL RDBMS", State: "stopped", StartMode: "manual"},
		"masked":     {ServiceName: "masked", DisplayName: "masked", State: "stopped", StartMode: "disabled"},
	}
	if len(byName) != len(cases) {
		t.Fatalf("services = %+v", services)
	}
	for name, want := range cases {
		if got := byName[name]; got != want {
			t.Errorf("%s = %+v, want %+v", name, got, want)
		}
	}
	// The controller's host_service_down rule matches these states.
	for _, inv := range serviceInventory(services) {
		if inv["service_name"] == "nginx" && inv["state"] != "failed" {
			t.Fatalf("failed unit reported as %v", inv["state"])
		}
		if inv["service_name"] == "postgresql" && (inv["state"] != "stopped" || inv["start_mode"] != "manual") {
			t.Fatalf("stopped unit reported as %v", inv)
		}
	}
}

func TestSystemdStateAndStartModeVocabulary(t *testing.T) {
	states := map[string]string{
		"active": "running", "reloading": "running", "activating": "start_pending",
		"deactivating": "stop_pending", "inactive": "stopped", "failed": "failed", "": "unknown",
	}
	for in, want := range states {
		if got := systemdServiceState(in); got != want {
			t.Errorf("state %q = %q, want %q", in, got, want)
		}
	}
	modes := map[string]string{
		"enabled": "auto", "enabled-runtime": "auto", "alias": "auto", "static": "manual",
		"disabled": "manual", "indirect": "manual", "generated": "manual", "transient": "manual",
		"masked": "disabled", "": "unknown",
	}
	for in, want := range modes {
		if got := systemdStartMode(in, "loaded"); got != want {
			t.Errorf("start mode %q = %q, want %q", in, got, want)
		}
	}
	if got := systemdStartMode("enabled", "masked"); got != "disabled" {
		t.Fatalf("masked unit start mode = %q", got)
	}
}

func TestSystemdShowIsBatched(t *testing.T) {
	var list strings.Builder
	for i := 0; i < maxSystemdUnitsPerShow+3; i++ {
		fmt.Fprintf(&list, "unit%d.service loaded active running Unit\n", i)
	}
	shows := 0
	run := func(_ context.Context, _ string, args ...string) ([]byte, error) {
		if args[0] == "list-units" {
			return []byte(list.String()), nil
		}
		shows++
		return nil, nil
	}
	if _, err := listSystemdServices(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	if shows != 2 {
		t.Fatalf("show calls = %d, want 2", shows)
	}
}

func TestSystemctlFailureIsACollectorError(t *testing.T) {
	run := func(context.Context, string, ...string) ([]byte, error) { return nil, errors.New("exit status 1") }
	if _, err := listSystemdServices(context.Background(), run); err == nil || !strings.Contains(err.Error(), "list-units") {
		t.Fatalf("err = %v", err)
	}
}

func TestSystemStateStoppingReadsOutputDespiteExitStatus(t *testing.T) {
	for out, want := range map[string]bool{"stopping\n": true, "running\n": false, "degraded\n": false, "": false} {
		run := func(context.Context, string, ...string) ([]byte, error) {
			return []byte(out), errors.New("exit status 1")
		}
		if got := systemStateStopping(context.Background(), run); got != want {
			t.Errorf("%q -> %v, want %v", out, got, want)
		}
	}
}

func TestParseLoadAverage(t *testing.T) {
	got, err := parseLoadAverage([]byte("1.40 2.66 3.02 5/2100 858021\n"))
	if err != nil {
		t.Fatal(err)
	}
	if got != (loadAverage{One: 1.40, Five: 2.66, Fifteen: 3.02}) {
		t.Fatalf("load = %+v", got)
	}
	for _, bad := range []string{"", "1.0 2.0", "a b c d e", "-1 0 0 1/1 1"} {
		if _, err := parseLoadAverage([]byte(bad)); err == nil {
			t.Errorf("%q parsed without error", bad)
		}
	}
}

func TestAddLoadAverageUsesTheControllerFields(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "loadavg")
	if err := os.WriteFile(path, []byte("0.50 0.25 0.10 1/300 42\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	previous := procLoadavg
	procLoadavg = path
	defer func() { procLoadavg = previous }()
	data, errs := map[string]any{}, map[string]string{}
	addLoadAverage(data, errs)
	if len(errs) != 0 || data["load_1"] != 0.50 || data["load_5"] != 0.25 || data["load_15"] != 0.10 {
		t.Fatalf("data=%v errs=%v", data, errs)
	}
	procLoadavg = filepath.Join(dir, "missing")
	data, errs = map[string]any{}, map[string]string{}
	addLoadAverage(data, errs)
	if _, ok := data["load_1"]; ok || errs["load_average"] == "" {
		t.Fatalf("missing source: data=%v errs=%v", data, errs)
	}
}

func TestParseBootTime(t *testing.T) {
	stat := "cpu  1 2 3 4\nintr 5\nctxt 6\nbtime 1795083707\nprocesses 7\n"
	if got, err := parseBootTime([]byte(stat)); err != nil || got != 1795083707 {
		t.Fatalf("btime = %d, %v", got, err)
	}
	if _, err := parseBootTime([]byte("cpu 1 2 3\n")); err == nil {
		t.Fatal("missing btime parsed")
	}
}
