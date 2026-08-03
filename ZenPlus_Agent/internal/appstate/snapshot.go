package appstate

import (
	"context"
	"encoding/json"
	"errors"
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
	"zenplus-agent/internal/spool"
)

type Snapshot struct {
	Now        time.Time
	Status     *model.Status
	Identity   *identity.Identity
	Service    ServiceSnapshot
	Controller ControllerSnapshot
	Spool      SpoolSnapshot
	Latest     *BatchSnapshot
	Config     ConfigSnapshot
	Logs       []string
	Paths      PathSnapshot
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

type BatchSnapshot struct {
	BatchID       string
	CollectedAt   time.Time
	MetricCount   int
	EventCount    int
	HealthStatus  string
	SampleMetrics []model.Metric
}

type ConfigSnapshot struct {
	ControllerURL             string
	SiteID                    string
	PolicyID                  string
	EnrollmentTokenConfigured bool
	VerifyTLS                 bool
	DataDir                   string
	HeartbeatIntervalSeconds  int
	UploadIntervalSeconds     int
	CollectIntervalSeconds    int
	CollectorEnabled          map[string]bool
	Labels                    map[string]string
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
	}
	if id, err := readIdentity(paths.IdentityFile); err == nil {
		snap.Identity = &id
	}
	if stats, latest, err := readSpool(paths.SpoolDB); err == nil {
		snap.Spool.Depth = stats.Depth
		snap.Spool.Bytes = stats.Bytes
		snap.Latest = latest
	} else if !errors.Is(err, os.ErrNotExist) {
		snap.Spool.Error = err.Error()
	}
	return snap
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
		ControllerURL:             cfg.ControllerURL,
		SiteID:                    cfg.SiteID,
		PolicyID:                  cfg.PolicyID,
		EnrollmentTokenConfigured: cfg.EnrollmentToken != "",
		VerifyTLS:                 cfg.VerifyTLS,
		DataDir:                   cfg.DataDir,
		HeartbeatIntervalSeconds:  cfg.HeartbeatIntervalSeconds,
		UploadIntervalSeconds:     cfg.UploadIntervalSeconds,
		CollectIntervalSeconds:    cfg.CollectIntervalSeconds,
		Labels:                    cfg.Labels,
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

func readSpool(path string) (spool.Stats, *BatchSnapshot, error) {
	store, err := spool.OpenReadOnly(path)
	if err != nil {
		return spool.Stats{}, nil, err
	}
	defer store.Close()
	stats, err := store.Stats()
	if err != nil {
		return spool.Stats{}, nil, err
	}
	records, err := store.PeekLatest(1)
	if err != nil || len(records) == 0 {
		return stats, nil, err
	}
	var batch model.Batch
	if err := json.Unmarshal(records[0].Payload, &batch); err != nil {
		return stats, nil, err
	}
	sample := batch.Metrics
	if len(sample) > 8 {
		sample = sample[:8]
	}
	healthStatus := batch.Health.Status
	if healthStatus == "" {
		healthStatus = healthFromSamples(batch.Metrics)
	}
	return stats, &BatchSnapshot{
		BatchID:       batch.BatchID,
		CollectedAt:   batch.CollectedAt,
		MetricCount:   len(batch.Metrics),
		EventCount:    len(batch.Events),
		HealthStatus:  healthStatus,
		SampleMetrics: sample,
	}, nil
}

func healthFromSamples(metrics []model.Metric) string {
	for _, metric := range metrics {
		if metric.Kind != "agent_health" {
			continue
		}
		if ok, _ := metric.Data["config_apply_ok"].(bool); !ok {
			return "degraded"
		}
		if last, _ := metric.Data["last_error"].(string); last != "" {
			return "degraded"
		}
		return "ok"
	}
	return ""
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
	case !s.Controller.Reachable:
		return "Controller offline", "bad"
	case uploadError != "" || heartbeatError != "":
		return "Spooling locally", "warn"
	case s.Spool.Depth > 0:
		return "Queue draining", "warn"
	default:
		return "Healthy", "ok"
	}
}

func statusIsStale(s Snapshot) bool {
	if s.Status == nil {
		return false
	}
	last := latestTime(s.Status.LastHeartbeat, s.Status.LastUpload, s.Status.LastCollection)
	if last == nil {
		return true
	}
	threshold := time.Duration(maxInt(s.Config.HeartbeatIntervalSeconds, s.Config.UploadIntervalSeconds, s.Config.CollectIntervalSeconds))*time.Second + 2*time.Minute
	if threshold < 3*time.Minute {
		threshold = 3 * time.Minute
	}
	return s.Now.Sub(*last) > threshold
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
