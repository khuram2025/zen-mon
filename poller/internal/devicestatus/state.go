package devicestatus

import (
	"math/bits"
	"time"
)

// Hard statuses, the maintenance overlay and the derived flap kinds.
const (
	StatusUp          = "up"
	StatusDegraded    = "degraded"
	StatusDown        = "down"
	StatusUnknown     = "unknown"
	StatusMaintenance = "maintenance"

	KindReachability = "reachability"
	KindPerformance  = "performance"
	KindBoth         = "both"
)

// Reseed and flap-stop reasons (device_flap_episodes.end_reason).
const (
	EndStable      = "stable"
	EndDisabled    = "disabled"
	EndMaintenance = "maintenance"
	EndOwnerChange = "owner_change"
	EndReseed      = "reseed"
	EndNotPolled   = "not_polled"
)

// streakCap is where fail/ok/clear streaks saturate.
const streakCap = 100

// maxChanges bounds each change-time list.
const maxChanges = 64

// State is the persisted machine state of one device (device_status_state).
// The JSON names are the column names; Owner is the row owner ('central' or
// the owning sensor's UUID) and takes no part in Step.
type State struct {
	Owner         string      `json:"owner,omitempty"`
	HardStatus    string      `json:"hard_status"`
	InMaintenance bool        `json:"in_maintenance"`
	StatusSince   *time.Time  `json:"status_since"`
	FailStreak    int         `json:"fail_streak"`
	OkStreak      int         `json:"ok_streak"`
	ClearStreak   int         `json:"clear_streak"`
	WindowBits    int         `json:"window_bits"`
	WindowLen     int         `json:"window_len"`
	ReachChanges  []time.Time `json:"reach_changes"`
	PerfChanges   []time.Time `json:"perf_changes"`
	ReachFlapping bool        `json:"reach_flapping"`
	PerfFlapping  bool        `json:"perf_flapping"`
	FlappingSince *time.Time  `json:"flapping_since"`
	LastSampleAt  *time.Time  `json:"last_sample_at"`
}

// Displayed is the status shown on the device: the maintenance overlay wins
// over the hard status.
func (s State) Displayed() string {
	if s.InMaintenance {
		return StatusMaintenance
	}
	return s.HardStatus
}

// IsFlapping is the device-level flag (devices.is_flapping).
func (s State) IsFlapping() bool {
	return s.ReachFlapping || s.PerfFlapping
}

// FlapKind is the derived devices.flap_kind, "" when not flapping.
func (s State) FlapKind() string {
	switch {
	case s.ReachFlapping && s.PerfFlapping:
		return KindBoth
	case s.ReachFlapping:
		return KindReachability
	case s.PerfFlapping:
		return KindPerformance
	default:
		return ""
	}
}

// BadInWindow returns the bad checks among the newest min(window_len, size)
// window entries and that effective length.
func (s State) BadInWindow(p Policy) (bad, eff int) {
	return windowRead(uint32(s.WindowBits), s.WindowLen, p.DegradedWindowChecks)
}

// clone copies the change lists so Step never writes into the caller's
// backing arrays. Time pointers are shared: Step replaces, never mutates them.
// The lists are never nil (the columns are NOT NULL arrays).
func (s State) clone() State {
	s.ReachChanges = append(make([]time.Time, 0, len(s.ReachChanges)), s.ReachChanges...)
	s.PerfChanges = append(make([]time.Time, 0, len(s.PerfChanges)), s.PerfChanges...)
	return s
}

// windowMask is (1<<n)-1 for the ring width n, computed without shifting by a
// negative or oversized count.
func windowMask(n int) uint32 {
	switch {
	case n <= 0:
		return 0
	case n >= 32:
		return ^uint32(0)
	default:
		return (uint32(1) << uint(n)) - 1
	}
}

// windowPush appends one check (bit 0 = newest) to a ring of size entries.
func windowPush(bitsIn uint32, n, size int, bad bool) (uint32, int) {
	var b uint32
	if bad {
		b = 1
	}
	return ((bitsIn << 1) | b) & windowMask(size), min(n+1, max(size, 0))
}

// windowRead counts the bad entries among the newest min(n, size).
func windowRead(bitsIn uint32, n, size int) (bad, eff int) {
	eff = max(min(n, size), 0)
	return bits.OnesCount32(bitsIn & windowMask(eff)), eff
}

func (s *State) push(bad bool, p Policy) {
	b, n := windowPush(uint32(s.WindowBits), s.WindowLen, p.DegradedWindowChecks, bad)
	s.WindowBits, s.WindowLen = int(b), n
}

func (s *State) clearWindow() {
	s.WindowBits, s.WindowLen = 0, 0
}

// Seed builds the state for a device whose row is missing or must be
// replaced (see NeedsReseed): the visible status is taken over as confirmed,
// everything else starts empty. A reseed never emits events.
func Seed(devicesStatus string, statusChangedAt *time.Time, owner string) State {
	s := State{
		Owner:        owner,
		HardStatus:   StatusUnknown,
		ReachChanges: []time.Time{},
		PerfChanges:  []time.Time{},
	}
	switch devicesStatus {
	case StatusUp, StatusDegraded, StatusDown:
		s.HardStatus = devicesStatus
	}
	s.InMaintenance = devicesStatus == StatusMaintenance
	if statusChangedAt != nil {
		t := *statusChangedAt
		s.StatusSince = &t
	}
	return s
}

// NeedsReseed reports whether the persisted row (nil when missing) must be
// replaced by Seed before the next Step: the row is missing, belongs to
// another owner, or another writer changed devices.status behind it (any
// status other than maintenance that differs from the row's hard status).
// endReason is the end_reason for closing an open flap episode: owner_change
// when the owner differs, reseed otherwise.
func NeedsReseed(row *State, devicesStatus, owner string) (reseed bool, endReason string) {
	switch {
	case row == nil:
		return true, EndReseed
	case row.Owner != owner:
		return true, EndOwnerChange
	case devicesStatus != StatusMaintenance && row.HardStatus != devicesStatus:
		return true, EndReseed
	default:
		return false, ""
	}
}

// PendingChange describes a hard change that is collecting confirmation
// (status-explanation `pending`).
type PendingChange struct {
	Target   string `json:"target"`
	Count    int    `json:"count"`
	Required int    `json:"required"`
}

// Pending reports the hard change the machine is currently confirming, or
// nil. A pending DOWN (failed checks) outranks a pending DEGRADED, as DOWN
// outranks DEGRADED in Step. source selects the down confirmation (the
// owner's check source: SourceSensor, or anything else for central).
func Pending(s State, p Policy, source string) *PendingChange {
	if s.HardStatus == StatusDown {
		if s.OkStreak > 0 {
			return &PendingChange{Target: StatusUp, Count: s.OkStreak, Required: p.UpConfirmChecks}
		}
		return nil
	}
	if s.FailStreak > 0 {
		return &PendingChange{Target: StatusDown, Count: s.FailStreak, Required: p.DownConfirm(source)}
	}
	switch s.HardStatus {
	case StatusUp:
		if bad, _ := s.BadInWindow(p); bad > 0 {
			return &PendingChange{Target: StatusDegraded, Count: bad, Required: p.DegradedConfirmChecks}
		}
	case StatusDegraded:
		if s.ClearStreak > 0 {
			return &PendingChange{Target: StatusUp, Count: s.ClearStreak, Required: p.DegradedClearChecks}
		}
	}
	return nil
}
