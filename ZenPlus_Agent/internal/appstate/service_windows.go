//go:build windows

package appstate

import (
	"fmt"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const serviceName = "ZenPlusAgent"

func ReadServiceSnapshot() ServiceSnapshot {
	out := ServiceSnapshot{Name: serviceName}
	scm, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_CONNECT)
	if err != nil {
		out.Error = fmt.Sprintf("connect service manager: %v", err)
		return out
	}
	defer windows.CloseServiceHandle(scm)

	name, _ := windows.UTF16PtrFromString(serviceName)
	handle, err := windows.OpenService(scm, name, windows.SERVICE_QUERY_STATUS|windows.SERVICE_QUERY_CONFIG)
	if err != nil {
		out.State = "Not installed"
		return out
	}
	s := &mgr.Service{Name: serviceName, Handle: handle}
	defer s.Close()

	out.Installed = true
	if cfg, err := s.Config(); err == nil {
		out.StartMode = startModeText(cfg.StartType)
	}
	status, err := s.Query()
	if err != nil {
		out.Error = fmt.Sprintf("query service: %v", err)
		return out
	}
	out.State = serviceStateText(status.State)
	out.Running = status.State == svc.Running
	return out
}

func serviceStateText(state svc.State) string {
	switch state {
	case svc.Stopped:
		return "Stopped"
	case svc.StartPending:
		return "Starting"
	case svc.StopPending:
		return "Stopping"
	case svc.Running:
		return "Running"
	case svc.ContinuePending:
		return "Continuing"
	case svc.PausePending:
		return "Pausing"
	case svc.Paused:
		return "Paused"
	default:
		return fmt.Sprintf("Unknown (%d)", state)
	}
}

func startModeText(startType uint32) string {
	switch startType {
	case mgr.StartAutomatic:
		return "Automatic"
	case mgr.StartManual:
		return "Manual"
	case mgr.StartDisabled:
		return "Disabled"
	default:
		return fmt.Sprintf("Start type %d", startType)
	}
}
