package runtime

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/secrets"
)

const (
	dashboardSnapshotSchemaVersion = 1
	maxDashboardErrorRunes         = 2048
)

// DashboardSnapshot is the deliberately small, non-secret view published by
// the machine service for the unelevated desktop dashboard. Raw spool records,
// logs, APM queues, credential metadata, and instrumentation rollback values
// are intentionally excluded.
type DashboardSnapshot struct {
	SchemaVersion int                     `json:"schema_version"`
	GeneratedAt   time.Time               `json:"generated_at"`
	Status        model.Status            `json:"status"`
	Identity      DashboardIdentity       `json:"identity"`
	Config        DashboardConfigSnapshot `json:"config"`
}

type DashboardIdentity struct {
	AgentID   string `json:"agent_id,omitempty"`
	ServerID  string `json:"server_id,omitempty"`
	Hostname  string `json:"hostname,omitempty"`
	FQDN      string `json:"fqdn,omitempty"`
	Platform  string `json:"platform,omitempty"`
	OSName    string `json:"os_name,omitempty"`
	OSVersion string `json:"os_version,omitempty"`
}

type DashboardConfigSnapshot struct {
	ControllerURL            string          `json:"controller_url"`
	PolicyID                 string          `json:"policy_id,omitempty"`
	VerifyTLS                bool            `json:"verify_tls"`
	DataDir                  string          `json:"data_dir"`
	HeartbeatIntervalSeconds int             `json:"heartbeat_interval_seconds"`
	UploadIntervalSeconds    int             `json:"upload_interval_seconds"`
	CollectIntervalSeconds   int             `json:"collect_interval_seconds"`
	MonitoringProfile        string          `json:"monitoring_profile"`
	CollectorEnabled         map[string]bool `json:"collector_enabled"`
}

func NewDashboardSnapshot(cfg config.Config, id identity.Identity, status model.Status) DashboardSnapshot {
	controllerURL := publicControllerURL(cfg.ControllerURL)
	status.ControllerURL = controllerURL
	status.LastHeartbeatError = sanitizeDashboardError(status.LastHeartbeatError)
	status.LastUploadError = sanitizeDashboardError(status.LastUploadError)
	status.LastConfigError = sanitizeDashboardError(status.LastConfigError)
	if len(status.CollectorErrors) > 0 {
		collectorErrors := make(map[string]string, len(status.CollectorErrors))
		for name, message := range status.CollectorErrors {
			collectorErrors[name] = sanitizeDashboardError(message)
		}
		status.CollectorErrors = collectorErrors
	}
	if status.LocalAPM != nil {
		copy := *status.LocalAPM
		copy.LastError = sanitizeDashboardError(copy.LastError)
		if len(copy.Bundles) > 0 {
			copy.Bundles = cloneStringMap(copy.Bundles)
		}
		status.LocalAPM = &copy
	}
	if status.APM != nil {
		copy := *status.APM
		copy.Message = sanitizeDashboardError(copy.Message)
		status.APM = &copy
	}
	return DashboardSnapshot{
		SchemaVersion: dashboardSnapshotSchemaVersion,
		GeneratedAt:   time.Now().UTC(),
		Status:        status,
		Identity: DashboardIdentity{
			AgentID:   id.AgentID,
			ServerID:  id.ServerID,
			Hostname:  id.Hostname,
			FQDN:      id.FQDN,
			Platform:  id.Platform,
			OSName:    id.OSName,
			OSVersion: id.OSVersion,
		},
		Config: DashboardConfigSnapshot{
			ControllerURL:            controllerURL,
			PolicyID:                 cfg.PolicyID,
			VerifyTLS:                cfg.VerifyTLS,
			DataDir:                  cfg.DataDir,
			HeartbeatIntervalSeconds: cfg.HeartbeatIntervalSeconds,
			UploadIntervalSeconds:    cfg.UploadIntervalSeconds,
			CollectIntervalSeconds:   cfg.CollectIntervalSeconds,
			MonitoringProfile:        cfg.APM.Profile,
			CollectorEnabled: map[string]bool{
				"cpu":        cfg.Collectors.CPU.Enabled,
				"memory":     cfg.Collectors.Memory.Enabled,
				"filesystem": cfg.Collectors.Filesystem.Enabled,
				"disk_io":    cfg.Collectors.DiskIO.Enabled,
				"network":    cfg.Collectors.Network.Enabled,
				"processes":  cfg.Collectors.Processes.Enabled,
				"services":   cfg.Collectors.Services.Enabled,
				"event_log":  cfg.Collectors.EventLog.Enabled,
				"inventory":  cfg.Collectors.Inventory.Enabled,
			},
		},
	}
}

func WriteMachineDashboardSnapshot(cfg config.Config, id identity.Identity, status model.Status) error {
	path, ok := MachineDashboardPathForDataDir(cfg.DataDir)
	if !ok {
		return nil
	}
	contents, err := json.MarshalIndent(NewDashboardSnapshot(cfg, id, status), "", "  ")
	if err != nil {
		return err
	}
	directory := filepath.Dir(path)
	if err := secrets.PrepareMachineDashboardDirectory(directory); err != nil {
		return fmt.Errorf("prepare dashboard status directory: %w", err)
	}
	temp, err := os.CreateTemp(directory, ".snapshot-*.json")
	if err != nil {
		return fmt.Errorf("create dashboard status file: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if _, err := temp.Write(contents); err != nil {
		_ = temp.Close()
		return fmt.Errorf("write dashboard status: %w", err)
	}
	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return fmt.Errorf("flush dashboard status: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close dashboard status: %w", err)
	}
	if err := os.Chmod(tempPath, 0o644); err != nil {
		return fmt.Errorf("set dashboard status mode: %w", err)
	}
	if err := secrets.ProtectMachineDashboardFile(tempPath); err != nil {
		return fmt.Errorf("protect dashboard status file: %w", err)
	}
	if err := replaceDashboardFile(tempPath, path); err != nil {
		return fmt.Errorf("publish dashboard status: %w", err)
	}
	return nil
}

func ReadMachineDashboardSnapshot(configPath string) (DashboardSnapshot, error) {
	path, ok := MachineDashboardPathForConfig(configPath)
	if !ok {
		return DashboardSnapshot{}, os.ErrNotExist
	}
	if err := secrets.ValidateMachineDashboardSnapshot(path); err != nil {
		return DashboardSnapshot{}, fmt.Errorf("validate dashboard status file: %w", err)
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return DashboardSnapshot{}, err
	}
	var snapshot DashboardSnapshot
	if err := json.Unmarshal(contents, &snapshot); err != nil {
		return DashboardSnapshot{}, err
	}
	if snapshot.SchemaVersion != dashboardSnapshotSchemaVersion {
		return DashboardSnapshot{}, fmt.Errorf("unsupported dashboard status schema %d", snapshot.SchemaVersion)
	}
	if snapshot.GeneratedAt.IsZero() {
		return DashboardSnapshot{}, fmt.Errorf("dashboard status has no generation time")
	}
	return snapshot, nil
}

func RemoveMachineDashboardSnapshot(dataDir string) error {
	path, ok := MachineDashboardPathForDataDir(dataDir)
	if !ok {
		return nil
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Remove(filepath.Dir(path)); err != nil && !errors.Is(err, os.ErrNotExist) {
		// Keep a non-empty directory intact; it may contain a newer snapshot
		// format owned by a future installation.
		return nil
	}
	return nil
}

func MachineDashboardPathForConfig(configPath string) (string, bool) {
	root, ok := machineDataRoot()
	if !ok || !sameDashboardPath(configPath, filepath.Join(root, "config", "agent.yaml")) {
		return "", false
	}
	return machineDashboardSnapshotPath(root), true
}

func MachineDashboardPathForDataDir(dataDir string) (string, bool) {
	root, ok := machineDataRoot()
	if !ok || !sameDashboardPath(dataDir, root) {
		return "", false
	}
	return machineDashboardSnapshotPath(root), true
}

func machineDashboardSnapshotPath(machineRoot string) string {
	return filepath.Join(filepath.Dir(machineRoot), "AgentDashboard", "snapshot.json")
}

func machineDataRoot() (string, bool) {
	programData := strings.TrimSpace(os.Getenv("ProgramData"))
	if programData == "" {
		if filepath.Separator != '\\' {
			return "", false
		}
		programData = `C:\ProgramData`
	}
	return filepath.Join(programData, "ZenPlus", "Agent"), true
}

func sameDashboardPath(left, right string) bool {
	leftAbsolute, leftErr := filepath.Abs(left)
	rightAbsolute, rightErr := filepath.Abs(right)
	if leftErr != nil || rightErr != nil {
		return false
	}
	return strings.EqualFold(filepath.Clean(leftAbsolute), filepath.Clean(rightAbsolute))
}

func replaceDashboardFile(source, target string) error {
	// os.Rename replaces a same-volume file atomically on supported platforms,
	// including Windows (MoveFileEx with MOVEFILE_REPLACE_EXISTING). Never
	// remove the live snapshot first: readers must see either complete version.
	return os.Rename(source, target)
}

func publicControllerURL(raw string) string {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return ""
	}
	// User info and URL parameters are not valid appliance identity and may
	// contain credentials supplied by a legacy or hand-edited configuration.
	parsed.User = nil
	parsed.RawQuery = ""
	parsed.ForceQuery = false
	parsed.Fragment = ""
	return strings.TrimRight(parsed.String(), "/")
}

func sanitizeDashboardError(message string) string {
	message = Redact(message)
	message = dashboardURLUserinfoRule.ReplaceAllString(message, `${1}REDACTED@`)
	for _, rule := range dashboardSecretRules {
		message = rule.ReplaceAllString(message, `${1}REDACTED`)
	}
	runes := []rune(message)
	if len(runes) > maxDashboardErrorRunes {
		message = string(runes[:maxDashboardErrorRunes]) + "..."
	}
	return message
}

func cloneStringMap(source map[string]string) map[string]string {
	clone := make(map[string]string, len(source))
	for key, value := range source {
		clone[key] = value
	}
	return clone
}

var dashboardURLUserinfoRule = regexp.MustCompile(`(?i)(https?://)[^/\s@]+@`)

var dashboardSecretRules = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(["']?(?:api[_-]?key|private[_-]?key|secret|password|credential|pending[_-]?secret|client[_-]?secret|(?:access|refresh|id|enrollment)[_-]?token|token|connection[_-]?string)["']?\s*[:=]\s*["']?)[^\s,;"'&}]+`),
	regexp.MustCompile(`(?i)(\bauthorization\s*[:=]\s*(?:basic|bearer|apikey)?\s*)[^\s,;"']+`),
}
