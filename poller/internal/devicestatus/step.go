package devicestatus

import (
	"math"
	"time"
)

// Check is one poll-cycle result for one device. A remote sensor's in-cycle
// retries still make ONE check.
type Check struct {
	TS         time.Time `json:"ts"`
	Responding bool      `json:"responding"`
	RTTMs      float64   `json:"rtt_ms"`
	// Loss is the packet-loss fraction (0..1).
	Loss float64 `json:"loss"`
	// IntervalS is the configured ping interval; it sizes the gap reset.
	IntervalS float64 `json:"interval_s"`
	// Maintenance is true while an active maintenance window covers the device.
	Maintenance bool `json:"maintenance"`
	// Ordered is true for a single sequential writer (the central poller):
	// a timestamp at or before the last sample is a clock step, not a stale
	// sample.
	Ordered bool `json:"ordered"`
	// Source is SourceSensor for remote-sensor checks (sensor down
	// confirmation); anything else is central.
	Source string `json:"source"`
}

// Event types.
const (
	EventTransition = "transition"
	EventFlapStart  = "flap_start"
	EventFlapStop   = "flap_stop"
)

// Event is one outcome of Step, in delivery order.
//
//   - transition: Old -> New with Reason. Alert is false only for the
//     maintenance entry. Flapping reports whether the change's class
//     (reachability or performance) is flapping after this check.
//   - flap_start: the device started flapping; Kind is the derived flap kind
//     and Flapping is true.
//   - flap_stop: the device stopped flapping; EndReason is stable, disabled
//     or maintenance (Reason stays empty: it is the transition text).
type Event struct {
	Type      string `json:"type"`
	Old       string `json:"old,omitempty"`
	New       string `json:"new,omitempty"`
	Reason    string `json:"reason,omitempty"`
	Alert     bool   `json:"alert"`
	Flapping  bool   `json:"flapping"`
	Kind      string `json:"kind,omitempty"`
	EndReason string `json:"end_reason,omitempty"`
}

// Change classes (DESIGN.md "Change classes").
const (
	classNone = iota
	classReach
	classPerf
)

// changeClass classifies a hard change: into or out of DOWN is reachability,
// UP <-> DEGRADED is performance; a change from or to UNKNOWN has no class
// (never recorded, and its transition never reports flapping).
func changeClass(prev, next string) int {
	switch {
	case prev == next, prev == StatusUnknown, next == StatusUnknown:
		return classNone
	case prev == StatusDown || next == StatusDown:
		return classReach
	case (prev == StatusUp && next == StatusDegraded) || (prev == StatusDegraded && next == StatusUp):
		return classPerf
	default:
		return classNone
	}
}

// gapFloorSeconds is the minimum silence that resets the streaks and window.
const gapFloorSeconds = 180.0

// Step advances the machine by one check. It is pure: s is not modified (the
// change lists are copied) and only c.TS is used as the clock.
func Step(s State, c Check, p Policy) (State, []Event) {
	s = s.clone()
	ts := c.TS

	// 0. Ordering and gaps.
	if s.LastSampleAt != nil {
		last := *s.LastSampleAt
		if !ts.After(last) {
			if !c.Ordered {
				// A late or duplicate remote sample: ignore it entirely.
				return s, nil
			}
			// The central clock stepped back: accept, and forget changes
			// that are now in the future.
			s.ReachChanges = keepNotAfter(s.ReachChanges, ts)
			s.PerfChanges = keepNotAfter(s.PerfChanges, ts)
		} else if ts.Sub(last).Seconds() > math.Max(float64(3*c.IntervalS), gapFloorSeconds) {
			s.FailStreak, s.OkStreak, s.ClearStreak = 0, 0, 0
			s.clearWindow()
		}
	}
	s.LastSampleAt = timePtr(ts)
	switch s.HardStatus {
	case StatusUp, StatusDegraded, StatusDown, StatusUnknown:
	default:
		// A status outside the contract (the zero State included) is unconfirmed.
		s.HardStatus = StatusUnknown
	}

	var events []Event

	// 1. Maintenance entry.
	if c.Maintenance && !s.InMaintenance {
		wasFlapping := s.IsFlapping()
		events = append(events, Event{
			Type:   EventTransition,
			Old:    s.Displayed(),
			New:    StatusMaintenance,
			Reason: reasonMaintenanceActive,
		})
		s.InMaintenance = true
		s.ReachChanges, s.PerfChanges = []time.Time{}, []time.Time{}
		s.ReachFlapping, s.PerfFlapping = false, false
		s.FlappingSince = nil
		if wasFlapping {
			events = append(events, flapStop(EndMaintenance))
		}
	}

	// 2. Hard-status machine.
	prev := s.HardStatus
	next := prev
	class := Classify(c, p)
	breach := class == ClassBreach
	reason := ""
	const (
		keepNone = iota
		keepFail
		keepOk
		keepClear
	)
	keep := keepNone

	if class == ClassFail {
		s.FailStreak = min(s.FailStreak+1, streakCap)
		s.OkStreak, s.ClearStreak = 0, 0
		if prev != StatusDown {
			s.push(true, p)
			if s.FailStreak >= p.DownConfirm(c.Source) {
				next, reason, keep = StatusDown, reasonDown(s.FailStreak), keepFail
			}
		}
	} else {
		s.FailStreak = 0
		toTarget := false
		switch prev {
		case StatusDown:
			s.OkStreak = min(s.OkStreak+1, streakCap)
			if s.OkStreak >= p.UpConfirmChecks {
				toTarget, keep = true, keepOk
			}
		case StatusUp:
			s.push(breach, p)
			if bad, eff := s.BadInWindow(p); bad >= p.DegradedConfirmChecks {
				next, reason = StatusDegraded, reasonWindowDegraded(c, p, breach, bad, eff)
			}
		case StatusDegraded:
			s.push(breach, p)
			if class == ClassClear {
				s.ClearStreak = min(s.ClearStreak+1, streakCap)
			} else {
				s.ClearStreak = 0
			}
			bad, eff := s.BadInWindow(p)
			switch {
			case s.ClearStreak >= p.DegradedClearChecks:
				next, reason, keep = StatusUp, reasonClearStreak(p, s.ClearStreak), keepClear
			case s.WindowLen >= p.DegradedWindowChecks && bad == 0:
				next, reason = StatusUp, reasonCleanWindow(eff)
			}
		default:
			// unknown: the first responding check is the baseline.
			toTarget = true
		}
		if toTarget {
			s.clearWindow()
			s.push(breach, p)
			next = StatusUp
			if bad, _ := s.BadInWindow(p); breach && bad >= p.DegradedConfirmChecks {
				next = StatusDegraded
			}
			switch {
			case prev == StatusDown && next == StatusUp:
				reason = reasonDownRecovered(p, s.OkStreak)
			case prev == StatusDown:
				reason = reasonRespondingAgain + "; " + checkDegradedReason(c, p)
			case next == StatusUp:
				reason = reasonUnknownUp
			default:
				reason = checkDegradedReason(c, p)
			}
		}
	}

	transition := -1
	changed := changeClass(prev, next)
	if next != prev {
		s.HardStatus = next
		s.StatusSince = timePtr(ts)
		if keep != keepFail {
			s.FailStreak = 0
		}
		if keep != keepOk {
			s.OkStreak = 0
		}
		if keep != keepClear {
			s.ClearStreak = 0
		}
		s.clearWindow()
		if c.Responding {
			s.push(breach, p)
		}
		if !s.InMaintenance {
			switch changed {
			case classReach:
				s.ReachChanges = append(s.ReachChanges, ts)
			case classPerf:
				s.PerfChanges = append(s.PerfChanges, ts)
			}
		}
		if !s.InMaintenance {
			transition = len(events)
			events = append(events, Event{Type: EventTransition, Old: prev, New: next, Reason: reason, Alert: true})
		}
	}

	// 3. Maintenance exit (an unconfirmed device keeps the overlay).
	if !c.Maintenance && s.InMaintenance {
		exit := ""
		switch s.HardStatus {
		case StatusUp:
			exit = reasonMaintenanceUp
		case StatusDegraded:
			exit = reasonMaintenanceDeg
		case StatusDown:
			exit = reasonMaintenanceDown(p, s.FailStreak, s.OkStreak)
		}
		if exit != "" {
			s.InMaintenance = false
			events = append(events, Event{
				Type:   EventTransition,
				Old:    StatusMaintenance,
				New:    s.HardStatus,
				Reason: exit,
				Alert:  true,
			})
		}
	}

	// 4. Flap detection (not while the maintenance overlay is shown).
	if !s.InMaintenance {
		wasFlapping := s.IsFlapping()
		var reachStop, perfStop string
		s.ReachChanges, s.ReachFlapping, reachStop = evaluateFlap(s.ReachChanges, s.ReachFlapping, ts, p)
		s.PerfChanges, s.PerfFlapping, perfStop = evaluateFlap(s.PerfChanges, s.PerfFlapping, ts, p)
		switch isFlapping := s.IsFlapping(); {
		case !wasFlapping && isFlapping:
			s.FlappingSince = timePtr(ts)
			events = append(events, Event{Type: EventFlapStart, Kind: s.FlapKind(), Flapping: true})
		case wasFlapping && !isFlapping:
			s.FlappingSince = nil
			stop := reachStop
			if stop == "" {
				stop = perfStop
			}
			events = append(events, flapStop(stop))
		}
	}

	// 5. The hard transition reports whether its own class is flapping.
	if transition >= 0 {
		switch changed {
		case classReach:
			events[transition].Flapping = s.ReachFlapping
		case classPerf:
			events[transition].Flapping = s.PerfFlapping
		}
	}
	return s, events
}

// evaluateFlap prunes one class's change times and applies the start/stop
// rules. stopped is the end reason when a class that was flapping on entry
// is not flapping any more. The rules run in contract order, so a class that
// starts on history whose newest change is already older than the stable
// time stops again on the same check (no episode) and drops that history.
func evaluateFlap(changes []time.Time, flapping bool, ts time.Time, p Policy) (out []time.Time, nowFlapping bool, stopped string) {
	cutoff := ts.Add(-time.Duration(p.FlapWindowMinutes) * time.Minute)
	out = changes[:0]
	for _, t := range changes {
		if t.After(cutoff) {
			out = append(out, t)
		}
	}
	if len(out) > maxChanges {
		out = out[len(out)-maxChanges:]
	}
	if out == nil {
		out = []time.Time{}
	}

	if !p.FlapDetectionEnabled {
		// Change times keep being recorded so re-enabling sees real history.
		if flapping {
			return out, false, EndDisabled
		}
		return out, false, ""
	}
	was := flapping
	if !flapping && len(out) >= p.FlapStartChanges {
		flapping = true
	}
	if flapping {
		stableBefore := ts.Add(-time.Duration(p.FlapStableMinutes) * time.Minute)
		quiet := len(out) > 0 && !out[len(out)-1].After(stableBefore)
		if len(out) <= p.FlapStopChanges || quiet {
			flapping = false
			if was {
				stopped = EndStable
			}
			if quiet {
				// A stability stop (no change for flap_stable_minutes)
				// forgets the class's history: a restart needs
				// flap_start_changes fresh changes. A count stop keeps it.
				out = []time.Time{}
			}
		}
	}
	return out, flapping, stopped
}

func flapStop(endReason string) Event {
	return Event{Type: EventFlapStop, EndReason: endReason}
}

// keepNotAfter drops change times after ts (a central clock step).
func keepNotAfter(changes []time.Time, ts time.Time) []time.Time {
	out := changes[:0]
	for _, t := range changes {
		if !t.After(ts) {
			out = append(out, t)
		}
	}
	if out == nil {
		out = []time.Time{}
	}
	return out
}

func timePtr(t time.Time) *time.Time {
	return &t
}
