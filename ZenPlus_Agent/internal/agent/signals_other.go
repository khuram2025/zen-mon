//go:build !windows

package agent

import (
	"os"
	"syscall"
)

// systemd stops the service with SIGTERM. Handling it lets the agent record a
// clean stop (and a system shutdown) instead of being killed mid-cycle.
func stopSignals() []os.Signal { return []os.Signal{os.Interrupt, syscall.SIGTERM} }
