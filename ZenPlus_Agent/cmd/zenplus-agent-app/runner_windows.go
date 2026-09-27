//go:build windows

package main

// The per-user background agent used to be a separate zenplus-agent-user.exe,
// which linked the same packages as this program. It is now a mode of this
// binary, started as its own process (so quitting the tray icon never stops
// monitoring) with --run-agent.

import (
	"context"
	"fmt"

	"golang.org/x/sys/windows"

	"zenplus-agent/internal/agent"
)

func runUserAgent(configPath string) error {
	mutex, duplicate, err := acquireUserRunnerInstance()
	if err != nil {
		return err
	}
	if duplicate {
		if mutex != 0 {
			_ = windows.CloseHandle(mutex)
		}
		return nil
	}
	defer windows.CloseHandle(mutex)
	return agent.Run(context.Background(), agent.Options{ConfigPath: configPath, Foreground: false})
}

// The lock name is unchanged from zenplus-agent-user.exe, so an upgraded
// runner never starts beside one from the previous version.
func acquireUserRunnerInstance() (windows.Handle, bool, error) {
	handle, err := windows.CreateMutex(nil, false, windows.StringToUTF16Ptr(`Local\ZenPlusAgentUserRunner`))
	if err == windows.ERROR_ALREADY_EXISTS {
		return handle, true, nil
	}
	if err != nil {
		return 0, false, fmt.Errorf("create user runner instance lock: %w", err)
	}
	return handle, false, nil
}
