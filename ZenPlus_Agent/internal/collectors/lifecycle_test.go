package collectors

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"
)

const (
	bootA = int64(1795000000)
	bootB = int64(1795086400)
)

func newTestTracker(t *testing.T, shuttingDown bool) *lifecycleTracker {
	t.Helper()
	return &lifecycleTracker{
		path:         filepath.Join(t.TempDir(), "state", lifecycleStateFile),
		computer:     "web-01",
		now:          func() time.Time { return time.Unix(bootB+600, 0).UTC() },
		shuttingDown: func(context.Context) bool { return shuttingDown },
	}
}

func bootReader(id string, at int64) func() (bootInfo, error) {
	return func() (bootInfo, error) { return bootInfo{ID: id, Time: at}, nil }
}

func writeTestState(t *testing.T, tr *lifecycleTracker, state lifecycleState) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(tr.path), 0o700); err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(state)
	if err := os.WriteFile(tr.path, body, 0o600); err != nil {
		t.Fatal(err)
	}
}

func readTestState(t *testing.T, tr *lifecycleTracker) lifecycleState {
	t.Helper()
	raw, err := os.ReadFile(tr.path)
	if err != nil {
		t.Fatal(err)
	}
	var state lifecycleState
	if err := json.Unmarshal(raw, &state); err != nil {
		t.Fatal(err)
	}
	return state
}

func eventIDs(events []map[string]any) []int {
	ids := []int{}
	for _, e := range events {
		ids = append(ids, e["event_id"].(int))
	}
	sort.Ints(ids)
	return ids
}

func TestFirstRunReportsNothingAndArms(t *testing.T) {
	tr := newTestTracker(t, false)
	if err := tr.start(bootReader("boot-b", bootB)); err != nil {
		t.Fatal(err)
	}
	if len(tr.pending) != 0 || !tr.armed {
		t.Fatalf("pending=%v armed=%v", tr.pending, tr.armed)
	}
	if s := readTestState(t, tr); s.State != lifecycleRunning || s.BootID != "boot-b" || s.BootTime != bootB {
		t.Fatalf("state = %+v", s)
	}
}

func TestAgentRestartInTheSameBootReportsNothing(t *testing.T) {
	tr := newTestTracker(t, false)
	// boot_id identifies the boot even if btime moved by a few seconds.
	writeTestState(t, tr, lifecycleState{BootID: "boot-b", BootTime: bootB - 1, State: lifecycleRunning})
	if err := tr.start(bootReader("boot-b", bootB)); err != nil || len(tr.pending) != 0 {
		t.Fatalf("pending=%v err=%v", tr.pending, err)
	}
	// Without a boot ID, two minutes of btime drift is still the same boot.
	tr = newTestTracker(t, false)
	writeTestState(t, tr, lifecycleState{BootTime: bootB - 90, State: lifecycleRunning})
	if err := tr.start(bootReader("", bootB)); err != nil || len(tr.pending) != 0 {
		t.Fatalf("pending=%v err=%v", tr.pending, err)
	}
}

func TestNewBootEvidenceDependsOnHowThePreviousBootEnded(t *testing.T) {
	for previous, want := range map[string][]int{
		lifecycleSystemShutdown: {lifecycleEventStarted},
		lifecycleAgentStopped:   {lifecycleEventStarted},
		lifecycleRunning:        {lifecycleEventStarted, lifecycleEventUnexpected},
	} {
		tr := newTestTracker(t, false)
		writeTestState(t, tr, lifecycleState{BootID: "boot-a", BootTime: bootA, State: previous,
			UpdatedAt: time.Unix(bootA+3600, 0).UTC()})
		if err := tr.start(bootReader("boot-b", bootB)); err != nil {
			t.Fatal(err)
		}
		if got := eventIDs(tr.pending); !equalInts(got, want) {
			t.Fatalf("after %s: events %v, want %v", previous, got, want)
		}
		boot := time.Unix(bootB, 0).UTC().Format(time.RFC3339)
		for _, e := range tr.pending {
			// Both are dated at the new boot, which is what lets the controller
			// describe the restart as unexpected.
			if e["event_time"] != boot || e["provider"] != lifecycleProvider || e["log_name"] != "System" || e["computer"] != "web-01" {
				t.Fatalf("event = %v", e)
			}
			if e["event_id"] == lifecycleEventUnexpected && e["level"] != "critical" {
				t.Fatalf("unexpected-shutdown level = %v", e["level"])
			}
		}
		// Not marked as reported until the evidence is spooled.
		if tr.armed || readTestState(t, tr).State != previous {
			t.Fatalf("state advanced before the evidence was queued")
		}
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestEvidenceIsKeptUntilQueuedAndStopBeforeThatKeepsTheOldState(t *testing.T) {
	tr := newTestTracker(t, true)
	writeTestState(t, tr, lifecycleState{BootID: "boot-a", BootTime: bootA, State: lifecycleRunning})
	if err := tr.start(bootReader("boot-b", bootB)); err != nil {
		t.Fatal(err)
	}
	first := tr.metrics()
	second := tr.metrics() // an enqueue failed in between: offered again
	if len(first) != 2 || len(second) != 2 || first[0].Kind != "event_detail" {
		t.Fatalf("offers = %v / %v", first, second)
	}
	if first[0].Data["event_uid"] != second[0].Data["event_uid"] || first[0].Data["record_id"] != second[0].Data["record_id"] {
		t.Fatal("re-offered evidence changed identity")
	}
	// Stopping now must not hide this boot's restart from the next start.
	if m, err := tr.stop(context.Background()); err != nil || len(m) != 0 {
		t.Fatalf("stop before queue: %v %v", m, err)
	}
	if readTestState(t, tr).BootID != "boot-a" {
		t.Fatal("state moved to the new boot before its evidence was queued")
	}
	if err := tr.queued(); err != nil {
		t.Fatal(err)
	}
	if len(tr.pending) != 0 || len(tr.metrics()) != 0 {
		t.Fatal("queued evidence offered again")
	}
	if s := readTestState(t, tr); s.State != lifecycleRunning || s.BootID != "boot-b" {
		t.Fatalf("state = %+v", s)
	}
}

func TestStopDuringSystemShutdownSendsShutdownEvidence(t *testing.T) {
	tr := newTestTracker(t, true)
	if err := tr.start(bootReader("boot-b", bootB)); err != nil {
		t.Fatal(err)
	}
	metrics, err := tr.stop(context.Background())
	if err != nil || len(metrics) != 1 {
		t.Fatalf("metrics=%v err=%v", metrics, err)
	}
	data := metrics[0].Data
	if metrics[0].Kind != "event_detail" || data["event_id"] != lifecycleEventShutdown || data["provider"] != lifecycleProvider {
		t.Fatalf("shutdown event = %v", metrics[0])
	}
	if data["event_time"] != time.Unix(bootB+600, 0).UTC().Format(time.RFC3339) {
		t.Fatalf("shutdown time = %v", data["event_time"])
	}
	if s := readTestState(t, tr); s.State != lifecycleSystemShutdown {
		t.Fatalf("state = %+v", s)
	}
	// The next boot is then a plain restart, not an unexpected one.
	next := newTestTracker(t, false)
	next.path = tr.path
	if err := next.start(bootReader("boot-c", bootB+7200)); err != nil {
		t.Fatal(err)
	}
	if got := eventIDs(next.pending); !equalInts(got, []int{lifecycleEventStarted}) {
		t.Fatalf("events after clean shutdown = %v", got)
	}
}

func TestStopOfTheAgentAloneSendsNothing(t *testing.T) {
	tr := newTestTracker(t, false)
	if err := tr.start(bootReader("boot-b", bootB)); err != nil {
		t.Fatal(err)
	}
	if metrics, err := tr.stop(context.Background()); err != nil || len(metrics) != 0 {
		t.Fatalf("metrics=%v err=%v", metrics, err)
	}
	if s := readTestState(t, tr); s.State != lifecycleAgentStopped {
		t.Fatalf("state = %+v", s)
	}
}

func TestLifecycleEventMatchesTheDetailedEventShape(t *testing.T) {
	at := time.Unix(bootB, 500).UTC()
	event := lifecycleEvent(lifecycleEventStarted, at, "information", "started", "host", nil)
	windowsShape := []string{"event_uid", "log_name", "event_id", "record_id", "event_time", "provider", "level",
		"computer", "message", "message_truncated", "fields"}
	if len(event) != len(windowsShape) {
		t.Fatalf("keys = %v", event)
	}
	for _, key := range windowsShape {
		if _, ok := event[key]; !ok {
			t.Fatalf("missing %s in %v", key, event)
		}
	}
	if event["record_id"] != uint64(bootB)*100+12 || event["event_time"] != time.Unix(bootB, 0).UTC().Format(time.RFC3339) {
		t.Fatalf("identity = %v %v", event["record_id"], event["event_time"])
	}
}

func TestGlobalLifecycleHooksFeedCollection(t *testing.T) {
	tr := newTestTracker(t, false)
	writeTestState(t, tr, lifecycleState{BootID: "boot-a", BootTime: bootA, State: lifecycleAgentStopped})
	if err := tr.start(bootReader("boot-b", bootB)); err != nil {
		t.Fatal(err)
	}
	lifecycleTrack.Lock()
	previous := lifecycleTrack.t
	lifecycleTrack.t = tr
	lifecycleTrack.Unlock()
	defer func() {
		lifecycleTrack.Lock()
		lifecycleTrack.t = previous
		lifecycleTrack.Unlock()
	}()
	if got := lifecycleMetrics(); len(got) != 1 {
		t.Fatalf("collection evidence = %v", got)
	}
	LifecycleQueued()
	if got := lifecycleMetrics(); len(got) != 0 {
		t.Fatalf("evidence offered after queueing = %v", got)
	}
	if got := StopLifecycle(context.Background()); len(got) != 0 {
		t.Fatalf("agent-only stop produced %v", got)
	}
}
