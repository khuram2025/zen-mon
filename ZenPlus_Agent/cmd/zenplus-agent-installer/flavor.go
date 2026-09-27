//go:build windows

package main

import (
	"fmt"
	"strings"

	"zenplus-agent/internal/config"
)

const (
	// flavorCore installs server monitoring only.
	flavorCore = "core"
	// flavorFull adds the offline APM bundle. It keeps the historical package
	// name so agents released before the split upgrade to a package that still
	// contains the APM components they may be using.
	flavorFull = "full"
)

// installerFlavor is set by scripts\build.ps1 with
// -X main.installerFlavor=core|full.
var installerFlavor = flavorFull

func payloadIncludesAPM() bool {
	return strings.TrimSpace(installerFlavor) != flavorCore
}

func flavorDisplayName() string {
	if payloadIncludesAPM() {
		return "Server monitoring and APM"
	}
	return "Server monitoring"
}

// requireFlavorSupportsConfig stops the core package from replacing an
// installation that still relies on APM. Installing core removes the APM
// bundle, so an APM profile would silently stop, and applications that
// ZenPlus instrumented would fail to load their profiler on the next restart.
func requireFlavorSupportsConfig(cfg config.Config, managedTargets int) error {
	if payloadIncludesAPM() {
		return nil
	}
	if managedTargets > 0 {
		return fmt.Errorf("this server has %d application(s) instrumented by ZenPlus APM; uninstrument them in APM, or install the ZenPlus Agent package with APM instead of the server-monitoring package", managedTargets)
	}
	profile := strings.ToLower(strings.TrimSpace(cfg.APM.Profile))
	if cfg.APM.Enabled && profile != "infrastructure" {
		// A command-line problem for deployment tools: the wrong package
		// or profile was chosen.
		return withExitCode(exitInvalidCommandLine, fmt.Errorf("the %q monitoring profile needs APM, which this server-monitoring package does not include; install the ZenPlus Agent package with APM, or choose the infrastructure profile", profile))
	}
	return nil
}

// uninstallerPayloadName is the payload entry, in canonical lower case, that
// carries the payload-free setup build installed as ZenPlusAgentSetup.exe.
const uninstallerPayloadName = "zenplusagentsetup.exe"

// agentctlShimName keeps the zenplus-agentctl command name working now that
// its subcommands live in zenplus-agent.exe.
const agentctlShimName = "zenplus-agentctl.cmd"

const agentctlShim = "@\"%~dp0zenplus-agent.exe\" %*\r\n"
