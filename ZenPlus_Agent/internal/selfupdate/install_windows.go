//go:build windows

package selfupdate

import (
	"fmt"
	"os/exec"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
)

// launchInstaller starts the package installer detached from this process.
// The installer stops the ZenPlusAgent service, replaces the binaries, and
// starts the service again, so we must not wait on it.
func launchInstaller(pkgPath string) error {
	var cmd *exec.Cmd
	switch {
	case strings.HasSuffix(strings.ToLower(pkgPath), ".msi"):
		cmd = exec.Command("msiexec.exe", "/i", pkgPath, "/qn", "/norestart")
	case strings.HasSuffix(strings.ToLower(pkgPath), ".exe"):
		cmd = exec.Command(pkgPath, "/machine", "/quiet", "/norestart")
	default:
		return fmt.Errorf("unsupported package type: %s", pkgPath)
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: windows.CREATE_NO_WINDOW | windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS,
	}
	return cmd.Start()
}
