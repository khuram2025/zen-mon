//go:build windows

package apm

import (
	"strings"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

type windowsServiceInfo struct {
	Name        string
	DisplayName string
	BinaryPath  string
}

func windowsServicesByPID() map[int32]windowsServiceInfo {
	result := map[int32]windowsServiceInfo{}
	manager, err := mgr.Connect()
	if err != nil {
		return result
	}
	defer manager.Disconnect()
	names, err := manager.ListServices()
	if err != nil {
		return result
	}
	for _, name := range names {
		service, openErr := manager.OpenService(name)
		if openErr != nil {
			continue
		}
		status, statusErr := service.Query()
		if statusErr != nil || status.State != svc.Running || status.ProcessId == 0 {
			service.Close()
			continue
		}
		info := windowsServiceInfo{Name: name}
		if config, configErr := service.Config(); configErr == nil {
			info.DisplayName = strings.TrimSpace(config.DisplayName)
			info.BinaryPath = strings.TrimSpace(config.BinaryPathName)
		}
		service.Close()
		// Shared-process services cannot be safely instrumented individually.
		// Mark the PID ambiguous by clearing its service identity.
		pid := int32(status.ProcessId)
		if _, duplicate := result[pid]; duplicate {
			result[pid] = windowsServiceInfo{}
		} else {
			result[pid] = info
		}
	}
	return result
}
