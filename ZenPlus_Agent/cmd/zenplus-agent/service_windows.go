//go:build windows

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/config"
)

const (
	serviceName        = "ZenPlusAgent"
	serviceDisplayName = "ZenPlus Agent"
)

type serviceHandler struct {
	configPath string
}

func runService(configPath string) error {
	isService, err := svc.IsWindowsService()
	if err != nil {
		return err
	}
	if !isService {
		return agent.Run(context.Background(), agent.Options{ConfigPath: configPath, Foreground: true})
	}
	return svc.Run(serviceName, &serviceHandler{configPath: configPath})
}

func (h *serviceHandler) Execute(_ []string, requests <-chan svc.ChangeRequest, changes chan<- svc.Status) (bool, uint32) {
	const accepts = svc.AcceptStop | svc.AcceptShutdown
	changes <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- agent.Run(ctx, agent.Options{ConfigPath: h.configPath, Foreground: false})
	}()
	changes <- svc.Status{State: svc.Running, Accepts: accepts}
	for {
		select {
		case req := <-requests:
			switch req.Cmd {
			case svc.Interrogate:
				changes <- req.CurrentStatus
			case svc.Stop, svc.Shutdown:
				changes <- svc.Status{State: svc.StopPending}
				cancel()
				select {
				case <-done:
				case <-time.After(20 * time.Second):
				}
				return false, 0
			default:
			}
		case err := <-done:
			if err != nil {
				return false, 1
			}
			return false, 0
		}
	}
}

func installService(exePath string, configPath string) error {
	if err := ensureInstallConfig(configPath); err != nil {
		return err
	}
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect service manager: %w", err)
	}
	defer m.Disconnect()
	if s, err := m.OpenService(serviceName); err == nil {
		_ = s.Close()
		return fmt.Errorf("%s is already installed", serviceDisplayName)
	}
	s, err := m.CreateService(serviceName, exePath, mgr.Config{
		DisplayName: serviceDisplayName,
		Description: "Collects Windows host telemetry and uploads it to the ZenPlus controller.",
		StartType:   mgr.StartAutomatic,
	}, "service", "--config", configPath)
	if err != nil {
		return fmt.Errorf("create service: %w", err)
	}
	defer s.Close()
	_ = eventlog.InstallAsEventCreate(serviceName, eventlog.Error|eventlog.Warning|eventlog.Info)
	if err := configureServiceRecovery(s); err != nil {
		return err
	}
	if err := s.Start(); err != nil {
		return fmt.Errorf("service installed but failed to start: %w", err)
	}
	fmt.Printf("%s installed and started\n", serviceDisplayName)
	return nil
}

func configureServiceRecovery(s *mgr.Service) error {
	actions := []mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: time.Minute},
		{Type: mgr.ServiceRestart, Delay: time.Minute},
		{Type: mgr.ServiceRestart, Delay: 5 * time.Minute},
	}
	if err := s.SetRecoveryActions(actions, 86400); err != nil {
		return fmt.Errorf("configure service recovery actions: %w", err)
	}
	if err := s.SetRecoveryActionsOnNonCrashFailures(true); err != nil {
		return fmt.Errorf("configure service non-crash recovery: %w", err)
	}
	return nil
}

func uninstallService() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect service manager: %w", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(serviceName)
	if err != nil {
		return fmt.Errorf("open service: %w", err)
	}
	defer s.Close()
	status, err := s.Control(svc.Stop)
	if err == nil {
		_ = status
		time.Sleep(2 * time.Second)
	}
	if err := s.Delete(); err != nil {
		return fmt.Errorf("delete service: %w", err)
	}
	_ = eventlog.Remove(serviceName)
	fmt.Printf("%s removed\n", serviceDisplayName)
	return nil
}

func ensureInstallConfig(path string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	cfg := config.Default()
	programData := os.Getenv("ProgramData")
	if programData != "" {
		cfg.DataDir = filepath.Join(programData, "ZenPlus", "Agent")
	}
	return config.Save(path, cfg)
}
