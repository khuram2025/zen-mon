//go:build linux

package collectors

import (
	"context"
	"os"
	"os/exec"
	"time"
)

// systemdServiceTimeout bounds the systemctl calls of one collection.
const systemdServiceTimeout = 15 * time.Second

// Seams for tests; production reads systemd.
var (
	runSystemctl   commandRunner = execRunner
	systemdBooted                = func() bool { return dirExists("/run/systemd/system") }
	lookupExecPath               = exec.LookPath
)

// listServices reads the systemd service units. A host that was not booted
// with systemd (sd_booted: /run/systemd/system is absent) has no unit state
// to report and returns errNoSystemd, which collectServices treats as
// "nothing to collect" rather than a failure.
func listServices() ([]serviceInfo, error) {
	if !systemdBooted() {
		return nil, errNoSystemd
	}
	if _, err := lookupExecPath("systemctl"); err != nil {
		return nil, errNoSystemd
	}
	ctx, cancel := context.WithTimeout(context.Background(), systemdServiceTimeout)
	defer cancel()
	return listSystemdServices(ctx, runSystemctl)
}

func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}
