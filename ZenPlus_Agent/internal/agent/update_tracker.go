package agent

import (
	"context"
	"encoding/json"
	"errors"
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

// A controller-requested change of the installation (an upgrade, or a
// monitoring-profile change that swaps the core and full packages) ends with
// this process being stopped by the installer, so its progress cannot live
// only in memory. The attempt is written to disk before Setup starts. On the
// next start the agent can tell whether the requested installation is now
// running (success) or Setup rolled back (failure, with the tail of Setup's
// log as the reason), report it, and hold off instead of retrying a failing
// change on every heartbeat.

var (
	errChangeInProgress = errors.New("another installation change is already in progress")
	errRetryCooldown    = errors.New("the same change failed recently and is retried after the cooldown")
)

const (
	updateRetryCooldown = time.Hour
	updateLogTailBytes  = 600

	changeUpgrade = "upgrade"
	changeProfile = "profile"
)

type updateAttempt struct {
	Kind          string    `json:"kind,omitempty"`
	TargetVersion string    `json:"target_version"`
	FromVersion   string    `json:"from_version"`
	TargetFlavor  string    `json:"target_flavor,omitempty"`
	TargetProfile string    `json:"target_profile,omitempty"`
	StartedAt     time.Time `json:"started_at"`
	LogPath       string    `json:"log_path,omitempty"`
	// Error is set when the attempt failed before Setup was launched.
	Error string `json:"error,omitempty"`
	// SetupLaunched distinguishes "Setup ran but the old installation is
	// still running" (it rolled back) from failures before download completed.
	SetupLaunched bool `json:"setup_launched,omitempty"`
}

// key identifies a requested change for the retry cooldown. Attempts written
// by 1.14.1 have no kind and are upgrades.
func (a updateAttempt) key() string {
	kind := a.Kind
	if kind == "" {
		kind = changeUpgrade
	}
	if kind == changeProfile {
		// The version of a profile change is resolved later, so it is not
		// part of what was requested.
		return kind + "|" + a.TargetFlavor + "|" + a.TargetProfile
	}
	return kind + "|" + a.TargetVersion
}

type updateTracker struct {
	statePath string
	logDir    string
	// configPath is the configuration a profile change rewrites.
	configPath string
	current    string
	now        func() time.Time
	// flavor reports the installed package; replaced in tests.
	flavor func() string

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
		flavor:    selfupdate.InstalledFlavor,
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

func (t *updateTracker) set(a updateAttempt, state, errText string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.status = &model.UpdateStatus{
		Kind: kindOf(a), State: state, TargetVersion: a.TargetVersion, FromVersion: t.current,
		TargetFlavor: a.TargetFlavor, TargetProfile: a.TargetProfile,
		Error: truncateUpdateError(errText), UpdatedAt: t.now(),
	}
}

func kindOf(a updateAttempt) string {
	if a.Kind == "" {
		return changeUpgrade
	}
	return a.Kind
}

// Record reports a change that completed without Setup, such as a profile
// change that only rewrote the configuration.
func (t *updateTracker) Record(kind, profile, flavor string) {
	if t == nil {
		return
	}
	t.set(updateAttempt{Kind: kind, TargetVersion: t.current, TargetFlavor: flavor, TargetProfile: profile}, "succeeded", "")
}

// Fail reports a change refused before anything was downloaded.
func (t *updateTracker) Fail(kind, profile, flavor, reason string) {
	if t == nil {
		return
	}
	t.set(updateAttempt{Kind: kind, TargetVersion: t.current, TargetFlavor: flavor, TargetProfile: profile}, "failed", reason)
}

// outcomeOfPreviousAttempt reads the attempt recorded before a restart.
func (t *updateTracker) outcomeOfPreviousAttempt() *model.UpdateStatus {
	a, ok := t.load()
	if !ok {
		return nil
	}
	status := &model.UpdateStatus{
		Kind: kindOf(a), TargetVersion: a.TargetVersion, FromVersion: a.FromVersion,
		TargetFlavor: a.TargetFlavor, TargetProfile: a.TargetProfile, UpdatedAt: t.now(),
	}
	reached := a.TargetVersion == t.current && (a.TargetFlavor == "" || a.TargetFlavor == t.flavor())
	switch {
	case reached:
		// A profile change is only requested for a different package, so
		// reaching its flavour means Setup completed.
		status.State = "succeeded"
		_ = os.Remove(t.statePath)
	case a.Error != "":
		status.State = "failed"
		status.Error = a.Error
	case a.SetupLaunched:
		status.State = "failed"
		status.Error = "Setup did not complete and the previous installation was restored"
		if tail := readTail(a.LogPath, updateLogTailBytes); tail != "" {
			status.Error += ": " + tail
		}
	default:
		status.State = "failed"
		status.Error = "the agent restarted before the change could start"
	}
	status.Error = truncateUpdateError(status.Error)
	return status
}

// Request starts an upgrade to target unless one is running or target is
// already installed. A failed target is retried after the cooldown, or at
// once when an operator asks explicitly (the upgrade_agent command).
func (t *updateTracker) Request(ctx context.Context, api *client.Client, channel, target string, explicit bool, logf func(string, ...any)) error {
	if target == "" || target == t.current {
		return nil
	}
	attempt := updateAttempt{Kind: changeUpgrade, TargetVersion: target, FromVersion: t.current, TargetFlavor: t.flavor()}
	if !selfupdate.IsNewer(target, t.current) {
		err := fmt.Errorf("refusing to downgrade from %s to %s", t.current, target)
		t.set(attempt, "failed", err.Error())
		return err
	}
	return t.start(ctx, attempt, explicit, logf, func() (selfupdate.Manifest, error) {
		return selfupdate.FetchPublishedVersion(ctx, api, channel, attempt.TargetFlavor, target)
	}, func(m selfupdate.Manifest, logPath string) error {
		return selfupdate.ApplyWithOptions(ctx, api, m, t.current, selfupdate.ApplyOptions{LogPath: logPath, IgnoreCooldown: explicit}, logf)
	})
}

// ChangePackage installs the given package flavour with profile, for a
// monitoring-profile change that adds or removes the APM components. The
// running version is used when the appliance publishes it in that flavour;
// otherwise the newest published one, never an older one.
func (t *updateTracker) ChangePackage(ctx context.Context, api *client.Client, channel, flavor, profile string, logf func(string, ...any)) error {
	attempt := updateAttempt{Kind: changeProfile, TargetVersion: t.current, FromVersion: t.current, TargetFlavor: flavor, TargetProfile: profile}
	return t.start(ctx, attempt, true, logf, func() (selfupdate.Manifest, error) {
		m, err := selfupdate.FetchPublishedVersion(ctx, api, channel, flavor, t.current)
		if err == nil {
			return m, nil
		}
		if !errors.Is(err, selfupdate.ErrVersionUnavailable) {
			return selfupdate.Manifest{}, err
		}
		latest, latestErr := selfupdate.FetchPublishedManifest(ctx, api, channel, flavor)
		if latestErr != nil {
			return selfupdate.Manifest{}, fmt.Errorf("no %s package is published on the appliance: %w", flavorName(flavor), latestErr)
		}
		if selfupdate.IsNewer(t.current, latest.LatestVersion) {
			return selfupdate.Manifest{}, fmt.Errorf("the appliance publishes the %s package only at %s, older than the running %s; publish %s or newer",
				flavorName(flavor), latest.LatestVersion, t.current, t.current)
		}
		return latest, nil
	}, func(m selfupdate.Manifest, logPath string) error {
		return selfupdate.ApplyWithOptions(ctx, api, m, t.current,
			selfupdate.ApplyOptions{LogPath: logPath, Profile: profile, SameVersion: true, IgnoreCooldown: true}, logf)
	})
}

func flavorName(flavor string) string {
	if flavor == selfupdate.FlavorCore {
		return "server-monitoring"
	}
	return "APM"
}

// start runs fetch then apply in the background, recording the attempt on
// disk and in the reported status at each step. Without explicit, a change
// that failed within the cooldown is not retried.
func (t *updateTracker) start(ctx context.Context, attempt updateAttempt, explicit bool, logf func(string, ...any),
	fetch func() (selfupdate.Manifest, error), apply func(selfupdate.Manifest, string) error) error {
	t.mu.Lock()
	if t.inFlight {
		t.mu.Unlock()
		return errChangeInProgress
	}
	if a, ok := t.load(); ok && !explicit && a.key() == attempt.key() && t.now().Sub(a.StartedAt) < updateRetryCooldown {
		t.mu.Unlock()
		return errRetryCooldown // its failure is already being reported
	}
	t.inFlight = true
	t.mu.Unlock()

	attempt.StartedAt = t.now()
	t.save(attempt)
	t.set(attempt, "downloading", "")
	logf("self-update: %s requested by the appliance (version %s, package %s, profile %s)",
		kindOf(attempt), attempt.TargetVersion, firstNonEmpty(attempt.TargetFlavor, "unchanged"), firstNonEmpty(attempt.TargetProfile, "unchanged"))
	go func() {
		defer func() {
			t.mu.Lock()
			t.inFlight = false
			t.mu.Unlock()
		}()
		fail := func(err error) {
			attempt.Error = err.Error()
			t.save(attempt)
			t.set(attempt, "failed", attempt.Error)
			logf("self-update: %s failed: %v", kindOf(attempt), err)
		}
		m, err := fetch()
		if err != nil {
			fail(err)
			return
		}
		attempt.TargetVersion = m.LatestVersion
		attempt.LogPath = filepath.Join(t.logDir, "update-"+m.LatestVersion+".log")
		_ = os.MkdirAll(t.logDir, 0o700)
		attempt.SetupLaunched = true
		t.save(attempt)
		t.set(attempt, "installing", "")
		if err := apply(m, attempt.LogPath); err != nil {
			attempt.SetupLaunched = false
			fail(err)
		}
	}()
	return nil
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
