//go:build windows

package agent

import "os"

// The Windows service stops the agent through the service control handler.
func stopSignals() []os.Signal { return []os.Signal{os.Interrupt} }
