//go:build !windows

package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/config"
)

const (
	serviceName        = "zenplus-agent"
	serviceDisplayName = "ZenPlus Agent"
)

func runService(configPath string) error {
	return agent.Run(context.Background(), agent.Options{ConfigPath: configPath, Foreground: false})
}

func installService(exePath string, configPath string) error {
	if runtime.GOOS != "linux" {
		return fmt.Errorf("%s service installation is not supported on %s", serviceDisplayName, runtime.GOOS)
	}
	if os.Geteuid() != 0 {
		return fmt.Errorf("install-service must be run as root")
	}
	resolvedExe, err := filepath.Abs(exePath)
	if err != nil {
		return err
	}
	resolvedConfig, err := filepath.Abs(configPath)
	if err != nil {
		return err
	}
	if err := ensureInstallConfig(resolvedConfig); err != nil {
		return err
	}
	unit := fmt.Sprintf(`[Unit]
Description=%s
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%s service --config %s
Restart=always
RestartSec=10
WorkingDirectory=%s
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
`, serviceDisplayName, resolvedExe, resolvedConfig, filepath.Dir(resolvedExe))
	unitPath := "/etc/systemd/system/" + serviceName + ".service"
	if err := os.WriteFile(unitPath, []byte(unit), 0o644); err != nil {
		return err
	}
	if err := runSystemctl("daemon-reload"); err != nil {
		return err
	}
	if err := runSystemctl("enable", "--now", serviceName+".service"); err != nil {
		return err
	}
	fmt.Printf("%s installed and started\n", serviceDisplayName)
	return nil
}

func uninstallService() error {
	if runtime.GOOS != "linux" {
		return fmt.Errorf("%s service removal is not supported on %s", serviceDisplayName, runtime.GOOS)
	}
	if os.Geteuid() != 0 {
		return fmt.Errorf("uninstall-service must be run as root")
	}
	_ = runSystemctl("disable", "--now", serviceName+".service")
	if err := os.Remove("/etc/systemd/system/" + serviceName + ".service"); err != nil && !os.IsNotExist(err) {
		return err
	}
	return runSystemctl("daemon-reload")
}

func ensureInstallConfig(path string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	} else if !os.IsNotExist(err) {
		return err
	}
	cfg := config.Default()
	if runtime.GOOS == "linux" {
		cfg.DataDir = "/var/lib/zenplus-agent"
		cfg.Collectors.Services.Enabled = false
		cfg.Collectors.EventLog.Enabled = false
	}
	return config.Save(path, cfg)
}

func runSystemctl(args ...string) error {
	cmd := exec.Command("systemctl", args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("systemctl %v failed: %w: %s", args, err, string(out))
	}
	return nil
}
