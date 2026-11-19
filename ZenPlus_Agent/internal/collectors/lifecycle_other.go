//go:build !linux

package collectors

import (
	"context"
	"errors"
)

// Windows reports restarts and shutdowns through its System event log; other
// platforms have no source yet.
const lifecycleSupported = false

func readBootInfo() (bootInfo, error) {
	return bootInfo{}, errors.New("boot tracking is not available on this platform")
}

func systemShuttingDown(context.Context) bool { return false }
