//go:build windows

package collectors

import (
	"context"
	"errors"
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	statusInfoLengthMismatch = windows.NTStatus(0xC0000004)
	// FILETIME epoch (1601-01-01) to Unix epoch, in 100 ns units.
	filetimeUnixOffset = 116444736000000000
)

// processSnapshot reads every process's name, thread and handle counts,
// working set, CPU time and start time with a single
// NtQuerySystemInformation call. Querying each process separately cost
// several system calls apiece, and the thread count took a full system
// snapshot per process, so collection grew quadratically with process count.
func processSnapshot(ctx context.Context) ([]processSnapshotEntry, error) {
	size := uint32(512 * 1024)
	for attempt := 0; attempt < 8; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		buf := make([]byte, size)
		var needed uint32
		err := windows.NtQuerySystemInformation(windows.SystemProcessInformation, unsafe.Pointer(&buf[0]), size, &needed)
		if errors.Is(err, statusInfoLengthMismatch) {
			size = needed + 64*1024 // processes may start before the retry
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("query process information: %w", err)
		}
		return parseProcessSnapshot(buf), nil
	}
	return nil, errors.New("query process information: buffer kept growing")
}

func parseProcessSnapshot(buf []byte) []processSnapshotEntry {
	var entries []processSnapshotEntry
	for offset := uint32(0); int(offset)+int(unsafe.Sizeof(windows.SYSTEM_PROCESS_INFORMATION{})) <= len(buf); {
		info := (*windows.SYSTEM_PROCESS_INFORMATION)(unsafe.Pointer(&buf[offset]))
		pid := int32(info.UniqueProcessID)
		// PID 0 is the idle process: its "CPU time" is machine idle time.
		if pid != 0 {
			name := info.ImageName.String()
			if pid == 4 && name == "" {
				name = "System"
			}
			var createdMs int64
			if info.CreateTime > filetimeUnixOffset {
				createdMs = (info.CreateTime - filetimeUnixOffset) / 10000
			}
			entries = append(entries, processSnapshotEntry{
				PID:        pid,
				Name:       name,
				Threads:    int32(info.NumberOfThreads),
				Handles:    int32(info.HandleCount),
				RSS:        uint64(info.WorkingSetSize),
				CPUSeconds: float64(info.UserTime+info.KernelTime) / 1e7,
				CreatedMs:  createdMs,
			})
		}
		if info.NextEntryOffset == 0 {
			break
		}
		offset += info.NextEntryOffset
	}
	return entries
}
