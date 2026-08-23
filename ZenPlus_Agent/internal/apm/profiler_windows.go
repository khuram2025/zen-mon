//go:build windows

package apm

import (
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

func detectProfiler(pid int32) bool {
	facts := inspectProcessModules(pid)
	return facts.OTelDetected
}

type processModuleFacts struct {
	OTelDetected    bool
	DotnetCore      bool
	DotnetFramework bool
}

func inspectProcessModules(pid int32) processModuleFacts {
	facts := processModuleFacts{}
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPMODULE|windows.TH32CS_SNAPMODULE32, uint32(pid))
	if err != nil {
		return facts
	}
	defer windows.CloseHandle(snapshot)
	entry := windows.ModuleEntry32{Size: uint32(unsafeSizeofModuleEntry32())}
	if windows.Module32First(snapshot, &entry) != nil {
		return facts
	}
	for {
		name := strings.ToLower(windows.UTF16ToString(entry.Module[:]))
		path := strings.ToLower(windows.UTF16ToString(entry.ExePath[:]))
		if strings.Contains(name, "opentelemetry.autoinstrumentation.native") || strings.Contains(path, "opentelemetry.autoinstrumentation.native") {
			facts.OTelDetected = true
		}
		if name == "coreclr.dll" {
			facts.DotnetCore = true
		}
		if name == "clr.dll" || name == "mscorwks.dll" {
			facts.DotnetFramework = true
		}
		if windows.Module32Next(snapshot, &entry) != nil {
			break
		}
	}
	return facts
}

// Kept as a function to make the platform struct sizing explicit at the API
// boundary. The Windows package requires Size to be initialized.
func unsafeSizeofModuleEntry32() uintptr {
	var entry windows.ModuleEntry32
	return unsafe.Sizeof(entry)
}
