package config

import (
	"fmt"
	"strings"
)

// ApplyProfile makes the persisted collector and APM switches consistent
// with an installation profile. It also repairs the all-disabled collector
// state left by installers older than 1.11.3.
func ApplyProfile(cfg *Config, profile string) error {
	if cfg == nil {
		return fmt.Errorf("config is required")
	}
	profile = strings.ToLower(strings.TrimSpace(profile))
	if profile != "infrastructure" && profile != "apm" && profile != "combined" {
		return fmt.Errorf("profile must be infrastructure, apm, or combined")
	}

	previousProfile := strings.ToLower(strings.TrimSpace(cfg.APM.Profile))
	cfg.APM.Profile = profile
	cfg.APM.Enabled = profile != "infrastructure"
	if profile == "apm" {
		setInfrastructureCollectors(cfg, false)
		// Inventory remains useful for application discovery and support.
		cfg.Collectors.Inventory.Enabled = true
		return nil
	}
	if previousProfile == "apm" || InfrastructureCollectorsDisabled(*cfg) {
		setInfrastructureCollectors(cfg, true)
		cfg.Collectors.Inventory.Enabled = true
	}
	return nil
}

// InfrastructureCollectorsDisabled identifies the broken state in which a
// non-APM-only profile cannot emit any server telemetry.
func InfrastructureCollectorsDisabled(cfg Config) bool {
	return !cfg.Collectors.CPU.Enabled &&
		!cfg.Collectors.Memory.Enabled &&
		!cfg.Collectors.Filesystem.Enabled &&
		!cfg.Collectors.DiskIO.Enabled &&
		!cfg.Collectors.Network.Enabled &&
		!cfg.Collectors.Processes.Enabled &&
		!cfg.Collectors.Services.Enabled &&
		!cfg.Collectors.EventLog.Enabled
}

func setInfrastructureCollectors(cfg *Config, enabled bool) {
	cfg.Collectors.CPU.Enabled = enabled
	cfg.Collectors.Memory.Enabled = enabled
	cfg.Collectors.Filesystem.Enabled = enabled
	cfg.Collectors.DiskIO.Enabled = enabled
	cfg.Collectors.Network.Enabled = enabled
	cfg.Collectors.Processes.Enabled = enabled
	cfg.Collectors.Services.Enabled = enabled
	cfg.Collectors.EventLog.Enabled = enabled
}
