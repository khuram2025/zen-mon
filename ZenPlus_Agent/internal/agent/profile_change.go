package agent

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	apmruntime "zenplus-agent/internal/apm"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/selfupdate"
	"zenplus-agent/internal/uploader"
)

// Monitoring profiles and the package each needs: server monitoring runs on
// the small core package; any profile with APM needs the full package. A
// change between them from Agent Fleet swaps the package so a server that no
// longer uses APM does not keep its components, and one that starts using it
// gets them.

// profileChangePlan is what switching to Profile needs on this installation.
type profileChangePlan struct {
	Profile string
	// Flavor is the package the profile needs.
	Flavor string
	// SwapPackage is set when the installed package must be replaced.
	SwapPackage bool
}

func normalizeProfile(profile string) (string, error) {
	profile = strings.ToLower(strings.TrimSpace(profile))
	switch profile {
	case "infrastructure", "apm", "combined":
		return profile, nil
	}
	return "", fmt.Errorf("monitoring profile must be infrastructure, apm or combined, not %q", profile)
}

func planProfileChange(profile, installedFlavor string) profileChangePlan {
	flavor := selfupdate.FlavorFull
	if profile == "infrastructure" {
		flavor = selfupdate.FlavorCore
	}
	return profileChangePlan{Profile: profile, Flavor: flavor, SwapPackage: flavor != installedFlavor}
}

// effectiveProfile is the profile the agent runs, for reporting.
func effectiveProfile(cfg config.Config) string {
	if profile, err := normalizeProfile(cfg.APM.Profile); err == nil {
		if profile != "infrastructure" && !cfg.APM.Enabled {
			return "infrastructure"
		}
		return profile
	}
	if cfg.APM.Enabled {
		return "combined"
	}
	return "infrastructure"
}

// Replaced in tests.
var (
	canChangeInstallation   = canManageApplicationInstrumentation
	installedFlavor         = selfupdate.InstalledFlavor
	managedInstrumentation  = apmruntime.ManagedInstrumentationCount
	rollbackInstrumentation = apmruntime.RollbackAll
)

// changeMonitoringProfile handles the set_monitoring_profile command.
func changeMonitoringProfile(ctx context.Context, cmd model.Command, cfg *config.Config, paths runtime.Paths, up *uploader.Uploader, updates *updateTracker, logf func(string, ...any)) model.CommandResult {
	profile, err := normalizeProfile(paramString(cmd.Params, "profile"))
	if err != nil {
		return model.CommandResult{Success: false, ErrorMessage: err.Error()}
	}
	if !canChangeInstallation() {
		return model.CommandResult{Success: false, ErrorMessage: "changing the monitoring profile remotely requires the all-users Windows service installation"}
	}
	if cfg == nil || updates == nil || updates.configPath == "" {
		return model.CommandResult{Success: false, ErrorMessage: "profile changes are unavailable in this run mode"}
	}
	flavor := installedFlavor()
	plan := planProfileChange(profile, flavor)
	fail := func(reason string) model.CommandResult {
		updates.Fail(changeProfile, profile, plan.Flavor, reason)
		return model.CommandResult{Success: false, ErrorMessage: reason}
	}

	// Applications ZenPlus instrumented load the profiler from the APM
	// components. Before APM is turned off they are restored to their
	// original settings, which restarts them, so this needs explicit consent.
	restored := 0
	if profile == "infrastructure" {
		count, err := managedInstrumentation(paths.APMInstrumentationState)
		if err != nil {
			return fail("check applications instrumented by APM: " + err.Error())
		}
		if count > 0 {
			if !paramBool(cmd.Params, "remove_instrumentation", false) {
				return fail(fmt.Sprintf("%d application(s) on this server are instrumented by ZenPlus APM. Choose to remove their instrumentation (they are restarted) to turn APM off.", count))
			}
			exe, err := os.Executable()
			if err != nil {
				return fail("locate the agent installation: " + err.Error())
			}
			rollbackCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
			err = rollbackInstrumentation(rollbackCtx, paths.APMInstrumentationState, filepath.Dir(exe), true)
			cancel()
			if err != nil {
				return fail("remove APM instrumentation: " + err.Error())
			}
			restored = count
			logf("monitoring profile: removed APM instrumentation from %d application(s)", count)
		}
	}

	output := map[string]any{"profile": profile, "package": plan.Flavor, "instrumentation_removed": restored}
	if !plan.SwapPackage {
		// The installed package already fits; only the configuration
		// changes, and the running agent picks it up on its next settings
		// check.
		if err := config.SetProfileInFile(updates.configPath, profile); err != nil {
			return fail("save the monitoring profile: " + err.Error())
		}
		updates.Record(changeProfile, profile, flavor)
		logf("monitoring profile changed to %s by the appliance", profile)
		output["status"] = "profile applied"
		return model.CommandResult{Success: true, Output: output}
	}
	if up == nil {
		return fail("the appliance connection is unavailable")
	}
	if err := updates.ChangePackage(ctx, up.Client(), cfg.UpdateRing, plan.Flavor, profile, logf); err != nil {
		return fail(err.Error())
	}
	output["status"] = "package change started"
	return model.CommandResult{Success: true, Output: output}
}
