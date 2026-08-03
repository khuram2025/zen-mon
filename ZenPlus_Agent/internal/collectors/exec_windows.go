//go:build windows

package collectors

import (
	"syscall"

	"golang.org/x/sys/windows"
)

func hiddenPowerShellSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: windows.CREATE_NO_WINDOW,
	}
}
