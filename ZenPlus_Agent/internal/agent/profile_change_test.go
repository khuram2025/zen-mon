package agent

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/selfupdate"
)

func TestPlanProfileChangeSwapsPackageOnlyWhenNeeded(t *testing.T) {
	cases := []struct {
		profile, installed, flavor string
		swap                       bool
	}{
		{"infrastructure", selfupdate.FlavorFull, selfupdate.FlavorCore, true},
		{"infrastructure", selfupdate.FlavorCore, selfupdate.FlavorCore, false},
		{"combined", selfupdate.FlavorCore, selfupdate.FlavorFull, true},
		{"combined", selfupdate.FlavorFull, selfupdate.FlavorFull, false},
		{"apm", selfupdate.FlavorFull, selfupdate.FlavorFull, false},
	}
	for _, c := range cases {
		got := planProfileChange(c.profile, c.installed)
		if got.Flavor != c.flavor || got.SwapPackage != c.swap {
			t.Errorf("%s on %s = %+v", c.profile, c.installed, got)
		}
	}
}

// stubProfileChange makes the command runnable in an unprivileged test.
func stubProfileChange(t *testing.T, flavor string, instrumented int, rolledBack *bool) {
	t.Helper()
	prevPriv, prevFlavor, prevCount, prevRollback := canChangeInstallation, installedFlavor, managedInstrumentation, rollbackInstrumentation
	t.Cleanup(func() {
		canChangeInstallation, installedFlavor, managedInstrumentation, rollbackInstrumentation = prevPriv, prevFlavor, prevCount, prevRollback
	})
	canChangeInstallation = func() bool { return true }
	installedFlavor = func() string { return flavor }
	managedInstrumentation = func(string) (int, error) { return instrumented, nil }
	rollbackInstrumentation = func(context.Context, string, string, bool) error {
		*rolledBack = true
		return nil
	}
}

func profileTestTracker(t *testing.T, profile string) (*updateTracker, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "agent.yaml")
	cfg := config.Default()
	if err := config.ApplyProfile(&cfg, profile); err != nil {
		t.Fatal(err)
	}
	if err := config.Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	tr := newUpdateTracker(filepath.Join(dir, "state"), filepath.Join(dir, "logs"), model.AgentVersion)
	tr.configPath = path
	return tr, path
}

func TestProfileChangeWithinPackageRewritesConfig(t *testing.T) {
	rolledBack := false
	stubProfileChange(t, selfupdate.FlavorFull, 0, &rolledBack)
	tr, path := profileTestTracker(t, "combined")
	cfg := config.Default()
	result := changeMonitoringProfile(context.Background(),
		model.Command{Command: "set_monitoring_profile", Params: map[string]any{"profile": "apm"}},
		&cfg, runtime.Paths{}, nil, tr, func(string, ...any) {})
	if !result.Success {
		t.Fatalf("profile change failed: %s", result.ErrorMessage)
	}
	saved, err := config.LoadForEdit(path)
	if err != nil {
		t.Fatal(err)
	}
	if saved.APM.Profile != "apm" {
		t.Fatalf("saved profile = %q", saved.APM.Profile)
	}
	if s := tr.Status(); s == nil || s.Kind != changeProfile || s.State != "succeeded" || s.TargetProfile != "apm" {
		t.Fatalf("reported status = %+v", s)
	}
}

func TestTurningAPMOffNeedsConsentForInstrumentedApplications(t *testing.T) {
	rolledBack := false
	stubProfileChange(t, selfupdate.FlavorCore, 2, &rolledBack)
	tr, path := profileTestTracker(t, "combined")
	cfg := config.Default()
	cmd := model.Command{Command: "set_monitoring_profile", Params: map[string]any{"profile": "infrastructure"}}

	result := changeMonitoringProfile(context.Background(), cmd, &cfg, runtime.Paths{}, nil, tr, func(string, ...any) {})
	if result.Success || !strings.Contains(result.ErrorMessage, "2 application(s)") || rolledBack {
		t.Fatalf("change without consent: success=%v rolledBack=%v %q", result.Success, rolledBack, result.ErrorMessage)
	}
	if s := tr.Status(); s == nil || s.State != "failed" {
		t.Fatalf("refusal not reported: %+v", s)
	}

	cmd.Params["remove_instrumentation"] = true
	result = changeMonitoringProfile(context.Background(), cmd, &cfg, runtime.Paths{}, nil, tr, func(string, ...any) {})
	if !result.Success || !rolledBack {
		t.Fatalf("change with consent: success=%v rolledBack=%v %q", result.Success, rolledBack, result.ErrorMessage)
	}
	saved, _ := config.LoadForEdit(path)
	if saved.APM.Enabled || saved.APM.Profile != "infrastructure" {
		t.Fatalf("APM still enabled: %+v", saved.APM)
	}
}

func TestProfileChangeRejectsUnknownProfiles(t *testing.T) {
	rolledBack := false
	stubProfileChange(t, selfupdate.FlavorFull, 0, &rolledBack)
	tr, _ := profileTestTracker(t, "combined")
	cfg := config.Default()
	result := changeMonitoringProfile(context.Background(),
		model.Command{Command: "set_monitoring_profile", Params: map[string]any{"profile": "everything"}},
		&cfg, runtime.Paths{}, nil, tr, func(string, ...any) {})
	if result.Success {
		t.Fatal("unknown profile accepted")
	}
}

func TestPackageChangeOutcomeNeedsTheNewFlavor(t *testing.T) {
	dir := t.TempDir()
	tr := &updateTracker{statePath: filepath.Join(dir, "update-attempt.json")}
	tr.save(updateAttempt{Kind: changeProfile, TargetVersion: "1.14.2", FromVersion: "1.14.2",
		TargetFlavor: selfupdate.FlavorFull, TargetProfile: "combined", SetupLaunched: true})
	tr.current = "1.14.2"
	tr.now = func() time.Time { return time.Now().UTC() }

	tr.flavor = func() string { return selfupdate.FlavorCore }
	if s := tr.outcomeOfPreviousAttempt(); s.State != "failed" {
		t.Fatalf("still core after Setup: %+v", s)
	}
	tr.flavor = func() string { return selfupdate.FlavorFull }
	if s := tr.outcomeOfPreviousAttempt(); s.State != "succeeded" || s.Kind != changeProfile {
		t.Fatalf("full after Setup: %+v", s)
	}
}
