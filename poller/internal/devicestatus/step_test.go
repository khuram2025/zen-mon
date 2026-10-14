package devicestatus

import (
	"reflect"
	"testing"
	"time"
)

var t0 = time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC)

func at(sec int) time.Time { return t0.Add(time.Duration(sec) * time.Second) }

func check(sec int, responding bool, rtt, loss float64) Check {
	return Check{TS: at(sec), Responding: responding, RTTMs: rtt, Loss: loss, IntervalS: 60, Ordered: true, Source: "central"}
}

// good, slow, lossy and fail build central checks at t0+sec.
func good(sec int) Check                { return check(sec, true, 10, 0) }
func slow(sec int, rtt float64) Check   { return check(sec, true, rtt, 0) }
func lossy(sec int, loss float64) Check { return check(sec, true, 10, loss) }
func fail(sec int) Check                { return check(sec, false, 0, 1) }

func maint(c Check) Check { c.Maintenance = true; return c }

func policyOf(raw map[string]any) Policy { return PolicyFromSettings(raw, Defaults{}) }

func seeded(status string) State { return Seed(status, nil, "central") }

// run steps every check in order and returns the final state plus the events
// of each check.
func run(s State, p Policy, checks ...Check) (State, [][]Event) {
	all := make([][]Event, 0, len(checks))
	for _, c := range checks {
		var ev []Event
		s, ev = Step(s, c, p)
		all = append(all, ev)
	}
	return s, all
}

func transition(old, new, reason string, alert, flapping bool) Event {
	return Event{Type: EventTransition, Old: old, New: new, Reason: reason, Alert: alert, Flapping: flapping}
}

func flapStartEvent(kind string) Event {
	return Event{Type: EventFlapStart, Kind: kind, Flapping: true}
}

func flapStopEvent(reason string) Event { return Event{Type: EventFlapStop, EndReason: reason} }

func wantEvents(t *testing.T, label string, got []Event, want ...Event) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%s events\n got %+v\nwant %+v", label, got, want)
	}
}

func wantQuiet(t *testing.T, events [][]Event, upTo int) {
	t.Helper()
	for i := 0; i < upTo; i++ {
		if len(events[i]) != 0 {
			t.Fatalf("check %d: unexpected events %+v", i, events[i])
		}
	}
}

func wantTimes(t *testing.T, label string, got []time.Time, want ...time.Time) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s: got %v, want %v", label, got, want)
	}
	for i := range want {
		if !got[i].Equal(want[i]) {
			t.Fatalf("%s: got %v, want %v", label, got, want)
		}
	}
}

func TestSingleCheckBlipsAreSuppressed(t *testing.T) {
	s, events := run(seeded(StatusUp), DefaultPolicy(),
		good(0), slow(60, 150), good(120), lossy(180, 1.0/3), good(240), good(300))
	wantQuiet(t, events, len(events))
	if s.HardStatus != StatusUp || s.WindowBits != 0b10100 || s.WindowLen != 5 {
		t.Fatalf("state after blips: %+v", s)
	}
	if bad, eff := s.BadInWindow(DefaultPolicy()); bad != 2 || eff != 5 {
		t.Fatalf("bad %d of %d, want 2 of 5", bad, eff)
	}
}

func TestDegradedNeedsThreeOfLastFive(t *testing.T) {
	p := DefaultPolicy()
	s, events := run(seeded(StatusUp), p,
		slow(0, 150), good(60), slow(120, 150), good(180), slow(240, 150))
	wantQuiet(t, events, 4)
	wantEvents(t, "5th check", events[4], transition(StatusUp, StatusDegraded,
		"Latency 150.000 ms exceeds 100 ms by 50.000 ms; 3 of the last 5 checks over limits or unanswered", true, false))
	if s.HardStatus != StatusDegraded || !s.StatusSince.Equal(at(240)) {
		t.Fatalf("state: %+v", s)
	}
	// The window restarts with the check that confirmed the change.
	if s.WindowBits != 1 || s.WindowLen != 1 {
		t.Fatalf("window after change: bits %b len %d", s.WindowBits, s.WindowLen)
	}
	wantTimes(t, "perf changes", s.PerfChanges, at(240))
	wantTimes(t, "reach changes", s.ReachChanges)

	// Bad checks that slide out of the window stop counting.
	s, events = run(seeded(StatusUp), p,
		slow(0, 150), good(60), good(120), good(180), slow(240, 150), good(300), slow(360, 150))
	wantQuiet(t, events, len(events))
	_, events = run(s, p, lossy(420, 0.5))
	wantEvents(t, "8th check", events[0], transition(StatusUp, StatusDegraded,
		"Packet loss 50.00% exceeds 10% by 40.00 percentage points; 3 of the last 5 checks over limits or unanswered", true, false))
}

func TestFailedChecksCountAsBadEvidenceButNeverDegrade(t *testing.T) {
	p := DefaultPolicy()
	// F F C: two failures are neither DOWN (3 needed) nor DEGRADED (2 of 3 bad).
	s, events := run(seeded(StatusUp), p, fail(0), fail(60), good(120))
	wantQuiet(t, events, len(events))
	if s.FailStreak != 0 || s.HardStatus != StatusUp {
		t.Fatalf("after F F C: %+v", s)
	}
	// A third failure makes 3 bad of 4, but a failing check never enters
	// DEGRADED: DOWN outranks it.
	s, events = run(s, p, fail(180))
	wantQuiet(t, events, 1)
	if s.HardStatus != StatusUp || s.FailStreak != 1 {
		t.Fatalf("after F F C F: %+v", s)
	}
	// The next answered check within limits confirms DEGRADED on the evidence.
	s, events = run(s, p, good(240))
	wantEvents(t, "F F C F C", events[0], transition(StatusUp, StatusDegraded,
		"3 of the last 5 checks over limits or unanswered; latest check within limits", true, false))
	if s.WindowBits != 0 || s.WindowLen != 1 {
		t.Fatalf("window after change: bits %b len %d", s.WindowBits, s.WindowLen)
	}
}

func TestDownAfterConsecutiveFailuresAndRecovery(t *testing.T) {
	p := DefaultPolicy()
	s, events := run(seeded(StatusUp), p, fail(0), fail(60), fail(120))
	wantQuiet(t, events, 2)
	wantEvents(t, "3rd failure", events[2], transition(StatusUp, StatusDown, "No response for 3 consecutive checks", true, false))
	if s.FailStreak != 3 || s.WindowLen != 0 || s.WindowBits != 0 {
		t.Fatalf("down state: %+v", s)
	}
	// Failures while DOWN keep counting (saturating) without touching the window.
	s, events = run(s, p, fail(180))
	wantQuiet(t, events, 1)
	if s.FailStreak != 4 || s.WindowLen != 0 {
		t.Fatalf("still down: %+v", s)
	}
	// A recovering device goes UP first, even when its first answer is slow.
	s, events = run(s, p, slow(240, 150))
	wantEvents(t, "recovery", events[0], transition(StatusDown, StatusUp, "Responding again", true, false))
	if s.OkStreak != 1 || s.FailStreak != 0 || s.WindowBits != 1 || s.WindowLen != 1 {
		t.Fatalf("recovered state: %+v", s)
	}
	wantTimes(t, "reach changes", s.ReachChanges, at(120), at(240))
	// DEGRADED is then confirmed on its own.
	s, events = run(s, p, slow(300, 150), slow(360, 150))
	wantQuiet(t, events, 1)
	wantEvents(t, "degraded", events[1], transition(StatusUp, StatusDegraded,
		"Latency 150.000 ms exceeds 100 ms by 50.000 ms; 3 of the last 3 checks over limits or unanswered", true, false))
	if s.OkStreak != 0 {
		t.Fatalf("streaks reset on change: %+v", s)
	}
}

func TestUpConfirmAndSensorDownConfirm(t *testing.T) {
	p := policyOf(map[string]any{"up_confirm_checks": 2.0, "sensor_down_confirm_checks": 1.0})
	sensorFail := fail(0)
	sensorFail.Source = SourceSensor
	sensorFail.Ordered = false
	s, events := run(seeded(StatusUp), p, sensorFail)
	wantEvents(t, "sensor failure", events[0], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, false))
	s, events = run(s, p, good(60), fail(120), good(180), good(240))
	wantQuiet(t, events, 3)
	wantEvents(t, "confirmed recovery", events[3], transition(StatusDown, StatusUp, "Responding for 2 consecutive checks", true, false))
	if s.OkStreak != 2 {
		t.Fatalf("ok streak keeps the value that caused the change: %+v", s)
	}
}

func TestUnknownBaseline(t *testing.T) {
	p := DefaultPolicy()
	_, events := run(seeded(StatusUnknown), p, slow(0, 150))
	wantEvents(t, "unknown slow", events[0], transition(StatusUnknown, StatusUp, "Device responding normally", true, false))

	s, events := run(seeded(StatusUnknown), p, fail(0), fail(60), fail(120))
	wantQuiet(t, events, 2)
	wantEvents(t, "unknown down", events[2], transition(StatusUnknown, StatusDown, "No response for 3 consecutive checks", true, false))
	wantTimes(t, "changes from unknown are not counted", s.ReachChanges)

	// A change from unknown has no class, so it never reports flapping even
	// when a (hand-built) state carries a flapping reachability history.
	flapping := seeded(StatusUnknown)
	flapping.ReachChanges = []time.Time{at(-300), at(-240), at(-180), at(-120)}
	flapping.ReachFlapping = true
	s, events = run(flapping, p, fail(0), fail(60), fail(120))
	wantEvents(t, "unknown down while flapping", events[2],
		transition(StatusUnknown, StatusDown, "No response for 3 consecutive checks", true, false))
	if !s.ReachFlapping || len(s.ReachChanges) != 4 {
		t.Fatalf("history must be kept and the change not recorded: %+v", s)
	}
}

func TestStatusOutsideTheContractIsUnknown(t *testing.T) {
	p := DefaultPolicy()
	for _, status := range []string{"", "pending", StatusMaintenance} {
		s := State{HardStatus: status}
		s, events := run(s, p, good(0))
		wantEvents(t, "status "+status, events[0], transition(StatusUnknown, StatusUp, "Device responding normally", true, false))
		if s.HardStatus != StatusUp {
			t.Fatalf("status %q: %+v", status, s)
		}
	}
}

func TestLegacyPresetReproducesSingleCheckBehaviour(t *testing.T) {
	p := policyOf(map[string]any{
		"degraded_confirm_checks": 1.0, "degraded_window_checks": 1.0, "degraded_clear_checks": 1.0,
		"degraded_rtt_clear_ms": 100.0, "degraded_loss_clear_pct": 10.0, "flap_detection_enabled": false,
	})
	s, events := run(seeded(StatusDown), p, slow(0, 150))
	wantEvents(t, "down slow", events[0], transition(StatusDown, StatusDegraded,
		"Responding again; Latency 150.000 ms exceeds 100 ms by 50.000 ms", true, false))
	wantTimes(t, "down->degraded is a reachability change", s.ReachChanges, at(0))

	_, events = run(seeded(StatusUnknown), p, lossy(0, 0.5))
	wantEvents(t, "unknown lossy", events[0], transition(StatusUnknown, StatusDegraded,
		"Packet loss 50.00% exceeds 10% by 40.00 percentage points", true, false))

	s, events = run(seeded(StatusUp), p, slow(0, 150), slow(60, 99))
	wantEvents(t, "up slow", events[0], transition(StatusUp, StatusDegraded,
		"Latency 150.000 ms exceeds 100 ms by 50.000 ms; 1 of the last 1 checks over limits or unanswered", true, false))
	wantEvents(t, "degraded clean", events[1], transition(StatusDegraded, StatusUp,
		"Within recovery limits (100 ms, 10%) for 1 consecutive checks", true, false))
	wantTimes(t, "perf changes", s.PerfChanges, at(0), at(60))
	if s.IsFlapping() {
		t.Fatal("flap detection is off in the legacy preset")
	}
}

func TestHysteresisRecovery(t *testing.T) {
	p := DefaultPolicy()

	// Inside the band (80 < rtt <= 100) the clear streak never grows, but a
	// full window without bad evidence recovers.
	s, events := run(seeded(StatusDegraded), p,
		slow(0, 90), slow(60, 95), lossy(120, 0.09), slow(180, 100), slow(240, 81))
	wantQuiet(t, events, 4)
	wantEvents(t, "window recovery", events[4], transition(StatusDegraded, StatusUp,
		"No slow, lossy or unanswered checks in the last 5 checks", true, false))
	if s.ClearStreak != 0 || s.WindowBits != 0 || s.WindowLen != 1 {
		t.Fatalf("after window recovery: %+v", s)
	}

	// The streak path: three consecutive checks inside the recovery limits.
	// A breach resets the streak and keeps the window path closed.
	s, events = run(seeded(StatusDegraded), p,
		good(0), good(60), slow(120, 150), good(180), slow(240, 80), good(300))
	wantQuiet(t, events, 5)
	wantEvents(t, "streak recovery", events[5], transition(StatusDegraded, StatusUp,
		"Within recovery limits (80 ms, 8%) for 3 consecutive checks", true, false))
	if s.ClearStreak != 3 {
		t.Fatalf("clear streak keeps the value that caused the change: %+v", s)
	}

	// A marginal check resets the streak too.
	s, events = run(seeded(StatusDegraded), p, good(0), good(60), slow(120, 90), good(180))
	wantQuiet(t, events, len(events))
	if s.ClearStreak != 1 || s.HardStatus != StatusDegraded {
		t.Fatalf("marginal check must reset the clear streak: %+v", s)
	}

	// A failure resets the streak and counts as bad evidence.
	s, events = run(seeded(StatusDegraded), p, good(0), good(60), fail(120), good(180), good(240))
	wantQuiet(t, events, len(events))
	if s.ClearStreak != 2 || s.HardStatus != StatusDegraded {
		t.Fatalf("failure must reset the clear streak: %+v", s)
	}

	// When both paths hold on the same check the streak reason wins.
	p3 := policyOf(map[string]any{"degraded_window_checks": 3.0})
	_, events = run(seeded(StatusDegraded), p3, good(0), good(60), good(120))
	wantEvents(t, "both paths", events[2], transition(StatusDegraded, StatusUp,
		"Within recovery limits (80 ms, 8%) for 3 consecutive checks", true, false))
}

func TestMaintenanceOverlay(t *testing.T) {
	p := DefaultPolicy()

	t.Run("entry and exit to down", func(t *testing.T) {
		s, events := run(seeded(StatusUp), p, maint(good(0)), maint(fail(60)), maint(fail(120)), maint(fail(180)), fail(240))
		wantEvents(t, "entry", events[0], transition(StatusUp, StatusMaintenance, "Maintenance window active", false, false))
		wantQuiet(t, events[1:], 3)
		wantEvents(t, "exit", events[4], transition(StatusMaintenance, StatusDown,
			"Maintenance ended; no response for 4 consecutive checks", true, false))
		if s.InMaintenance || s.HardStatus != StatusDown || !s.StatusSince.Equal(at(180)) {
			t.Fatalf("after exit: %+v", s)
		}
		wantTimes(t, "changes inside maintenance are not counted", s.ReachChanges)
	})

	t.Run("exit to up", func(t *testing.T) {
		_, events := run(seeded(StatusDown), p, maint(fail(0)), maint(fail(60)), good(120))
		wantEvents(t, "entry", events[0], transition(StatusDown, StatusMaintenance, "Maintenance window active", false, false))
		wantEvents(t, "exit", events[2], transition(StatusMaintenance, StatusUp,
			"Maintenance ended; device responding normally", true, false))
	})

	t.Run("exit to down while answering before recovery is confirmed", func(t *testing.T) {
		p2 := policyOf(map[string]any{"up_confirm_checks": 2.0})
		s, events := run(seeded(StatusDown), p2, maint(fail(0)), good(60), good(120))
		wantEvents(t, "exit", events[1], transition(StatusMaintenance, StatusDown,
			"Maintenance ended; responding for 1 of 2 checks needed to confirm recovery", true, false))
		wantEvents(t, "recovery", events[2], transition(StatusDown, StatusUp, "Responding for 2 consecutive checks", true, false))
		if s.HardStatus != StatusUp {
			t.Fatalf("after recovery: %+v", s)
		}
	})

	t.Run("exit to degraded", func(t *testing.T) {
		_, events := run(seeded(StatusDegraded), p, maint(slow(0, 150)), slow(60, 150))
		wantEvents(t, "exit", events[1], transition(StatusMaintenance, StatusDegraded,
			"Maintenance ended; latency or loss above limits", true, false))
	})

	t.Run("unknown keeps the overlay until confirmed", func(t *testing.T) {
		s := seeded(StatusMaintenance)
		if s.HardStatus != StatusUnknown || !s.InMaintenance {
			t.Fatalf("seed: %+v", s)
		}
		s, events := run(s, p, fail(0), fail(60))
		wantQuiet(t, events, 2)
		if s.Displayed() != StatusMaintenance {
			t.Fatalf("overlay dropped before confirmation: %+v", s)
		}
		s, events = run(s, p, fail(120))
		wantEvents(t, "confirmed", events[0], transition(StatusMaintenance, StatusDown,
			"Maintenance ended; no response for 3 consecutive checks", true, false))
		if s.Displayed() != StatusDown {
			t.Fatalf("after exit: %+v", s)
		}
	})

	t.Run("entry ends flapping", func(t *testing.T) {
		p1 := policyOf(map[string]any{"down_confirm_checks": 1.0})
		s, _ := run(seeded(StatusUp), p1, fail(0), good(60), fail(120), good(180))
		if !s.ReachFlapping {
			t.Fatalf("expected flapping: %+v", s)
		}
		s, events := run(s, p1, maint(fail(240)))
		wantEvents(t, "entry", events[0],
			transition(StatusUp, StatusMaintenance, "Maintenance window active", false, false),
			flapStopEvent(EndMaintenance))
		if s.IsFlapping() || s.FlappingSince != nil || len(s.ReachChanges) != 0 || len(s.PerfChanges) != 0 {
			t.Fatalf("maintenance must clear flapping: %+v", s)
		}
		if s.HardStatus != StatusDown {
			t.Fatalf("the machine keeps running in maintenance: %+v", s)
		}
	})
}

func TestReachabilityFlapping(t *testing.T) {
	p := policyOf(map[string]any{"down_confirm_checks": 1.0})
	s, events := run(seeded(StatusUp), p, fail(0), good(60), fail(120), good(180))
	wantEvents(t, "1", events[0], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, false))
	wantEvents(t, "3", events[2], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, false))
	wantEvents(t, "4th change", events[3],
		transition(StatusDown, StatusUp, "Responding again", true, true),
		flapStartEvent(KindReachability))
	if !s.FlappingSince.Equal(at(180)) || s.FlapKind() != KindReachability {
		t.Fatalf("flapping state: %+v", s)
	}

	// Changes while flapping carry the flag; no further flap events.
	s, events = run(s, p, fail(240), good(300))
	wantEvents(t, "5th change", events[0], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, true))
	wantEvents(t, "6th change", events[1], transition(StatusDown, StatusUp, "Responding again", true, true))
	if !s.FlappingSince.Equal(at(180)) {
		t.Fatalf("flapping_since moved: %+v", s)
	}

	// Stable for flap_stable_minutes after the last change -> stop.
	var checks []Check
	for sec := 360; sec <= 900; sec += 60 {
		checks = append(checks, good(sec))
	}
	s, events = run(s, p, checks...)
	wantQuiet(t, events, len(events)-1)
	wantEvents(t, "stable", events[len(events)-1], flapStopEvent(EndStable))
	if s.IsFlapping() || s.FlappingSince != nil || len(s.ReachChanges) != 0 {
		t.Fatalf("a stability stop clears the class history: %+v", s)
	}

	// A restart needs flap_start_changes fresh changes.
	s, events = run(s, p, fail(960), good(1020), fail(1080))
	for i, ev := range events {
		if len(ev) != 1 || ev[0].Type != EventTransition || ev[0].Flapping {
			t.Fatalf("change %d after the stop: %+v", i+1, ev)
		}
	}
	_, events = run(s, p, good(1140))
	wantEvents(t, "restart", events[0],
		transition(StatusDown, StatusUp, "Responding again", true, true),
		flapStartEvent(KindReachability))
}

func TestFlapStopByChangeCount(t *testing.T) {
	p := policyOf(map[string]any{
		"down_confirm_checks": 1.0, "flap_window_minutes": 10.0, "flap_stop_changes": 3.0, "flap_stable_minutes": 10.0,
	})
	s, events := run(seeded(StatusUp), p, fail(0), good(60), fail(120), good(180))
	wantEvents(t, "start", events[3], transition(StatusDown, StatusUp, "Responding again", true, true), flapStartEvent(KindReachability))
	// At t=540 the change at t=0 is still inside the 10 minute window
	// (exclusive bound at 0 only from t=600).
	s, events = run(s, p, good(540))
	wantQuiet(t, events, 1)
	s, events = run(s, p, good(600))
	wantEvents(t, "count stop", events[0], flapStopEvent(EndStable))
	// A count stop keeps the history (only a stability stop forgets it), so
	// a restart needs only enough changes inside the window.
	wantTimes(t, "count stop keeps history", s.ReachChanges, at(60), at(120), at(180))
	s, events = run(s, p, fail(660))
	wantEvents(t, "3 in window", events[0], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, false))
	_, events = run(s, p, good(700))
	wantEvents(t, "restart", events[0],
		transition(StatusDown, StatusUp, "Responding again", true, true),
		flapStartEvent(KindReachability))
}

func TestFlapStopCountAndQuietTogetherClearsHistory(t *testing.T) {
	p := policyOf(map[string]any{"down_confirm_checks": 1.0, "flap_stop_changes": 1.0})
	s := seeded(StatusUp)
	s.ReachChanges = []time.Time{at(-60)}
	s.ReachFlapping = true
	since := at(-600)
	s.FlappingSince = &since
	// One change left (count stop) that is also older than the stable time
	// (stability stop): the stability rule applies and the history is dropped.
	s, events := Step(s, good(600), p)
	wantEvents(t, "stop", events, flapStopEvent(EndStable))
	if len(s.ReachChanges) != 0 {
		t.Fatalf("history kept: %v", s.ReachChanges)
	}
}

func TestPerformanceAndCombinedFlapping(t *testing.T) {
	p := policyOf(map[string]any{
		"degraded_confirm_checks": 1.0, "degraded_window_checks": 1.0, "degraded_clear_checks": 1.0, "down_confirm_checks": 1.0,
	})
	s, events := run(seeded(StatusUp), p, slow(0, 150), good(60), slow(120, 150), good(180))
	wantEvents(t, "perf start", events[3],
		transition(StatusDegraded, StatusUp, "Within recovery limits (80 ms, 8%) for 1 consecutive checks", true, true),
		flapStartEvent(KindPerformance))

	// Reachability starts flapping too: the kind becomes both, no event.
	s, events = run(s, p, fail(240), good(300), fail(360), good(420))
	wantEvents(t, "reach 1 (perf flapping, reach not)", events[0], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, false))
	wantEvents(t, "reach 4", events[3], transition(StatusDown, StatusUp, "Responding again", true, true))
	if s.FlapKind() != KindBoth || !s.FlappingSince.Equal(at(180)) {
		t.Fatalf("combined flapping: %+v", s)
	}

	// Performance goes quiet first (its last change was at 180): it stops
	// without a device event while reachability keeps flapping.
	s, events = run(s, p, good(780))
	wantQuiet(t, events, 1)
	if s.PerfFlapping || !s.ReachFlapping || s.FlapKind() != KindReachability {
		t.Fatalf("perf should stop alone: %+v", s)
	}
	_, events = run(s, p, good(1020))
	wantEvents(t, "device stop", events[0], flapStopEvent(EndStable))
}

func TestFlapDetectionDisabledAndReenabled(t *testing.T) {
	on := policyOf(map[string]any{"down_confirm_checks": 1.0})
	off := policyOf(map[string]any{"down_confirm_checks": 1.0, "flap_detection_enabled": false})

	s, _ := run(seeded(StatusUp), on, fail(0), good(60), fail(120), good(180))
	s, events := run(s, off, good(240))
	wantEvents(t, "disabled", events[0], flapStopEvent(EndDisabled))
	wantTimes(t, "history kept while disabled", s.ReachChanges, at(0), at(60), at(120), at(180))

	// Changes keep being recorded while disabled, without starting.
	s, events = run(s, off, fail(300))
	wantEvents(t, "disabled change", events[0], transition(StatusUp, StatusDown, "No response for 1 consecutive checks", true, false))
	if len(s.ReachChanges) != 5 {
		t.Fatalf("change not recorded while disabled: %v", s.ReachChanges)
	}

	// Re-enabling evaluates the real history.
	s, events = run(s, on, fail(360))
	wantEvents(t, "re-enabled", events[0], flapStartEvent(KindReachability))
	if !s.FlappingSince.Equal(at(360)) {
		t.Fatalf("flapping_since: %+v", s)
	}
}

func TestReenableOnStableHistoryStartsNoEpisode(t *testing.T) {
	on := policyOf(map[string]any{"down_confirm_checks": 1.0})
	off := policyOf(map[string]any{"down_confirm_checks": 1.0, "flap_detection_enabled": false})
	s, _ := run(seeded(StatusUp), off, fail(0), good(60), fail(120), good(180))
	// The newest change is older than flap_stable_minutes: the start and the
	// stability stop apply on the same check, so no episode is opened and the
	// stale history is dropped.
	s, events := run(s, on, good(900))
	wantQuiet(t, events, 1)
	if s.IsFlapping() || len(s.ReachChanges) != 0 {
		t.Fatalf("stale history must not start flapping: %+v", s)
	}
}

func TestOrderingRules(t *testing.T) {
	p := DefaultPolicy()
	base, _ := run(seeded(StatusUp), p, fail(0), fail(60))

	t.Run("late remote sample is ignored", func(t *testing.T) {
		for _, sec := range []int{60, 30} {
			c := fail(sec)
			c.Ordered, c.Source = false, SourceSensor
			got, events := Step(base, c, p)
			if len(events) != 0 || !reflect.DeepEqual(got, base) {
				t.Fatalf("ts %d: state %+v events %+v", sec, got, events)
			}
		}
	})

	t.Run("central clock step is accepted", func(t *testing.T) {
		s := base
		s.ReachChanges = []time.Time{at(-600), at(30), at(60)}
		got, events := Step(s, fail(30), p)
		wantEvents(t, "clock step", events, transition(StatusUp, StatusDown, "No response for 3 consecutive checks", true, false))
		wantTimes(t, "future changes dropped", got.ReachChanges, at(-600), at(30), at(30))
		if !got.LastSampleAt.Equal(at(30)) {
			t.Fatalf("last_sample_at: %v", got.LastSampleAt)
		}
	})
}

func TestGapResetsStreaksAndWindow(t *testing.T) {
	p := DefaultPolicy()
	s, events := run(seeded(StatusUp), p, fail(0), fail(60), fail(241))
	wantQuiet(t, events, len(events))
	if s.FailStreak != 1 || s.WindowLen != 1 {
		t.Fatalf("a gap > 180 s must reset: %+v", s)
	}

	// Exactly at the floor is not a gap.
	_, events = run(seeded(StatusUp), p, fail(0), fail(60), fail(240))
	wantEvents(t, "no gap", events[2], transition(StatusUp, StatusDown, "No response for 3 consecutive checks", true, false))

	// Long intervals widen the gap to 3 x interval.
	slowPoll := func(c Check) Check { c.IntervalS = 120; return c }
	_, events = run(seeded(StatusUp), p, slowPoll(fail(0)), slowPoll(fail(120)), slowPoll(fail(480)))
	wantEvents(t, "3 x interval", events[2], transition(StatusUp, StatusDown, "No response for 3 consecutive checks", true, false))

	// The gap keeps the hard status, change lists and flapping.
	p1 := policyOf(map[string]any{"down_confirm_checks": 2.0})
	s, _ = run(seeded(StatusUp), p1, fail(0), fail(60), good(120), fail(180), fail(240), good(300), slow(360, 150))
	if !s.ReachFlapping || s.WindowLen != 2 {
		t.Fatalf("setup: %+v", s)
	}
	s, events = run(s, p1, slow(800, 150))
	wantQuiet(t, events, 1)
	if !s.ReachFlapping || len(s.ReachChanges) != 4 || s.HardStatus != StatusUp || s.WindowLen != 1 || s.WindowBits != 1 {
		t.Fatalf("gap must only reset streaks and window: %+v", s)
	}
}

func TestStepIsCopyOnWrite(t *testing.T) {
	p := policyOf(map[string]any{"down_confirm_checks": 1.0})
	s := seeded(StatusUp)
	backing := make([]time.Time, 1, 8)
	backing[0] = at(-60)
	s.ReachChanges = backing
	last := at(-30)
	s.LastSampleAt = &last
	before := s

	next, _ := Step(s, fail(0), p)
	if len(next.ReachChanges) != 2 || !backing[:2][1].IsZero() {
		t.Fatalf("Step wrote into the caller's backing array: %v", backing[:2])
	}
	if !reflect.DeepEqual(s, before) || !s.LastSampleAt.Equal(at(-30)) {
		t.Fatalf("input state changed: %+v", s)
	}
}

func TestStreaksSaturate(t *testing.T) {
	p := DefaultPolicy()
	s := seeded(StatusDown)
	s.FailStreak = 99
	s, _ = run(s, p, fail(0), fail(60))
	if s.FailStreak != streakCap {
		t.Fatalf("fail streak %d", s.FailStreak)
	}
	s = seeded(StatusDegraded)
	s.ClearStreak = 100
	s, _ = run(s, p, good(0))
	if s.ClearStreak != streakCap {
		t.Fatalf("clear streak %d", s.ClearStreak)
	}
}

func TestWindowRingShrinkAndGrow(t *testing.T) {
	var bits uint32
	n := 0
	for i := 0; i < 5; i++ {
		bits, n = windowPush(bits, n, 5, true)
	}
	if bits != 0b11111 || n != 5 {
		t.Fatalf("full ring: %b %d", bits, n)
	}
	if bad, eff := windowRead(bits, n, 3); bad != 3 || eff != 3 {
		t.Fatalf("shrunk read: %d of %d", bad, eff)
	}
	bits, n = windowPush(bits, n, 3, false)
	if bits != 0b110 || n != 3 {
		t.Fatalf("shrunk push: %b %d", bits, n)
	}
	if bad, eff := windowRead(bits, n, 10); bad != 2 || eff != 3 {
		t.Fatalf("grown read: %d of %d", bad, eff)
	}
	bits, n = windowPush(bits, n, 10, true)
	if bits != 0b1101 || n != 4 {
		t.Fatalf("grown push: %b %d", bits, n)
	}
	// Stray high bits beyond window_len never count.
	if bad, eff := windowRead(0b111100, 2, 20); bad != 0 || eff != 2 {
		t.Fatalf("stray bits: %d of %d", bad, eff)
	}
	if windowMask(0) != 0 || windowMask(-1) != 0 || windowMask(32) != ^uint32(0) || windowMask(20) != 1<<20-1 {
		t.Fatal("window masks")
	}
}

func TestPredicatesAndReasons(t *testing.T) {
	for _, tc := range []struct {
		loss, limit float64
		want        bool
	}{
		{float64(float32(.05)), 5, false},
		{float64(float32(.1)), 10, false},
		{.0501, 5, true},
		{1.0 / 3, 10, true},
		{.1, 10, false},
	} {
		if got := ExceedsPct(tc.loss, tc.limit); got != tc.want {
			t.Errorf("ExceedsPct(%v, %v) = %v", tc.loss, tc.limit, got)
		}
	}
	if ExceedsMs(100, 100) || ExceedsMs(100.0000005, 100) || !ExceedsMs(100.00001, 100) {
		t.Error("ExceedsMs tolerance")
	}

	for _, tc := range []struct {
		in   float64
		want string
	}{
		{500, "500"}, {5.6, "5.6"}, {1234.5678, "1234.568"}, {0.8, "0.8"}, {0, "0"}, {80.0000000001, "80"}, {8000, "8000"},
	} {
		if got := Fmt3(tc.in); got != tc.want {
			t.Errorf("Fmt3(%v) = %q, want %q", tc.in, got, tc.want)
		}
	}

	if got := DegradedReason(.839, .5, 500, 5); got != "Packet loss 50.00% exceeds 5% by 45.00 percentage points" {
		t.Error(got)
	}
	if got := DegradedReason(650, .5, 500, 5); got != "Latency 650.000 ms exceeds 500 ms by 150.000 ms; Packet loss 50.00% exceeds 5% by 45.00 percentage points" {
		t.Error(got)
	}
	if got := DegradedReason(650, float64(float32(.05)), 500, 5); got != "Latency 650.000 ms exceeds 500 ms by 150.000 ms" {
		t.Error("at-limit loss must not be blamed:", got)
	}
	if got := DegradedReason(10, 0, 100, 10); got != "High latency or packet loss" {
		t.Error(got)
	}
	if got := DegradedReason(100.1234, 0, 99.5, 10); got != "Latency 100.123 ms exceeds 99.5 ms by 0.623 ms" {
		t.Error(got)
	}
}

func TestClassify(t *testing.T) {
	p := DefaultPolicy()
	for _, tc := range []struct {
		c    Check
		want Class
	}{
		{fail(0), ClassFail},
		{slow(0, 150), ClassBreach},
		{lossy(0, 1.0/3), ClassBreach},
		{slow(0, 100), ClassMarginal},
		{lossy(0, .09), ClassMarginal},
		{slow(0, 80), ClassClear},
		{lossy(0, .08), ClassClear},
		{good(0), ClassClear},
	} {
		if got := Classify(tc.c, p); got != tc.want {
			t.Errorf("Classify(rtt %v loss %v responding %v) = %s, want %s", tc.c.RTTMs, tc.c.Loss, tc.c.Responding, got, tc.want)
		}
	}
}

func TestSeedAndReseed(t *testing.T) {
	since := at(-3600)
	for _, tc := range []struct {
		status, hard string
		maint        bool
	}{
		{StatusUp, StatusUp, false},
		{StatusDegraded, StatusDegraded, false},
		{StatusDown, StatusDown, false},
		{StatusUnknown, StatusUnknown, false},
		{StatusMaintenance, StatusUnknown, true},
		{"pending", StatusUnknown, false},
	} {
		s := Seed(tc.status, &since, "central")
		if s.HardStatus != tc.hard || s.InMaintenance != tc.maint || !s.StatusSince.Equal(since) || s.Owner != "central" {
			t.Errorf("Seed(%s) = %+v", tc.status, s)
		}
		if s.ReachChanges == nil || s.PerfChanges == nil || s.FailStreak != 0 || s.WindowLen != 0 || s.IsFlapping() || s.LastSampleAt != nil {
			t.Errorf("Seed(%s) not empty: %+v", tc.status, s)
		}
	}
	if s := Seed(StatusUp, nil, "x"); s.StatusSince != nil {
		t.Error("nil status_changed_at must stay nil")
	}

	row := Seed(StatusDown, nil, "central")
	for _, tc := range []struct {
		name          string
		row           *State
		status, owner string
		want          bool
		reason        string
	}{
		{"missing row", nil, StatusUp, "central", true, EndReseed},
		{"owner changed", &row, StatusDown, "4b7d0bb2-5c0a-4a1e-9a59-0b6f0c7f7a11", true, EndOwnerChange},
		{"status changed behind the row", &row, StatusUnknown, "central", true, EndReseed},
		{"maintenance shown", &row, StatusMaintenance, "central", false, ""},
		{"in sync", &row, StatusDown, "central", false, ""},
	} {
		got, reason := NeedsReseed(tc.row, tc.status, tc.owner)
		if got != tc.want || reason != tc.reason {
			t.Errorf("%s: NeedsReseed = %v %q, want %v %q", tc.name, got, reason, tc.want, tc.reason)
		}
	}
}

func TestPendingChange(t *testing.T) {
	p := DefaultPolicy()
	sensor := policyOf(map[string]any{"sensor_down_confirm_checks": 2.0})
	upTwoFails, _ := run(seeded(StatusUp), p, slow(0, 150), fail(60))
	degradedClear, _ := run(seeded(StatusDegraded), p, good(0))
	upSlow, _ := run(seeded(StatusUp), p, slow(0, 150), slow(60, 150))
	downAnswer, _ := run(seeded(StatusDown), policyOf(map[string]any{"up_confirm_checks": 3.0}), good(0))
	sensorFail := fail(0)
	sensorFail.Source = SourceSensor
	sensorUp, _ := run(seeded(StatusUp), sensor, sensorFail)

	for _, tc := range []struct {
		name   string
		s      State
		p      Policy
		source string
		want   *PendingChange
	}{
		{"steady up", seeded(StatusUp), p, "central", nil},
		{"down outranks degraded", upTwoFails, p, "central", &PendingChange{StatusDown, 1, 3}},
		{"degraded evidence", upSlow, p, "central", &PendingChange{StatusDegraded, 2, 3}},
		{"recovery limits", degradedClear, p, "central", &PendingChange{StatusUp, 1, 3}},
		{"up confirmation", downAnswer, policyOf(map[string]any{"up_confirm_checks": 3.0}), "central", &PendingChange{StatusUp, 1, 3}},
		{"sensor down confirmation", sensorUp, sensor, SourceSensor, &PendingChange{StatusDown, 1, 2}},
		{"down keeps counting failures", Seed(StatusDown, nil, "central"), p, "central", nil},
	} {
		got := Pending(tc.s, tc.p, tc.source)
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: Pending = %+v, want %+v", tc.name, got, tc.want)
		}
	}
}
