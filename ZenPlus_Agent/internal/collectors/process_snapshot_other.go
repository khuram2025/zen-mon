//go:build !windows

package collectors

import (
	"context"

	"github.com/shirou/gopsutil/v4/process"
)

// processSnapshot reads each process through gopsutil. Windows uses a single
// system call instead (process_snapshot_windows.go).
func processSnapshot(ctx context.Context) ([]processSnapshotEntry, error) {
	procs, err := process.ProcessesWithContext(ctx)
	if err != nil {
		return nil, err
	}
	entries := make([]processSnapshotEntry, 0, len(procs))
	for _, p := range procs {
		name, _ := p.NameWithContext(ctx)
		entry := processSnapshotEntry{PID: p.Pid, Name: name}
		if memInfo, err := p.MemoryInfoWithContext(ctx); err == nil && memInfo != nil {
			entry.RSS = memInfo.RSS
		}
		entry.Threads, _ = p.NumThreadsWithContext(ctx)
		entry.Handles, _ = p.NumFDsWithContext(ctx)
		entry.CreatedMs, _ = p.CreateTimeWithContext(ctx)
		if times, err := p.TimesWithContext(ctx); err == nil && times != nil {
			entry.CPUSeconds = times.User + times.System
		}
		entries = append(entries, entry)
	}
	return entries, nil
}
