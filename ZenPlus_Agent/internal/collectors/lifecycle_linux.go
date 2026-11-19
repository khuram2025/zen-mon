//go:build linux

package collectors

import (
	"context"
	"os"
	"strings"
	"time"
)

const lifecycleSupported = true

// Replaced in tests.
var (
	procStat   = "/proc/stat"
	procBootID = "/proc/sys/kernel/random/boot_id"
)

func readBootInfo() (bootInfo, error) {
	raw, err := os.ReadFile(procStat)
	if err != nil {
		return bootInfo{}, err
	}
	btime, err := parseBootTime(raw)
	if err != nil {
		return bootInfo{}, err
	}
	info := bootInfo{Time: btime}
	if id, err := os.ReadFile(procBootID); err == nil {
		info.ID = strings.TrimSpace(string(id))
	}
	return info, nil
}

// systemShuttingDown asks systemd whether the whole system is stopping (a
// shutdown or reboot), as opposed to only the agent's unit being stopped.
func systemShuttingDown(ctx context.Context) bool {
	if !systemdBooted() {
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return systemStateStopping(ctx, runSystemctl)
}
