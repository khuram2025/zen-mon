//go:build windows

package selfupdate

import (
	"fmt"
	"os/exec"
	"path/filepath"
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

// OpenInstaller opens the verified package's normal Setup UI. Machine scope
// requests Windows administrator approval before Setup loads the protected
// existing configuration; user scope stays user.
func OpenInstaller(pkgPath string, machine bool) error {
	if !strings.EqualFold(filepath.Ext(pkgPath), ".exe") {
		return fmt.Errorf("interactive updates require the EXE installer")
	}
	scope := "/user"
	if machine {
		file, err := windows.UTF16PtrFromString(pkgPath)
		if err != nil {
			return err
		}
		return windows.ShellExecute(0, windows.StringToUTF16Ptr("runas"), file,
			windows.StringToUTF16Ptr("/machine /norestart"), nil, windows.SW_SHOWNORMAL)
	}
	cmd := exec.Command(pkgPath, scope, "/norestart")
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}
