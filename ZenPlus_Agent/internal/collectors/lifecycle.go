package collectors

// Server restart and shutdown evidence for hosts whose operating system log
// the agent does not read (Linux). Windows reports these through its System
// event log (Kernel-General 12/13, Kernel-Power 41); here the agent records
// them itself, in the same event_detail shape and with the same event IDs, so
// the controller's host_restart and host_shutdown rules apply unchanged:
//
//	12  the operating system started (a new boot was found at agent start)
//	13  the operating system is shutting down (the agent was stopped while
//	    systemd reported the system as "stopping")
//	41  the previous boot ended without a clean shutdown (the agent was still
//	    running in that boot and never saw it stop: power loss, crash, reset)
//
// A small state file in the agent's state directory remembers the boot the
// agent last ran in and how it stopped.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"zenplus-agent/internal/model"
)

const (
	// lifecycleProvider is the event source the controller recognises for
	// agent-recorded lifecycle evidence (server_event_service.lifecycle_kind).
	lifecycleProvider = "zenplus-agent"
	lifecycleChannel  = "System"

	lifecycleEventStarted    = 12
	lifecycleEventShutdown   = 13
	lifecycleEventUnexpected = 41

	lifecycleStateFile = "lifecycle.json"

	// Boot times read from /proc/stat can move by a second as the clock is
	// disciplined; the controller uses the same two-minute tolerance.
	sameBootTolerance = 120
)

// How the agent last left a boot.
const (
	lifecycleRunning        = "running"         // started; no stop seen yet
	lifecycleAgentStopped   = "agent_stopped"   // the agent alone was stopped
	lifecycleSystemShutdown = "system_shutdown" // stopped by a system shutdown
)

type bootInfo struct {
	ID   string // /proc/sys/kernel/random/boot_id, when readable
	Time int64  // boot time, seconds since the epoch
}

type lifecycleState struct {
	BootID    string    `json:"boot_id,omitempty"`
	BootTime  int64     `json:"boot_time"`
	State     string    `json:"state"`
	UpdatedAt time.Time `json:"updated_at"`
}

func sameBoot(prev lifecycleState, cur bootInfo) bool {
	if prev.BootID != "" && cur.ID != "" {
		return prev.BootID == cur.ID
	}
	d := prev.BootTime - cur.Time
	return d <= sameBootTolerance && d >= -sameBootTolerance
}

func formatLifecycleTime(t time.Time) string {
	return t.UTC().Format("2006-01-02 15:04:05 UTC")
}

// lifecycleEvent builds one event_detail row in the shape decodeDetailedEvent
// produces for Windows events. The record ID is derived from the event time
// and ID so a re-sent event keeps its identity on the controller.
func lifecycleEvent(eventID int, at time.Time, level, message, computer string, fields map[string]string) map[string]any {
	at = at.UTC().Truncate(time.Second)
	stamp := at.Format(time.RFC3339)
	record := uint64(at.Unix())*100 + uint64(eventID)
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s\x00%d\x00%s", strings.ToLower(lifecycleChannel), record, stamp)))
	if fields == nil {
		fields = map[string]string{}
	}
	return map[string]any{
		"event_uid":         hex.EncodeToString(sum[:]),
		"log_name":          lifecycleChannel,
		"event_id":          eventID,
		"record_id":         record,
		"event_time":        stamp,
		"provider":          lifecycleProvider,
		"level":             level,
		"computer":          truncateUTF8(computer, 255),
		"message":           message,
		"message_truncated": false,
		"fields":            fields,
	}
}

// bootEvidence is what a new boot proves about the previous one. Nothing is
// reported on the agent's first run (no previous boot is known) or when the
// agent restarts within the same boot.
func bootEvidence(prev *lifecycleState, cur bootInfo, computer string) []map[string]any {
	if prev == nil || prev.BootTime <= 0 || sameBoot(*prev, cur) {
		return nil
	}
	boot := time.Unix(cur.Time, 0).UTC()
	previous := time.Unix(prev.BootTime, 0).UTC()
	fields := map[string]string{
		"boot_time":          boot.Format(time.RFC3339),
		"previous_boot_time": previous.Format(time.RFC3339),
		"source":             "/proc/stat btime",
	}
	if cur.ID != "" {
		fields["boot_id"] = cur.ID
	}
	events := []map[string]any{lifecycleEvent(lifecycleEventStarted, boot, "information",
		fmt.Sprintf("The operating system started at %s.", formatLifecycleTime(boot)), computer, fields)}
	if prev.State == lifecycleRunning {
		unexpected := map[string]string{
			"boot_time":           boot.Format(time.RFC3339),
			"previous_boot_time":  previous.Format(time.RFC3339),
			"previous_agent_seen": prev.UpdatedAt.UTC().Format(time.RFC3339),
		}
		events = append(events, lifecycleEvent(lifecycleEventUnexpected, boot, "critical",
			fmt.Sprintf("The previous boot (started %s) ended without a clean shutdown: the agent was still running "+
				"and was never stopped by a system shutdown, which points to a power loss, a crash or a forced reset. "+
				"The operating system started again at %s.", formatLifecycleTime(previous), formatLifecycleTime(boot)),
			computer, unexpected))
	}
	return events
}

// lifecycleTracker owns the state file and the evidence not yet queued.
type lifecycleTracker struct {
	mu           sync.Mutex
	path         string
	boot         bootInfo
	computer     string
	pending      []map[string]any
	offered      int
	armed        bool // the state file says "running" for this boot
	now          func() time.Time
	shuttingDown func(context.Context) bool
}

func (t *lifecycleTracker) start(readBoot func() (bootInfo, error)) error {
	boot, err := readBoot()
	if err != nil {
		return err
	}
	t.boot = boot
	var prev *lifecycleState
	if raw, err := os.ReadFile(t.path); err == nil {
		var state lifecycleState
		if json.Unmarshal(raw, &state) == nil {
			prev = &state
		}
	}
	t.pending = bootEvidence(prev, boot, t.computer)
	if len(t.pending) == 0 {
		// Same boot or first run: arm now, so a crash from here on is seen.
		return t.writeState(lifecycleRunning)
	}
	return nil
}

func (t *lifecycleTracker) writeState(state string) error {
	body, err := json.Marshal(lifecycleState{BootID: t.boot.ID, BootTime: t.boot.Time, State: state, UpdatedAt: t.now().UTC()})
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(t.path), 0o700); err != nil {
		return err
	}
	tmp := t.path + ".tmp"
	if err := os.WriteFile(tmp, body, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, t.path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	t.armed = state == lifecycleRunning
	return nil
}

// metrics offers the pending evidence to a collection.
func (t *lifecycleTracker) metrics() []model.Metric {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.offered = len(t.pending)
	out := make([]model.Metric, 0, len(t.pending))
	for _, data := range t.pending {
		out = append(out, model.Metric{Kind: "event_detail", Timestamp: t.now().UTC(), Data: data})
	}
	return out
}

// queued records that the last offered evidence is durably spooled, and
// only then marks this boot as reported.
func (t *lifecycleTracker) queued() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.offered > 0 {
		t.pending = t.pending[t.offered:]
		t.offered = 0
	}
	if len(t.pending) == 0 && !t.armed {
		return t.writeState(lifecycleRunning)
	}
	return nil
}

// stop records how the agent left this boot and, when the system itself is
// shutting down, returns the shutdown evidence to send.
func (t *lifecycleTracker) stop(ctx context.Context) ([]model.Metric, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.armed {
		// This boot's start evidence never reached the spool: leave the
		// previous state so the next start reports it again.
		return nil, nil
	}
	if !t.shuttingDown(ctx) {
		return nil, t.writeState(lifecycleAgentStopped)
	}
	at := t.now().UTC()
	fields := map[string]string{"system_state": "stopping", "boot_time": time.Unix(t.boot.Time, 0).UTC().Format(time.RFC3339)}
	if t.boot.ID != "" {
		fields["boot_id"] = t.boot.ID
	}
	data := lifecycleEvent(lifecycleEventShutdown, at, "information",
		fmt.Sprintf("The operating system began shutting down at %s: systemd reported the system as stopping when the agent was stopped.",
			formatLifecycleTime(at)), t.computer, fields)
	return []model.Metric{{Kind: "event_detail", Timestamp: at, Data: data}}, t.writeState(lifecycleSystemShutdown)
}

var lifecycleTrack struct {
	sync.Mutex
	t *lifecycleTracker
}

func currentLifecycle() *lifecycleTracker {
	lifecycleTrack.Lock()
	defer lifecycleTrack.Unlock()
	return lifecycleTrack.t
}

// StartLifecycle begins restart/shutdown tracking for the running agent
// service on platforms without an OS event log source (Linux). It is a no-op
// elsewhere, and must not be called by one-shot commands that run beside the
// service.
func StartLifecycle(stateDir string) {
	if !lifecycleSupported {
		return
	}
	computer, _ := os.Hostname()
	t := &lifecycleTracker{
		path:         filepath.Join(stateDir, lifecycleStateFile),
		computer:     computer,
		now:          time.Now,
		shuttingDown: systemShuttingDown,
	}
	if err := t.start(readBootInfo); err != nil {
		logf("restart/shutdown tracking unavailable: %v", err)
		return
	}
	if n := len(t.pending); n > 0 {
		logf("new boot detected; %d restart event(s) queued for the controller", n)
	}
	lifecycleTrack.Lock()
	lifecycleTrack.t = t
	lifecycleTrack.Unlock()
}

// LifecycleQueued tells the tracker the collection that carried its
// evidence was durably spooled.
func LifecycleQueued() {
	if t := currentLifecycle(); t != nil {
		if err := t.queued(); err != nil {
			logf("restart/shutdown state not saved: %v", err)
		}
	}
}

// StopLifecycle records the agent's stop and returns shutdown evidence when
// the operating system is shutting down.
func StopLifecycle(ctx context.Context) []model.Metric {
	t := currentLifecycle()
	if t == nil {
		return nil
	}
	metrics, err := t.stop(ctx)
	if err != nil {
		logf("restart/shutdown state not saved: %v", err)
	}
	return metrics
}

func lifecycleMetrics() []model.Metric {
	if t := currentLifecycle(); t != nil {
		return t.metrics()
	}
	return nil
}
