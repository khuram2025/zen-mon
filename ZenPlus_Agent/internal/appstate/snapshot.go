package appstate

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
)

type Snapshot struct {
	Now         time.Time
	PublishedAt time.Time
	Status      *model.Status
	Identity    *identity.Identity
	Service     ServiceSnapshot
	Controller  ControllerSnapshot
	Spool       SpoolSnapshot
	Config      ConfigSnapshot
	Logs        []string
	Paths       PathSnapshot
}

type ControllerSnapshot struct {
	URL       string
	Reachable bool
	Status    string
	Message   string
}

type ServiceSnapshot struct {
	Name      string
	State     string
	StartMode string
	Installed bool
	Running   bool
	Error     string
}

type SpoolSnapshot struct {
	Depth int
	Bytes int64
	Error string
}

type ConfigSnapshot struct {
	ControllerURL            string
	PolicyID                 string
	VerifyTLS                bool
	DataDir                  string
	HeartbeatIntervalSeconds int
	UploadIntervalSeconds    int
	CollectIntervalSeconds   int
	MonitoringProfile        string
	CollectorEnabled         map[string]bool
	Labels                   map[string]string
}

type PathSnapshot struct {
	Status string
	Spool  string
	Log    string
	Config string
}

func Load(ctx context.Context, configPath string) Snapshot {
	if configPath == "" {
		configPath = DefaultConfigPath()
	}
	if published, err := runtime.ReadMachineDashboardSnapshot(configPath); err == nil {
		return loadPublishedMachineSnapshot(ctx, configPath, published)
	}
	cfg, cfgErr := config.Load(configPath)
	if cfgErr != nil {
		cfg = config.Default()
	}
	paths := runtime.NewPaths(cfg.DataDir)
	snap := Snapshot{
		Now:        time.Now().UTC(),
		Service:    ReadServiceSnapshot(),
		Controller: ControllerStatus(ctx, cfg.ControllerURL),
		Config:     ConfigFrom(cfg),
		Logs:       TailLines(paths.LogFile, 120),
		Paths: PathSnapshot{
			Status: paths.StatusFile,
			Spool:  paths.SpoolDB,
			Log:    paths.LogFile,
			Config: configPath,
		},
	}
	if cfgErr != nil {
		snap.Controller.Message = cfgErr.Error()
	}
	if status, err := agent.ReadStatus(configPath); err == nil {
		snap.Status = &status
		snap.Spool.Depth = status.QueueDepth
		snap.Spool.Bytes = status.SpoolBytes
	}
	if id, err := readIdentity(paths.IdentityFile); err == nil {
		snap.Identity = &id
	}
	return snap
}

func loadPublishedMachineSnapshot(ctx context.Context, configPath string, published runtime.DashboardSnapshot) Snapshot {
	status := published.Status
	id := identity.Identity{
		AgentID:   published.Identity.AgentID,
		ServerID:  published.Identity.ServerID,
		Hostname:  published.Identity.Hostname,
		FQDN:      published.Identity.FQDN,
		Platform:  published.Identity.Platform,
		OSName:    published.Identity.OSName,
		OSVersion: published.Identity.OSVersion,
	}
	return Snapshot{
		Now:         time.Now().UTC(),
		PublishedAt: published.GeneratedAt,
		Status:      &status,
		Identity:    &id,
		Service:     ReadServiceSnapshot(),
		Controller:  ControllerStatus(ctx, published.Config.ControllerURL),
		Spool: SpoolSnapshot{
			Depth: status.QueueDepth,
			Bytes: status.SpoolBytes,
		},
		Config: ConfigSnapshot{
			ControllerURL:            published.Config.ControllerURL,
			PolicyID:                 published.Config.PolicyID,
			VerifyTLS:                published.Config.VerifyTLS,
			DataDir:                  published.Config.DataDir,
			HeartbeatIntervalSeconds: published.Config.HeartbeatIntervalSeconds,
			UploadIntervalSeconds:    published.Config.UploadIntervalSeconds,
			CollectIntervalSeconds:   published.Config.CollectIntervalSeconds,
			MonitoringProfile:        published.Config.MonitoringProfile,
			CollectorEnabled:         published.Config.CollectorEnabled,
		},
		Paths: PathSnapshot{Config: configPath},
	}
}

func DefaultConfigPath() string {
	if _, err := os.Stat(config.DefaultConfigPath); err == nil {
		return config.DefaultConfigPath
	}
	programData := os.Getenv("ProgramData")
	if programData == "" {
		return config.DefaultConfigPath
	}
	return filepath.Join(programData, "ZenPlus", "Agent", "config", "agent.yaml")
}

func ConfigFrom(cfg config.Config) ConfigSnapshot {
	return ConfigSnapshot{
		ControllerURL:            cfg.ControllerURL,
		PolicyID:                 cfg.PolicyID,
		VerifyTLS:                cfg.VerifyTLS,
		DataDir:                  cfg.DataDir,
		HeartbeatIntervalSeconds: cfg.HeartbeatIntervalSeconds,
		UploadIntervalSeconds:    cfg.UploadIntervalSeconds,
		CollectIntervalSeconds:   cfg.CollectIntervalSeconds,
		MonitoringProfile:        cfg.APM.Profile,
		Labels:                   cfg.Labels,
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
	}
}

func ControllerStatus(ctx context.Context, controllerURL string) ControllerSnapshot {
	out := ControllerSnapshot{URL: controllerURL, Status: "unknown"}
	if controllerURL == "" {
		out.Message = "controller URL is empty"
		return out
	}
	reqCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, controllerURL, nil)
	if err != nil {
		out.Message = err.Error()
		return out
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		out.Status = "offline"
		out.Message = err.Error()
		return out
	}
	_ = resp.Body.Close()
	out.Reachable = resp.StatusCode >= 200 && resp.StatusCode < 500
	out.Status = resp.Status
	return out
}

func TailLines(path string, maxLines int) []string {
	if maxLines <= 0 {
		maxLines = 100
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	text := strings.ReplaceAll(string(b), "\r\n", "\n")
	lines := strings.Split(strings.TrimSpace(text), "\n")
	if len(lines) > maxLines {
		lines = lines[len(lines)-maxLines:]
	}
	return lines
}

func readIdentity(path string) (identity.Identity, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return identity.Identity{}, err
	}
	var id identity.Identity
	if err := json.Unmarshal(b, &id); err != nil {
		return identity.Identity{}, err
	}
	return id, nil
}

func HealthText(s Snapshot) (string, string) {
	uploadError := ""
	heartbeatError := ""
	if s.Status != nil {
		uploadError = s.Status.LastUploadError
		heartbeatError = s.Status.LastHeartbeatError
	}
	switch {
	case s.Service.Installed && !s.Service.Running:
		return "Service " + strings.ToLower(valueOr(s.Service.State, "stopped")), "bad"
	case s.Status == nil:
		return "Waiting for agent", "warn"
	case statusIsStale(s):
		return "Agent stale", "warn"
	case s.Status.AuthState == "pending" || s.Status.AuthState == "unenrolled":
		return "Pending authorization", "warn"
	case s.Status.AuthState == "revoked":
		return "Authorization revoked", "bad"
	case s.Status.AuthState == "unauthorized":
		return "Authorization required", "bad"
	case !s.Controller.Reachable:
		return "Controller unreachable", "bad"
	case s.Status.LastHeartbeat == nil:
		return "Connecting", "warn"
	case s.Status.LastConfigError != "":
		return "Configuration degraded", "bad"
	case len(s.Status.CollectorErrors) > 0:
		return "Collector degraded", "bad"
	case s.Config.MonitoringProfile != "apm" && infrastructureCollectorsDisabled(s.Config.CollectorEnabled):
		return "Server collectors disabled", "bad"
	case uploadError != "" || heartbeatError != "":
		return "Spooling locally", "warn"
	case s.Spool.Depth > 0:
		return "Queue draining", "warn"
	default:
		return "Healthy", "ok"
	}
}

func infrastructureCollectorsDisabled(enabled map[string]bool) bool {
	if len(enabled) == 0 {
		return false
	}
	for _, name := range []string{"cpu", "memory", "filesystem", "disk_io", "network", "processes", "services", "event_log"} {
		if enabled[name] {
			return false
		}
	}
	return true
}

func statusIsStale(s Snapshot) bool {
	if s.Status == nil {
		return false
	}
	last := latestTime(timePointer(s.PublishedAt), s.Status.LastHeartbeat, s.Status.LastUpload, s.Status.LastCollection)
	if last == nil {
		return false
	}
	threshold := time.Duration(maxInt(s.Config.HeartbeatIntervalSeconds, s.Config.UploadIntervalSeconds, s.Config.CollectIntervalSeconds))*time.Second + 2*time.Minute
	if threshold < 3*time.Minute {
		threshold = 3 * time.Minute
	}
	return s.Now.Sub(*last) > threshold
}

func timePointer(value time.Time) *time.Time {
	if value.IsZero() {
		return nil
	}
	return &value
}

func latestTime(values ...*time.Time) *time.Time {
	var out *time.Time
	for _, value := range values {
		if value == nil || value.IsZero() {
			continue
		}
		if out == nil || value.After(*out) {
			out = value
		}
	}
	return out
}

func valueOr(value string, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func maxInt(values ...int) int {
	max := 0
	for _, value := range values {
		if value > max {
			max = value
		}
	}
	return max
}

func Bytes(value int64) string {
	if value <= 0 {
		return "0 B"
	}
	units := []string{"B", "KB", "MB", "GB", "TB"}
	size := float64(value)
	unit := 0
	for size >= 1024 && unit < len(units)-1 {
		size /= 1024
		unit++
	}
	if size >= 10 || unit == 0 {
		return fmt.Sprintf("%.0f %s", size, units[unit])
	}
	return fmt.Sprintf("%.1f %s", size, units[unit])
}

func TimeAgo(t *time.Time) string {
	if t == nil || t.IsZero() {
		return "-"
	}
	return DurationAgo(time.Since(*t))
}

func DurationAgo(d time.Duration) string {
	if d < 0 {
		d = 0
	}
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd", int(d.Hours()/24))
	}
}
