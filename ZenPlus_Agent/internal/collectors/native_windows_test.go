//go:build windows

package collectors

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/shirou/gopsutil/v4/process"

	"zenplus-agent/internal/config"
)

// powerShellJSON is used only by these tests, to compare the native
// collectors with what the previous PowerShell implementation reported.
func powerShellJSON(t *testing.T, script string, out any) {
	t.Helper()
	body, err := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script).Output()
	if err != nil {
		t.Skipf("PowerShell comparison unavailable: %v", err)
	}
	if err := json.Unmarshal(body, out); err != nil {
		t.Fatalf("decode PowerShell output: %v\n%s", err, body)
	}
}

func TestListServicesMatchesWin32Service(t *testing.T) {
	services, err := listServices()
	if err != nil {
		t.Fatal(err)
	}
	var wmi []struct {
		Name      string
		State     string
		StartMode string
	}
	powerShellJSON(t, `@(Get-CimInstance Win32_Service | Select-Object Name,State,StartMode) | ConvertTo-Json -Compress`, &wmi)

	native := make(map[string]serviceInfo, len(services))
	for _, svc := range services {
		native[strings.ToLower(svc.ServiceName)] = svc
	}
	missing, stateDiff, modeDiff := 0, 0, 0
	for _, w := range wmi {
		svc, ok := native[strings.ToLower(w.Name)]
		if !ok {
			missing++
			continue
		}
		if normalizeState(svc.State) != normalizeState(w.State) {
			stateDiff++ // a service may legitimately change state between the two reads
		}
		if normalizeStartMode(svc.StartMode) != normalizeStartMode(w.StartMode) {
			modeDiff++
		}
	}
	if len(wmi) < 20 || missing > 2 || stateDiff > 3 || modeDiff > 2 {
		t.Fatalf("native services disagree with Win32_Service: wmi=%d native=%d missing=%d state=%d start_mode=%d",
			len(wmi), len(services), missing, stateDiff, modeDiff)
	}
	eventLog, ok := native["eventlog"]
	if !ok || normalizeState(eventLog.State) != "running" || eventLog.PID <= 0 || eventLog.Description == "" {
		t.Fatalf("EventLog service not reported correctly: %+v", eventLog)
	}
}

func TestReadSystemCounters(t *testing.T) {
	counters, err := readSystemCounters()
	if err != nil {
		t.Fatal(err)
	}
	if counters["committed_bytes"] <= 0 || counters["cached_bytes"] <= 0 {
		t.Fatalf("memory counters missing: %v", counters)
	}
	if queue, ok := counters["load_1"]; !ok || queue < 0 {
		t.Fatalf("processor queue length missing: %v", counters)
	}
}

func TestCountEventsMatchesGetWinEvent(t *testing.T) {
	filters := []config.EventLogFilter{{Channel: "System", Levels: []string{"Information"}}}
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = &filters
	cfg.LookbackMinutes = 24 * 60
	queries, err := eventLogQueries(cfg)
	if err != nil {
		t.Fatal(err)
	}
	count, sample, err := countEvents(queries[0])
	if err != nil {
		t.Fatal(err)
	}
	var psCount int
	powerShellJSON(t, `@(Get-WinEvent -FilterHashtable @{LogName='System';Level=4;StartTime=(Get-Date).AddMinutes(-1440)} -ErrorAction SilentlyContinue).Count | ConvertTo-Json`, &psCount)
	// New events can arrive between the two reads.
	if count < psCount-5 || count > psCount+5 {
		t.Fatalf("native count %d, Get-WinEvent count %d", count, psCount)
	}
	if count > 0 && (len(sample) == 0 || len(sample) > eventLogSampleIDs) {
		t.Fatalf("sample IDs = %v for %d events", sample, count)
	}
}

func TestCountEventsTreatsMissingChannelAsEmpty(t *testing.T) {
	count, sample, err := countEvents(eventLogQuery{
		Channel: "ZenPlus-Does-Not-Exist/Operational", Level: "error",
		XPaths: []string{"*[System[Level=2]]"},
	})
	if err != nil || count != 0 || len(sample) != 0 {
		t.Fatalf("missing channel = %d %v %v, want zero events and no error", count, sample, err)
	}
}

func TestInstalledSoftwareReadsUninstallEntries(t *testing.T) {
	items, err := installedSoftware()
	if err != nil {
		t.Fatal(err)
	}
	if len(items) == 0 {
		t.Skip("no installed software registered on this host")
	}
	for _, item := range items {
		if item["package_name"] == "" || item["architecture"] == "" || item["source"] == "" {
			t.Fatalf("incomplete software entry: %v", item)
		}
	}
}

func TestCPUCollectionNeedsNoSamplingSleepAfterTheFirst(t *testing.T) {
	cpuState.Lock()
	cpuState.taken = false
	cpuState.Unlock()
	run := func() (map[string]any, time.Duration) {
		var data map[string]any
		errs := map[string]string{}
		start := time.Now()
		collectCPU(context.Background(), func(_ string, d map[string]any) { data = d }, errs)
		if len(errs) > 0 {
			t.Fatalf("cpu errors: %v", errs)
		}
		return data, time.Since(start)
	}
	run()
	time.Sleep(200 * time.Millisecond)
	data, elapsed := run()
	if elapsed > 150*time.Millisecond {
		t.Fatalf("second CPU collection took %s; it should not sample-sleep", elapsed)
	}
	for _, key := range []string{"cpu_total_pct", "cpu_idle_pct", "cpu_user_pct", "cpu_system_pct", "cpu_iowait_pct", "per_core"} {
		if _, ok := data[key]; !ok {
			t.Fatalf("cpu sample missing %q: %v", key, data)
		}
	}
	if total := data["cpu_total_pct"].(float64); total < 0 || total > 100 {
		t.Fatalf("cpu_total_pct = %v", total)
	}
}

func TestProcessSnapshotMatchesPerProcessQueries(t *testing.T) {
	snapshot, err := processSnapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	pids, err := process.Pids()
	if err != nil {
		t.Fatal(err)
	}
	// Processes come and go between the two reads, and PID 0 is omitted.
	if diff := len(snapshot) - len(pids); diff < -10 || diff > 10 {
		t.Fatalf("snapshot has %d processes, the system reports %d", len(snapshot), len(pids))
	}
	self := int32(os.Getpid())
	var entry *processSnapshotEntry
	for i := range snapshot {
		if snapshot[i].PID == self {
			entry = &snapshot[i]
		}
		if snapshot[i].PID == 0 {
			t.Fatal("snapshot includes the idle process")
		}
	}
	if entry == nil {
		t.Fatal("snapshot is missing the test process")
	}
	p, err := process.NewProcess(self)
	if err != nil {
		t.Fatal(err)
	}
	name, _ := p.Name()
	created, _ := p.CreateTime()
	if !strings.EqualFold(entry.Name, name) || entry.CreatedMs != created {
		t.Fatalf("snapshot %+v disagrees with gopsutil name=%q created=%d", *entry, name, created)
	}
	if entry.Threads <= 0 || entry.Handles <= 0 || entry.RSS == 0 {
		t.Fatalf("snapshot counters missing: %+v", *entry)
	}
}
