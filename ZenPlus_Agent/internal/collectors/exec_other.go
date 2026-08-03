//go:build !windows

package collectors

import "syscall"

func hiddenPowerShellSysProcAttr() *syscall.SysProcAttr {
	return nil
}
