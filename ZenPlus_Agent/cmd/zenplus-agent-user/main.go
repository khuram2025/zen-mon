//go:build windows

package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	"golang.org/x/sys/windows"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/appstate"
)

func main() {
	if err := run(); err != nil {
		os.Exit(1)
	}
}

func run() error {
	fs := flag.NewFlagSet("zenplus-agent-user", flag.ExitOnError)
	configPath := fs.String("config", appstate.DefaultConfigPath(), "agent config path")
	if err := fs.Parse(os.Args[1:]); err != nil {
		return err
	}
	mutex, duplicate, err := acquireSingleInstance()
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
	return agent.Run(context.Background(), agent.Options{ConfigPath: *configPath, Foreground: false})
}

func acquireSingleInstance() (windows.Handle, bool, error) {
	handle, err := windows.CreateMutex(nil, false, windows.StringToUTF16Ptr(`Local\ZenPlusAgentUserRunner`))
	if err == windows.ERROR_ALREADY_EXISTS {
		return handle, true, nil
	}
	if err != nil {
		return 0, false, fmt.Errorf("create user runner instance lock: %w", err)
	}
	return handle, false, nil
}
