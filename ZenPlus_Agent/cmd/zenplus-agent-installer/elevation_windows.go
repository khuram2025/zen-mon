//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// elevation describes whether this process can install for all users.
type elevation int

const (
	// elevationNotNeeded: already running with administrator rights.
	elevationNotNeeded elevation = iota
	// elevationPrompt: Windows will show a UAC prompt, a consent prompt for an
	// administrator or a credential prompt for a standard user.
	elevationPrompt
	// elevationUnavailable: User Account Control is off, or set to deny
	// elevation requests from standard users, so no prompt can appear.
	elevationUnavailable
)

const (
	tokenElevationTypeLimited = 3
	errorCancelled            = windows.Errno(1223)
	seeMaskNoAsync            = 0x00000100
)

var errElevationCancelled = errors.New("administrator approval was cancelled, so nothing was installed")

// currentElevation inspects the process token and the UAC policy.
func currentElevation() elevation {
	if isElevated() {
		return elevationNotNeeded
	}
	policy, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System`, registry.QUERY_VALUE)
	enableLUA, standardPrompt := uint64(1), uint64(3)
	if err == nil {
		if v, _, err := policy.GetIntegerValue("EnableLUA"); err == nil {
			enableLUA = v
		}
		if v, _, err := policy.GetIntegerValue("ConsentPromptBehaviorUser"); err == nil {
			standardPrompt = v
		}
		policy.Close()
	}
	if enableLUA == 0 {
		return elevationUnavailable
	}
	if tokenElevationType() == tokenElevationTypeLimited {
		return elevationPrompt // an administrator running with a filtered token
	}
	if standardPrompt == 0 {
		return elevationUnavailable // "Automatically deny elevation requests"
	}
	return elevationPrompt
}

func tokenElevationType() uint32 {
	var value, size uint32
	token := windows.GetCurrentProcessToken()
	if err := windows.GetTokenInformation(token, windows.TokenElevationType, (*byte)(unsafe.Pointer(&value)), 4, &size); err != nil {
		return 0
	}
	return value
}

// relaunchElevated runs this Setup again with administrator rights and waits
// for it. owner parents the UAC prompt to the wizard window so the prompt
// appears in front of it. The elevated Setup writes a log whose last failure
// is returned, so the wizard can say why an install failed.
func relaunchElevated(args []string, owner windows.Handle) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	// ShellExecuteEx may use COM, which must be initialised on the calling
	// thread; without it the elevation request can fail without a prompt.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if err := windows.CoInitializeEx(0, windows.COINIT_APARTMENTTHREADED|windows.COINIT_DISABLE_OLE1DDE); err == nil {
		defer windows.CoUninitialize()
	}

	logPath := filepath.Join(os.TempDir(), fmt.Sprintf("ZenPlusAgentSetup-%d.log", time.Now().UnixNano()))
	args = append(append([]string(nil), args...), "LOG="+logPath)
	argv := make([]string, len(args))
	for i, arg := range args {
		argv[i] = windows.EscapeArg(arg)
	}
	verb, _ := windows.UTF16PtrFromString("runas")
	file, err := windows.UTF16PtrFromString(exe)
	if err != nil {
		return err
	}
	parameters, err := windows.UTF16PtrFromString(strings.Join(argv, " "))
	if err != nil {
		return err
	}
	info := shellExecuteInfo{
		Mask:       seeMaskNoCloseProcess | seeMaskNoAsync,
		Window:     owner,
		Verb:       verb,
		File:       file,
		Parameters: parameters,
		Show:       int32(windows.SW_SHOWNORMAL),
	}
	info.Size = uint32(unsafe.Sizeof(info))
	ok, _, callErr := shellExecuteExW.Call(uintptr(unsafe.Pointer(&info)))
	if ok == 0 {
		if errors.Is(callErr, errorCancelled) {
			return withExitCode(int(errorCancelled), errElevationCancelled)
		}
		return fmt.Errorf("Windows could not start Setup with administrator rights: %v", callErr)
	}
	if info.Process == 0 {
		return fmt.Errorf("elevated setup did not return a process handle")
	}
	defer windows.CloseHandle(info.Process)
	waitResult, err := windows.WaitForSingleObject(info.Process, windows.INFINITE)
	if err != nil {
		return fmt.Errorf("wait for elevated setup: %w", err)
	}
	if waitResult != windows.WAIT_OBJECT_0 {
		return fmt.Errorf("wait for elevated setup returned status 0x%x", waitResult)
	}
	var exitCode uint32
	if err := windows.GetExitCodeProcess(info.Process, &exitCode); err != nil {
		return fmt.Errorf("read elevated setup result: %w", err)
	}
	if exitCode == 0 {
		_ = os.Remove(logPath)
		return nil
	}
	if exitCode == exitElevationRequired {
		return withExitCode(int(exitCode), errors.New("Setup did not receive administrator rights. User Account Control may be turned off on this computer; sign in as an administrator to install for all users"))
	}
	if reason := lastSetupFailure(logPath); reason != "" {
		return withExitCode(int(exitCode), errors.New(reason))
	}
	return withExitCode(int(exitCode), fmt.Errorf("elevated setup failed with exit code %d (log: %s)", exitCode, logPath))
}

// lastSetupFailure returns the reason from the last "Setup failed:" line of a
// Setup log, or "".
func lastSetupFailure(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if reason, ok := strings.CutPrefix(strings.TrimSpace(lines[i]), "Setup failed: "); ok {
			return reason
		}
	}
	return ""
}

// installedVersion is the version recorded in Programs and Features, or "".
func installedVersion() string {
	for _, root := range []registry.Key{registry.LOCAL_MACHINE, registry.CURRENT_USER} {
		key, err := registry.OpenKey(root, `Software\Microsoft\Windows\CurrentVersion\Uninstall\ZenPlus Agent`, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		version, _, err := key.GetStringValue("DisplayVersion")
		key.Close()
		if err == nil && version != "" {
			return version
		}
	}
	return ""
}
