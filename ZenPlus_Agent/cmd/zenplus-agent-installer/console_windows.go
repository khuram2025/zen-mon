//go:build windows

package main

import (
	"os"

	"golang.org/x/sys/windows"
)

// Setup is a GUI-subsystem program, so a Command Prompt or PowerShell that
// starts it gets no output. For silent runs started from a console, Setup
// attaches to that console and prints its result there, so an administrator
// sees "not elevated" or "installed" without looking up the exit code. With
// no parent console (deployment tools, MSI, self-update) nothing changes.

var attachConsole = windows.NewLazySystemDLL("kernel32.dll").NewProc("AttachConsole")

const attachParentProcess = ^uintptr(0) // ATTACH_PARENT_PROCESS (DWORD -1)

// parentConsole returns a writer on the console of the process that started
// Setup, or nil when there is none.
func parentConsole() *os.File {
	if ok, _, _ := attachConsole.Call(attachParentProcess); ok == 0 {
		return nil
	}
	out, err := os.OpenFile("CONOUT$", os.O_WRONLY, 0)
	if err != nil {
		return nil
	}
	return out
}
