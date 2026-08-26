//go:build windows

package agent

import "golang.org/x/sys/windows"

func canManageApplicationInstrumentation() bool {
	return windows.GetCurrentProcessToken().IsElevated()
}
