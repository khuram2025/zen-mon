//go:build windows

package main

import (
	"errors"
	"fmt"
	"os/exec"
	"runtime"
	"time"

	"golang.org/x/sys/windows"
)

// Setup's exit codes follow Windows Installer conventions, which deployment
// tools (Intune, Configuration Manager, PDQ Deploy, GPO scripts) already
// interpret: 1618 is retried later, 1639 and 740 point at the command line.
const (
	exitInvalidCommandLine = 1639 // ERROR_INVALID_COMMAND_LINE
	exitElevationRequired  = 740  // ERROR_ELEVATION_REQUIRED
	exitAnotherInstall     = 1618 // ERROR_INSTALL_ALREADY_RUNNING
	exitFatal              = 1603 // ERROR_INSTALL_FAILURE
)

type exitCodeError struct {
	code int
	err  error
}

func (e *exitCodeError) Error() string { return e.err.Error() }
func (e *exitCodeError) Unwrap() error { return e.err }

func withExitCode(code int, err error) error {
	if err == nil {
		return nil
	}
	return &exitCodeError{code: code, err: err}
}

// exitCodeFor maps a Setup error to its process exit code. A relaunched
// Setup's own exit code is passed through unchanged.
func exitCodeFor(err error) int {
	if err == nil {
		return 0
	}
	var coded *exitCodeError
	if errors.As(err, &coded) {
		return coded.code
	}
	var exited *exec.ExitError
	if errors.As(err, &exited) && exited.ExitCode() > 0 {
		return exited.ExitCode()
	}
	return exitFatal
}

// setupLockWait is how long Setup waits for another installation of the
// agent (for example a self-update) before giving up with 1618.
const setupLockWait = 2 * time.Minute

// acquireSetupLock serializes installation and removal, so a deployment-tool
// install cannot race an agent self-update or a second deployment.
func acquireSetupLock(scope string) (func(), error) {
	name := `Global\ZenPlusAgentSetup`
	if scope != "machine" {
		// Creating global objects needs a privilege ordinary users lack.
		name = `Local\ZenPlusAgentSetup`
	}
	namePtr, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return nil, err
	}
	// A mutex is owned by an OS thread, so the goroutine stays on this
	// thread until release.
	runtime.LockOSThread()
	unlock := func() { runtime.UnlockOSThread() }
	handle, err := windows.CreateMutex(nil, false, namePtr)
	if handle == 0 {
		unlock()
		return nil, fmt.Errorf("create setup lock: %w", err)
	}
	event, err := windows.WaitForSingleObject(handle, uint32(setupLockWait/time.Millisecond))
	switch {
	case err != nil:
		_ = windows.CloseHandle(handle)
		unlock()
		return nil, fmt.Errorf("wait for setup lock: %w", err)
	case event == uint32(windows.WAIT_TIMEOUT):
		_ = windows.CloseHandle(handle)
		unlock()
		return nil, withExitCode(exitAnotherInstall, fmt.Errorf("another ZenPlus Agent installation or removal is in progress; retry when it finishes"))
	}
	// WAIT_ABANDONED means a previous Setup died holding the lock; the lock
	// is ours now and its work is protected by the install transaction.
	return func() {
		_ = windows.ReleaseMutex(handle)
		_ = windows.CloseHandle(handle)
		unlock()
	}, nil
}
