package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/selfupdate"
)

// A controller-requested upgrade ends with this process being stopped by the
// installer, so its progress cannot live only in memory. The attempt is
// written to disk before Setup starts. On the next start the agent can tell
// whether the new version is now running (success) or Setup rolled back
// (failure, with the tail of Setup's log as the reason), report it, and hold
// off instead of retrying a failing upgrade on every heartbeat.

const (
	updateRetryCooldown = time.Hour
	updateLogTailBytes  = 600
)

type updateAttempt struct {
	TargetVersion string    `json:"target_version"`
	FromVersion   string    `json:"from_version"`
	StartedAt     time.Time `json:"started_at"`
	LogPath       string    `json:"log_path,omitempty"`
	// Error is set when the attempt failed before Setup was launched.
	Error string `json:"error,omitempty"`
	// SetupLaunched distinguishes "Setup ran but the old version is still
	// running" (it rolled back) from failures before download completed.
	SetupLaunched bool `json:"setup_launched,omitempty"`
}

type updateTracker struct {
	statePath string
	logDir    string
	current   string
	now       func() time.Time

	mu       sync.Mutex
	status   *model.UpdateStatus
	inFlight bool
}

func newUpdateTracker(stateDir, logDir, currentVersion string) *updateTracker {
	t := &updateTracker{
		statePath: filepath.Join(stateDir, "update-attempt.json"),
		logDir:    logDir,
		current:   currentVersion,
		now:       func() time.Time { return time.Now().UTC() },
	}
	t.status = t.outcomeOfPreviousAttempt()
	return t
}

// Status is the latest update state to report, or nil when there is none.
func (t *updateTracker) Status() *model.UpdateStatus {
	if t == nil {
		return nil
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.status == nil {
		return nil
	}
	copy := *t.status
	return &copy
}

func (t *updateTracker) set(state, target, errText string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.status = &model.UpdateStatus{
		State: state, TargetVersion: target, FromVersion: t.current,
		Error: truncateUpdateError(errText), UpdatedAt: t.now(),
	}
}

// outcomeOfPreviousAttempt reads the attempt recorded before a restart.
func (t *updateTracker) outcomeOfPreviousAttempt() *model.UpdateStatus {
	a, ok := t.load()
	if !ok {
		return nil
	}
	status := &model.UpdateStatus{TargetVersion: a.TargetVersion, FromVersion: a.FromVersion, UpdatedAt: t.now()}
	switch {
	case a.TargetVersion == t.current:
		status.State = "succeeded"
		_ = os.Remove(t.statePath)
	case a.Error != "":
		status.State = "failed"
		status.Error = a.Error
	case a.SetupLaunched:
		status.State = "failed"
		status.Error = "Setup did not complete and the previous version was restored"
		if tail := readTail(a.LogPath, updateLogTailBytes); tail != "" {
			status.Error += ": " + tail
		}
	default:
		status.State = "failed"
		status.Error = "the agent restarted before the upgrade could start"
	}
	status.Error = truncateUpdateError(status.Error)
	return status
}

// Request starts an upgrade to target unless one is running, target is
// already installed, or the same target failed within the cooldown.
func (t *updateTracker) Request(ctx context.Context, api *client.Client, channel, target string, logf func(string, ...any)) {
	if target == "" || target == t.current {
		return
	}
	if !selfupdate.IsNewer(target, t.current) {
		t.set("failed", target, fmt.Sprintf("refusing to downgrade from %s to %s", t.current, target))
		return
	}
	t.mu.Lock()
	if t.inFlight {
		t.mu.Unlock()
		return
	}
	if a, ok := t.load(); ok && a.TargetVersion == target && t.now().Sub(a.StartedAt) < updateRetryCooldown {
		t.mu.Unlock()
		return // failed recently; its failure is already being reported
	}
	t.inFlight = true
	t.mu.Unlock()

	attempt := updateAttempt{
		TargetVersion: target, FromVersion: t.current, StartedAt: t.now(),
		LogPath: filepath.Join(t.logDir, "update-"+target+".log"),
	}
	t.save(attempt)
	t.set("downloading", target, "")
	logf("self-update: upgrading from %s to %s requested by the appliance", t.current, target)
	go func() {
		defer func() {
			t.mu.Lock()
			t.inFlight = false
			t.mu.Unlock()
		}()
		fail := func(err error) {
			attempt.Error = err.Error()
			t.save(attempt)
			t.set("failed", target, attempt.Error)
			logf("self-update to %s failed: %v", target, err)
		}
		m, err := selfupdate.FetchPublishedVersion(ctx, api, channel, selfupdate.InstalledFlavor(), target)
		if err != nil {
			fail(err)
			return
		}
		_ = os.MkdirAll(t.logDir, 0o700)
		attempt.SetupLaunched = true
		t.save(attempt)
		t.set("installing", target, "")
		if err := selfupdate.ApplyWithLog(ctx, api, m, t.current, attempt.LogPath, logf); err != nil {
			attempt.SetupLaunched = false
			fail(err)
		}
	}()
}

func (t *updateTracker) load() (updateAttempt, bool) {
	var a updateAttempt
	data, err := os.ReadFile(t.statePath)
	if err != nil || json.Unmarshal(data, &a) != nil || a.TargetVersion == "" {
		return updateAttempt{}, false
	}
	return a, true
}

func (t *updateTracker) save(a updateAttempt) {
	data, err := json.Marshal(a)
	if err != nil {
		return
	}
	_ = os.MkdirAll(filepath.Dir(t.statePath), 0o700)
	_ = os.WriteFile(t.statePath, data, 0o600)
}

func readTail(path string, maxBytes int) string {
	if path == "" {
		return ""
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	if len(data) > maxBytes {
		data = data[len(data)-maxBytes:]
	}
	lines := strings.Fields(strings.ReplaceAll(string(data), "\r", " "))
	return strings.Join(lines, " ")
}

func truncateUpdateError(s string) string {
	const max = 1000
	if len(s) > max {
		return s[:max]
	}
	return s
}
