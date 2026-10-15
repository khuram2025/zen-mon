package pinger

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/devicestatus"
)

// statusFixture is an engine with one centrally polled device and helpers to
// feed it ping results at explicit times.
type statusFixture struct {
	t      *testing.T
	e      *Engine
	id     uuid.UUID
	base   time.Time
	n      int
	loader *fakeDeviceLoader
	writer *fakeMetricWriter
	pub    *fakeEventPublisher
	alerts *fakeAlertEvaluator
}

func newStatusFixture(t *testing.T, status string) *statusFixture {
	t.Helper()
	e := testEngine()
	stopStatusOnCleanup(t, e)
	f := &statusFixture{
		t:      t,
		e:      e,
		id:     uuid.New(),
		base:   time.Date(2026, 10, 14, 9, 0, 0, 0, time.UTC),
		loader: e.loader.(*fakeDeviceLoader),
		writer: e.writer.(*fakeMetricWriter),
		pub:    e.publisher.(*fakeEventPublisher),
		alerts: e.alerts.(*fakeAlertEvaluator),
	}
	e.devices[f.id] = &Device{ID: f.id, Hostname: "edge-01", IPAddress: net.ParseIP("192.0.2.10"),
		PingInterval: 30 * time.Second, PingEnabled: true, Status: status}
	return f
}

// at is the time of the next check (30 s apart).
func (f *statusFixture) at() time.Time {
	return f.base.Add(time.Duration(f.n) * 30 * time.Second)
}

func (f *statusFixture) send(r *PingResult) {
	f.t.Helper()
	r.DeviceID = f.id
	r.Timestamp = f.at()
	f.n++
	f.e.processStatusChange(context.Background(), r)
}

func (f *statusFixture) fail()                   { f.send(&PingResult{IsUp: false, PacketLoss: 1}) }
func (f *statusFixture) clean()                  { f.send(&PingResult{IsUp: true, RTT: 5 * time.Millisecond}) }
func (f *statusFixture) slow()                   { f.send(&PingResult{IsUp: true, RTT: 250 * time.Millisecond}) }
func (f *statusFixture) status() string          { return f.e.devices[f.id].Status }
func (f *statusFixture) device() *Device         { return f.e.devices[f.id] }
func (f *statusFixture) flush()                  { flushStatus(f.t, f.e) }
func (f *statusFixture) logged() []*StatusChange { return f.writer.changes() }

func TestDeviceBlipDoesNotChangeStatus(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.slow()
	f.clean()
	f.clean()
	f.send(&PingResult{IsUp: true, RTT: 5 * time.Millisecond, PacketLoss: 1.0 / 3})
	f.clean()
	if f.status() != "up" {
		t.Fatalf("status %s after isolated slow/lossy checks, want up", f.status())
	}
	f.flush()
	for i, s := range f.loader.savedStatuses() {
		if s != "up" {
			t.Fatalf("save %d persisted %q: a blip must never reach devices.status", i, s)
		}
	}
	if rows := f.logged(); len(rows) != 0 {
		t.Fatalf("status log rows for a blip: %+v", rows)
	}
	if got := f.alerts.eventsCopy(); len(got) != 0 {
		t.Fatalf("alert evaluations for a blip: %+v", got)
	}
}

func TestDeviceThreeOfFiveSlowChecksDegrade(t *testing.T) {
	f := newStatusFixture(t, "up")
	for i, step := range []func(){f.slow, f.clean, f.slow, f.clean} {
		step()
		if f.status() != "up" {
			t.Fatalf("check %d: status %s, want up", i, f.status())
		}
	}
	f.slow()
	if f.status() != "degraded" {
		t.Fatalf("status %s after 3 of 5 slow checks, want degraded", f.status())
	}
	f.flush()
	rows := f.logged()
	if len(rows) != 1 {
		t.Fatalf("status log rows = %d, want 1", len(rows))
	}
	want := "Latency 250.000 ms exceeds 100 ms by 150.000 ms; 3 of the last 5 checks over limits or unanswered"
	if rows[0].OldStatus != "up" || rows[0].NewStatus != "degraded" || rows[0].Reason != want {
		t.Fatalf("row = %+v", rows[0])
	}
	// The log row carries the confirming check's time (status explanation
	// matches it to that ping sample), not the writer's clock.
	if !rows[0].Timestamp.Equal(f.base.Add(4 * 30 * time.Second)) {
		t.Fatalf("row timestamp %v, want the check time", rows[0].Timestamp)
	}
	alerts := f.alerts.eventsCopy()
	if len(alerts) != 1 || alerts[0].NewStatus != "degraded" || alerts[0].RTTMs != 250 || alerts[0].Flapping {
		t.Fatalf("alerts = %+v", alerts)
	}
}

// devices.status is always the confirmed (displayed) status, even on the
// checks whose own classification differs from it.
func TestPersistedStatusIsNeverTheRawClass(t *testing.T) {
	f := newStatusFixture(t, "up")
	steps := []struct {
		run  func()
		want string
	}{
		{f.slow, "up"}, {f.slow, "up"}, {f.slow, "degraded"}, // confirmed on the 3rd
		{f.clean, "degraded"}, {f.clean, "degraded"}, {f.clean, "up"}, // recovery streak
		{f.fail, "up"}, {f.fail, "down"},
		{f.slow, "up"}, // a recovering device goes Up first, Degraded is confirmed on its own
	}
	for _, s := range steps {
		s.run()
	}
	f.flush()
	saves := f.loader.savesCopy()
	if len(saves) != len(steps) {
		t.Fatalf("%d saves for %d checks", len(saves), len(steps))
	}
	for i, s := range steps {
		if got := saves[i].State.Displayed(); got != s.want {
			t.Fatalf("check %d persisted %q, want %q", i, got, s.want)
		}
	}
	if !saves[8].Responded || saves[8].RTTMs != 250 || saves[7].Responded || saves[7].RTTMs != 0 {
		t.Fatalf("last_seen/RTT inputs: %+v / %+v", saves[7], saves[8])
	}
}

func TestPersistOnlyWhenStateChangesRespondsOrHeartbeat(t *testing.T) {
	f := newStatusFixture(t, "down")
	f.device().State = devicestatus.Seed("down", nil, ownerCentral)
	f.device().State.FailStreak = 100
	f.fail() // first check after the engine saw the device: written
	f.fail() // nothing changed (streak saturated), no reply: not written
	f.fail()
	f.flush()
	if got := len(f.loader.savesCopy()); got != 1 {
		t.Fatalf("%d saves for a steady down device, want 1", got)
	}

	// A reply always refreshes the devices row; the state row only when the
	// state changed: down -> up, then the window fills (4 more checks).
	for i := 0; i < 5; i++ {
		f.clean()
	}
	f.clean() // steady up with a full clean window: devices row only
	f.flush()
	saves := f.loader.savesCopy()
	if len(saves) != 7 || !saves[1].WriteState || !saves[5].WriteState || saves[6].WriteState || !saves[6].Responded {
		t.Fatalf("saves = %+v", saves)
	}

	// Heartbeat: the state row is rewritten at least every 10 minutes.
	lastStateWrite := saves[5].At
	for f.at().Sub(lastStateWrite) <= stateHeartbeat {
		f.clean()
	}
	f.flush()
	saves = f.loader.savesCopy()
	for i, s := range saves[6:] {
		heartbeat := s.At.Sub(lastStateWrite) >= stateHeartbeat
		if s.WriteState != heartbeat {
			t.Fatalf("save %d at +%s: WriteState=%v", i+6, s.At.Sub(lastStateWrite), s.WriteState)
		}
	}
	if !saves[len(saves)-1].WriteState {
		t.Fatal("no heartbeat state write after 10 minutes")
	}
}

// A restarted poller resumes from the persisted row: a failure streak from
// before the restart still counts towards Down.
func TestRestartRestoresPersistedState(t *testing.T) {
	e := testEngine()
	stopStatusOnCleanup(t, e)
	loader := e.loader.(*fakeDeviceLoader)
	id := uuid.New()
	last := time.Now().UTC().Add(-30 * time.Second).Truncate(time.Microsecond)
	state := devicestatus.Seed("up", nil, ownerCentral)
	state.FailStreak = 1
	state.WindowBits, state.WindowLen = 1, 1
	state.LastSampleAt = &last
	loader.devices = []*Device{{ID: id, Hostname: "edge-01", IPAddress: net.ParseIP("192.0.2.10"),
		PingInterval: 30 * time.Second, PingEnabled: true, Status: "up", State: state, StateLoaded: true}}

	if err := e.syncDevices(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := e.devices[id].State.FailStreak; got != 1 {
		t.Fatalf("restored fail streak %d, want 1", got)
	}
	e.processStatusChange(context.Background(), &PingResult{DeviceID: id, IsUp: false, Timestamp: last.Add(30 * time.Second)})
	if got := e.devices[id].Status; got != "down" {
		t.Fatalf("status %s, want down (2 consecutive failures across the restart)", got)
	}
	flushStatus(t, e)
	if rows := e.writer.(*fakeMetricWriter).changes(); len(rows) != 1 || rows[0].Reason != "No response for 2 consecutive checks" {
		t.Fatalf("rows = %+v", rows)
	}
}

func TestSyncPreservesInMemoryState(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.fail()
	if f.device().State.FailStreak != 1 {
		t.Fatal("failure not counted")
	}
	// The reloaded row lags behind (or the device has no row yet): the
	// engine keeps its own state.
	f.loader.devices = []*Device{{ID: f.id, Hostname: "edge-01", IPAddress: net.ParseIP("192.0.2.10"),
		PingInterval: 30 * time.Second, PingEnabled: true, Status: "up",
		State: devicestatus.Seed("up", nil, ownerCentral), SeedEndReason: devicestatus.EndReseed}}
	if err := f.e.syncDevices(context.Background()); err != nil {
		t.Fatal(err)
	}
	if f.device().State.FailStreak != 1 || f.device().SeedEndReason != "" {
		t.Fatalf("sync replaced the in-memory state: %+v", f.device().State)
	}
	f.fail()
	if f.status() != "down" {
		t.Fatalf("status %s, want down", f.status())
	}
}

func TestMaintenanceEntryAndExitEvents(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.e.deviceMaint = map[uuid.UUID]struct{}{f.id: {}}
	f.fail()
	if f.status() != "maintenance" {
		t.Fatalf("status %s, want maintenance", f.status())
	}
	f.fail()
	f.fail() // Down is confirmed underneath the overlay, silently.
	f.e.deviceMaint = map[uuid.UUID]struct{}{}
	f.fail()
	if f.status() != "down" {
		t.Fatalf("status %s after the window, want down", f.status())
	}
	f.flush()

	rows := f.logged()
	if len(rows) != 2 ||
		rows[0].OldStatus != "up" || rows[0].NewStatus != "maintenance" || rows[0].Reason != "Maintenance window active" ||
		rows[1].OldStatus != "maintenance" || rows[1].NewStatus != "down" ||
		rows[1].Reason != "Maintenance ended; no response for 4 consecutive checks" {
		t.Fatalf("status log rows = %+v", rows)
	}
	if pub := f.pub.changes(); len(pub) != 2 || pub[0].NewStatus != "maintenance" || pub[1].NewStatus != "down" {
		t.Fatalf("published = %+v", pub)
	}
	// The entry is not alerted (as before); the exit pages right away.
	alerts := f.alerts.eventsCopy()
	if len(alerts) != 1 || alerts[0].OldStatus != "maintenance" || alerts[0].NewStatus != "down" {
		t.Fatalf("alerts = %+v", alerts)
	}
	saves := f.loader.savedStatuses()
	if strings.Join(saves, ",") != "maintenance,maintenance,maintenance,down" {
		t.Fatalf("persisted statuses %v", saves)
	}
}

// Four confirmed reachability changes within 30 minutes start flapping. The
// transition that starts it carries flapping=true (the server holds its
// notification); the episode is opened from the persisted state.
func TestFlappingTransitionCarriesFlappingFlag(t *testing.T) {
	f := newStatusFixture(t, "up") // down confirmation 2 in the test config
	f.fail()
	f.fail()  // up -> down (1)
	f.clean() // down -> up (2)
	f.fail()
	f.fail()  // up -> down (3)
	f.clean() // down -> up (4): flapping starts
	if !f.device().State.IsFlapping() {
		t.Fatal("device not flapping after 4 changes in 30 minutes")
	}
	f.flush()

	alerts := f.alerts.eventsCopy()
	if len(alerts) != 4 {
		t.Fatalf("alerts = %+v", alerts)
	}
	for i, a := range alerts[:3] {
		if a.Flapping || a.IsFlapping {
			t.Fatalf("alert %d flagged flapping before the start: %+v", i, a)
		}
	}
	if last := alerts[3]; !last.Flapping || !last.IsFlapping || last.OldStatus != "down" || last.NewStatus != "up" {
		t.Fatalf("flap-starting transition = %+v", last)
	}
	pub := f.pub.changes()
	if len(pub) != 4 || !pub[3].IsFlapping || !pub[3].Flapping || pub[2].IsFlapping {
		t.Fatalf("published = %+v", pub)
	}

	saves := f.loader.savesCopy()
	start := saves[len(saves)-1]
	if !start.WriteState || !start.State.IsFlapping() || start.State.FlapKind() != devicestatus.KindReachability ||
		start.State.FlappingSince == nil || !start.State.FlappingSince.Equal(start.At) ||
		start.FlapChanges != 4 || start.FlapWindowMinutes != 30 {
		t.Fatalf("flap start save = %+v", start)
	}
	// Flap start/stop are never posted as alerts or logged as statuses.
	if rows := f.logged(); len(rows) != 4 {
		t.Fatalf("status log rows = %d, want the 4 transitions only", len(rows))
	}
}

func TestFlapStopReasonClosesTheEpisode(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.fail()
	f.fail()
	f.clean()
	f.fail()
	f.fail()
	f.clean() // flapping starts
	f.e.deviceMaint = map[uuid.UUID]struct{}{f.id: {}}
	f.clean() // maintenance entry stops flapping
	f.flush()
	saves := f.loader.savesCopy()
	stop := saves[len(saves)-1]
	if stop.State.IsFlapping() || !stop.WriteState || stop.EndReason != devicestatus.EndMaintenance {
		t.Fatalf("stop save = %+v", stop)
	}
	if saves[0].EndReason != devicestatus.EndStable {
		t.Fatalf("default end reason %q, want stable", saves[0].EndReason)
	}
}

// A reseeded device (no row, another owner, status changed behind the row)
// closes any episode left open with the seed's reason on its first write.
func TestSeedEndReasonGoesWithTheFirstStateWrite(t *testing.T) {
	e := testEngine()
	stopStatusOnCleanup(t, e)
	loader := e.loader.(*fakeDeviceLoader)
	id := uuid.New()
	loader.devices = []*Device{{ID: id, Hostname: "edge-01", IPAddress: net.ParseIP("192.0.2.10"),
		PingInterval: 30 * time.Second, PingEnabled: true, Status: "up",
		State: devicestatus.Seed("up", nil, ownerCentral), SeedEndReason: devicestatus.EndOwnerChange}}
	if err := e.syncDevices(context.Background()); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	e.processStatusChange(context.Background(), &PingResult{DeviceID: id, IsUp: false, Timestamp: now})
	e.processStatusChange(context.Background(), &PingResult{DeviceID: id, IsUp: true, RTT: time.Millisecond, Timestamp: now.Add(30 * time.Second)})
	flushStatus(t, e)
	saves := loader.savesCopy()
	if len(saves) != 2 || !saves[0].WriteState || saves[0].EndReason != devicestatus.EndOwnerChange ||
		saves[1].EndReason != devicestatus.EndStable {
		t.Fatalf("saves = %+v", saves)
	}
}

func TestSideEffectsAreOrderedPerDevice(t *testing.T) {
	a := newStatusFixture(t, "up")
	e := a.e
	// A second device on the same engine.
	b := &statusFixture{t: t, e: e, id: uuid.New(), base: a.base, loader: a.loader, writer: a.writer, pub: a.pub, alerts: a.alerts}
	e.devices[b.id] = &Device{ID: b.id, Hostname: "edge-02", IPAddress: net.ParseIP("192.0.2.11"),
		PingInterval: 30 * time.Second, PingEnabled: true, Status: "up"}

	// Device A's alert evaluations hang until released.
	gate := make(chan struct{})
	a.alerts.block = map[uuid.UUID]chan struct{}{a.id: gate}

	a.fail()
	a.fail()  // up -> down
	a.clean() // down -> up
	a.fail()
	a.fail() // up -> down
	b.fail()
	b.fail() // B: up -> down, must not wait behind A

	deadline := time.Now().Add(5 * time.Second)
	for {
		got := a.alerts.eventsCopy()
		if len(got) == 1 && got[0].DeviceID == b.id {
			break
		}
		if len(got) > 1 || time.Now().After(deadline) {
			t.Fatalf("device B's alert blocked or A's leaked: %+v", got)
		}
		time.Sleep(5 * time.Millisecond)
	}
	// A's status log and live events are not held up by its slow alert.
	waitFor(t, func() bool { return countFor(a.writer.changes(), a.id) == 3 && countFor(a.pub.changes(), a.id) == 3 })

	close(gate)
	a.flush()
	var seq []string
	for _, ev := range a.alerts.eventsCopy() {
		if ev.DeviceID == a.id {
			seq = append(seq, ev.OldStatus+">"+ev.NewStatus)
		}
	}
	if strings.Join(seq, ",") != "up>down,down>up,up>down" {
		t.Fatalf("device A's alerts out of order: %v", seq)
	}
	var logged []string
	for _, sc := range a.writer.changes() {
		if sc.DeviceID == a.id {
			logged = append(logged, sc.OldStatus+">"+sc.NewStatus)
		}
	}
	if strings.Join(logged, ",") != "up>down,down>up,up>down" {
		t.Fatalf("device A's status log out of order: %v", logged)
	}
}

func countFor(rows []*StatusChange, id uuid.UUID) int {
	n := 0
	for _, r := range rows {
		if r.DeviceID == id {
			n++
		}
	}
	return n
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met in time")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// A save that updated no devices row (the device moved to a sensor between
// the sync and the write) drops the check's events.
func TestEventsDroppedWhenTheDeviceIsNoLongerCentral(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.loader.saveResult = func(*DeviceStatusSave) (DeviceStatusSaved, error) {
		return DeviceStatusSaved{}, nil
	}
	f.fail()
	f.fail()
	if f.status() != "down" {
		t.Fatalf("status %s, want down in memory", f.status())
	}
	f.flush()
	if rows, pub, alerts := f.logged(), f.pub.changes(), f.alerts.eventsCopy(); len(rows)+len(pub)+len(alerts) != 0 {
		t.Fatalf("events delivered for a device that is no longer central: %v %v %v", rows, pub, alerts)
	}
}

// A failed write does not lose the confirmed change, and the next check
// rewrites the full state.
func TestSaveErrorDeliversEventsAndRewritesState(t *testing.T) {
	f := newStatusFixture(t, "up")
	failing := true
	f.loader.saveResult = func(*DeviceStatusSave) (DeviceStatusSaved, error) {
		if failing {
			return DeviceStatusSaved{}, errors.New("connection refused")
		}
		return DeviceStatusSaved{Device: true, State: true}, nil
	}
	f.fail()
	f.fail()
	f.flush()
	if rows := f.logged(); len(rows) != 1 || rows[0].NewStatus != "down" {
		t.Fatalf("rows = %+v", rows)
	}
	f.loader.mu.Lock()
	failing = false
	f.loader.mu.Unlock()
	f.device().State.FailStreak = 100
	f.fail() // only the streak changes; after a failed write it must be a full write
	f.flush()
	saves := f.loader.savesCopy()
	if last := saves[len(saves)-1]; !last.WriteState {
		t.Fatalf("check after a failed write was not a full write: %+v", last)
	}
}

// Schema lag: the state row could not be written, so every check sends the
// full state until it is.
func TestStateNotSavedKeepsWritingTheFullState(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.loader.saveResult = func(*DeviceStatusSave) (DeviceStatusSaved, error) {
		return DeviceStatusSaved{Device: true, State: false}, nil
	}
	f.clean()
	f.flush()
	f.clean()
	f.flush()
	for i, s := range f.loader.savesCopy() {
		if !s.WriteState {
			t.Fatalf("save %d skipped the state row while it is not persisted", i)
		}
	}
}

func TestDevicesLeavingCentralPollingAreReleased(t *testing.T) {
	f := newStatusFixture(t, "up")
	f.fail()
	f.loader.devices = nil // ping disabled, deleted, or moved to a sensor
	if err := f.e.syncDevices(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, ok := f.e.devices[f.id]; ok {
		t.Fatal("device still polled")
	}
	f.flush()
	f.loader.mu.Lock()
	released := f.loader.released
	saves := len(f.loader.saves)
	f.loader.mu.Unlock()
	if len(released) != 1 || len(released[0]) != 1 || released[0][0] != f.id || saves != 1 {
		t.Fatalf("released = %v after %d saves", released, saves)
	}
}

func TestStatusPolicyReloadKeepsPreviousOnError(t *testing.T) {
	e := testEngine()
	stopStatusOnCleanup(t, e)
	loader := e.loader.(*fakeDeviceLoader)
	loader.policyRaw = map[string]any{"degraded_confirm_checks": 2.0, "flap_detection_enabled": false}
	if err := e.syncDevices(context.Background()); err != nil {
		t.Fatal(err)
	}
	if e.policy.DegradedConfirmChecks != 2 || e.policy.FlapDetectionEnabled || e.policy.DownConfirmChecks != 2 {
		t.Fatalf("policy = %+v", e.policy)
	}
	loader.policyErr = errors.New("connection reset")
	if err := e.syncDevices(context.Background()); err != nil {
		t.Fatal(err)
	}
	if e.policy.DegradedConfirmChecks != 2 || e.policy.FlapDetectionEnabled {
		t.Fatalf("policy replaced after a load error: %+v", e.policy)
	}
}

func TestPlanPersist(t *testing.T) {
	now := time.Date(2026, 10, 14, 9, 0, 0, 0, time.UTC)
	steady := devicestatus.Seed("up", nil, ownerCentral)
	newer := steady
	later := now.Add(time.Minute)
	newer.LastSampleAt = &later
	changed := steady
	changed.FailStreak = 1

	d := &Device{}
	if write, state := planPersist(d, steady, false, now); !write || !state {
		t.Fatal("a never written state must be written")
	}
	d.persisted, d.stateWrittenAt = &steady, now
	if write, _ := planPersist(d, newer, false, now.Add(time.Minute)); write {
		t.Fatal("only last_sample_at changed and no reply: nothing to write")
	}
	if write, state := planPersist(d, newer, true, now.Add(time.Minute)); !write || state {
		t.Fatal("a reply refreshes the devices row only")
	}
	if write, state := planPersist(d, changed, false, now.Add(time.Minute)); !write || !state {
		t.Fatal("a changed state must be written")
	}
	if write, state := planPersist(d, newer, false, now.Add(stateHeartbeat)); !write || !state {
		t.Fatal("heartbeat not written")
	}
}

func TestDeviceCheckInputs(t *testing.T) {
	d := &Device{PingInterval: 0}
	ts := time.Date(2026, 10, 14, 9, 0, 0, 123456789, time.FixedZone("x", 3600))
	c := deviceCheck(d, &PingResult{IsUp: true, RTT: 1500 * time.Microsecond, PacketLoss: .1, Timestamp: ts}, true)
	if !c.TS.Equal(ts.Truncate(time.Microsecond)) || c.TS.Location() != time.UTC || c.TS.Nanosecond()%1000 != 0 {
		t.Fatalf("ts = %v", c.TS)
	}
	if !c.Responding || c.RTTMs != 1.5 || c.Loss != float64(float32(.1)) || c.IntervalS != 60 ||
		!c.Maintenance || !c.Ordered || c.Source != "central" {
		t.Fatalf("check = %+v", c)
	}
	c = deviceCheck(&Device{PingInterval: 30 * time.Second}, &PingResult{IsUp: false, RTT: time.Second, PacketLoss: 1, Timestamp: ts}, false)
	if c.Responding || c.RTTMs != 0 || c.IntervalS != 30 || c.Maintenance {
		t.Fatalf("failed check = %+v", c)
	}
}

// The production evaluator posts the StatusChangeEvent payload; tested
// against a private test server, never the local API.
func TestHTTPAlertEvaluatorPayload(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPost || r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("request %s %s", r.Method, r.Header.Get("Content-Type"))
		}
		if err := json.Unmarshal(body, &got); err != nil {
			t.Errorf("body: %v", err)
		}
		if strings.Contains(r.URL.Path, "fail") {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"notifications_sent": 1}`))
	}))
	defer srv.Close()

	h := newHTTPAlertEvaluator(testEngine().logger)
	if h.url != "http://localhost:8000/api/v1/alert-engine/evaluate" || h.client.Timeout != 30*time.Second {
		t.Fatalf("default evaluator %s timeout %s", h.url, h.client.Timeout)
	}
	h.url = srv.URL + "/evaluate"
	id := uuid.New()
	ev := DeviceAlertEvent{DeviceID: id, Hostname: "edge-01", IPAddress: net.ParseIP("192.0.2.10"),
		OldStatus: "up", NewStatus: "down", RTTMs: 0, PacketLoss: 1, Flapping: true, IsFlapping: true}
	if err := h.EvaluateDeviceStatus(context.Background(), ev); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"device_id": id.String(), "hostname": "edge-01", "ip_address": "192.0.2.10",
		"old_status": "up", "new_status": "down", "rtt_ms": 0.0, "packet_loss": 1.0,
		"flapping": true, "is_flapping": true}
	if len(got) != len(want) {
		t.Fatalf("payload keys = %v", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Fatalf("payload[%s] = %v, want %v", k, got[k], v)
		}
	}
	h.url = srv.URL + "/fail"
	if err := h.EvaluateDeviceStatus(context.Background(), ev); err == nil {
		t.Fatal("a 500 answer must be reported")
	}
}

// No unit test may reach the local API: the test engine's evaluator is a fake.
func TestTestEngineNeverPostsToTheLocalAPI(t *testing.T) {
	if _, ok := testEngine().alerts.(*fakeAlertEvaluator); !ok {
		t.Fatal("testEngine must inject a fake AlertEvaluator")
	}
}
