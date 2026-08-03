package config

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

const (
	DefaultControllerURL = "http://192.168.8.221"
	DefaultConfigPath    = "config/agent.yaml"
)

// Baked in at build time via
//
//	-ldflags "-X zenplus-agent/internal/config.embeddedControllerURL=... \
//	          -X zenplus-agent/internal/config.embeddedEnrollmentToken=..."
//
// so the published MSI installs and enrolls with zero operator input. Both
// remain overridable afterwards through agent.yaml or MSI properties.
var (
	embeddedControllerURL   string
	embeddedEnrollmentToken string
)

// PlaceholderEnrollmentToken is the fixed-width default carried by the MSI's
// ENROLLMENT_TOKEN property. The controller rewrites it in place when an
// operator downloads the package. If it survives to the agent, the package
// was fetched without going through that flow, so it is treated as "no token"
// rather than attempted (and rejected) as a real one.
const PlaceholderEnrollmentToken = "zpa_enr_PLACEHOLDERTOKENPLACEHOLDERTOKEN"

// NormalizeEnrollmentToken trims a configured token and discards the
// un-substituted MSI placeholder.
func NormalizeEnrollmentToken(token string) string {
	token = strings.TrimSpace(token)
	if token == PlaceholderEnrollmentToken {
		return ""
	}
	return token
}

type Config struct {
	Version                  int               `yaml:"version" json:"version"`
	ConfigVersion            int               `yaml:"config_version,omitempty" json:"config_version,omitempty"`
	ConfigETag               string            `yaml:"config_etag,omitempty" json:"config_etag,omitempty"`
	AgentID                  string            `yaml:"agent_id,omitempty" json:"agent_id,omitempty"`
	ServerID                 string            `yaml:"server_id,omitempty" json:"server_id,omitempty"`
	AgentName                string            `yaml:"agent_name,omitempty" json:"agent_name,omitempty"`
	ControllerURL            string            `yaml:"controller_url" json:"controller_url"`
	EnrollmentToken          string            `yaml:"enrollment_token,omitempty" json:"enrollment_token,omitempty"`
	SiteID                   string            `yaml:"site_id,omitempty" json:"site_id,omitempty"`
	PolicyID                 string            `yaml:"policy_id,omitempty" json:"policy_id,omitempty"`
	ProxyURL                 string            `yaml:"proxy_url,omitempty" json:"proxy_url,omitempty"`
	VerifyTLS                bool              `yaml:"verify_tls" json:"verify_tls"`
	DataDir                  string            `yaml:"data_dir" json:"data_dir"`
	HeartbeatIntervalSeconds int               `yaml:"heartbeat_interval_seconds" json:"heartbeat_interval_seconds"`
	UploadIntervalSeconds    int               `yaml:"upload_interval_seconds" json:"upload_interval_seconds"`
	ConfigIntervalSeconds    int               `yaml:"config_interval_seconds" json:"config_interval_seconds"`
	CollectIntervalSeconds   int               `yaml:"collect_interval_seconds" json:"collect_interval_seconds"`
	CollectorTimeoutSeconds  int               `yaml:"collector_timeout_seconds" json:"collector_timeout_seconds"`
	UpdateRing               string            `yaml:"update_ring,omitempty" json:"update_ring,omitempty"`
	DiskIgnore               []string          `yaml:"disk_ignore,omitempty" json:"disk_ignore,omitempty"`
	NetworkIgnore            []string          `yaml:"network_ignore,omitempty" json:"network_ignore,omitempty"`
	CompressUploads          bool              `yaml:"compress_uploads,omitempty" json:"compress_uploads,omitempty"`
	Collectors               CollectorConfig   `yaml:"collectors" json:"collectors"`
	Spool                    SpoolConfig       `yaml:"spool" json:"spool"`
	Security                 SecurityConfig    `yaml:"security" json:"security"`
	Limits                   LimitsConfig      `yaml:"limits" json:"limits"`
	Labels                   map[string]string `yaml:"labels,omitempty" json:"labels,omitempty"`
	Extra                    map[string]any    `yaml:"extra,omitempty" json:"extra,omitempty"`
}

type CollectorConfig struct {
	CPU        CollectorSwitch `yaml:"cpu" json:"cpu"`
	Memory     CollectorSwitch `yaml:"memory" json:"memory"`
	Filesystem CollectorSwitch `yaml:"filesystem" json:"filesystem"`
	DiskIO     CollectorSwitch `yaml:"disk_io" json:"disk_io"`
	Network    CollectorSwitch `yaml:"network" json:"network"`
	Processes  ProcessConfig   `yaml:"processes" json:"processes"`
	Services   WatchConfig     `yaml:"services" json:"services"`
	EventLog   EventLogConfig  `yaml:"event_log" json:"event_log"`
	Inventory  CollectorSwitch `yaml:"inventory" json:"inventory"`
}

type CollectorSwitch struct {
	Enabled         bool `yaml:"enabled" json:"enabled"`
	IntervalSeconds int  `yaml:"interval_seconds,omitempty" json:"interval_seconds,omitempty"`
}

type ProcessConfig struct {
	Enabled         bool     `yaml:"enabled" json:"enabled"`
	IntervalSeconds int      `yaml:"interval_seconds,omitempty" json:"interval_seconds,omitempty"`
	TopN            int      `yaml:"top_n" json:"top_n"`
	Watchlist       []string `yaml:"watchlist,omitempty" json:"watchlist,omitempty"`
}

type WatchConfig struct {
	Enabled         bool     `yaml:"enabled" json:"enabled"`
	IntervalSeconds int      `yaml:"interval_seconds,omitempty" json:"interval_seconds,omitempty"`
	Watchlist       []string `yaml:"watchlist,omitempty" json:"watchlist,omitempty"`
}

type EventLogConfig struct {
	Enabled         bool     `yaml:"enabled" json:"enabled"`
	IntervalSeconds int      `yaml:"interval_seconds,omitempty" json:"interval_seconds,omitempty"`
	Channels        []string `yaml:"channels" json:"channels"`
	Levels          []string `yaml:"levels" json:"levels"`
	LookbackMinutes int      `yaml:"lookback_minutes" json:"lookback_minutes"`
}

type SpoolConfig struct {
	MaxBytes    int64 `yaml:"max_bytes" json:"max_bytes"`
	MaxAgeHours int   `yaml:"max_age_hours" json:"max_age_hours"`
}

type SecurityConfig struct {
	RequireSignedConfig bool   `yaml:"require_signed_config" json:"require_signed_config"`
	ConfigPublicKey     string `yaml:"config_public_key,omitempty" json:"config_public_key,omitempty"`
}

type LimitsConfig struct {
	MaxProcessCount int `yaml:"max_process_count" json:"max_process_count"`
	MaxPayloadBytes int `yaml:"max_payload_bytes" json:"max_payload_bytes"`
}

func Default() Config {
	controllerURL := DefaultControllerURL
	if embeddedControllerURL != "" {
		if normalized, err := NormalizeControllerURL(embeddedControllerURL); err == nil {
			controllerURL = normalized
		}
	}
	return Config{
		Version:                  1,
		ControllerURL:            controllerURL,
		EnrollmentToken:          NormalizeEnrollmentToken(embeddedEnrollmentToken),
		VerifyTLS:                true,
		DataDir:                  "data",
		HeartbeatIntervalSeconds: 30,
		UploadIntervalSeconds:    60,
		ConfigIntervalSeconds:    60,
		CollectIntervalSeconds:   60,
		CollectorTimeoutSeconds:  20,
		UpdateRing:               "stable",
		CompressUploads:          false,
		Collectors: CollectorConfig{
			CPU:        CollectorSwitch{Enabled: true, IntervalSeconds: 60},
			Memory:     CollectorSwitch{Enabled: true, IntervalSeconds: 60},
			Filesystem: CollectorSwitch{Enabled: true, IntervalSeconds: 60},
			DiskIO:     CollectorSwitch{Enabled: true, IntervalSeconds: 60},
			Network:    CollectorSwitch{Enabled: true, IntervalSeconds: 60},
			Processes:  ProcessConfig{Enabled: true, IntervalSeconds: 120, TopN: 10},
			Services:   WatchConfig{Enabled: true, IntervalSeconds: 60, Watchlist: []string{"MSSQLSERVER", "W3SVC"}},
			EventLog: EventLogConfig{
				Enabled:         true,
				IntervalSeconds: 60,
				Channels:        []string{"System", "Application"},
				Levels:          []string{"Critical", "Error", "Warning"},
				LookbackMinutes: 5,
			},
			Inventory: CollectorSwitch{Enabled: true, IntervalSeconds: 21600},
		},
		Spool: SpoolConfig{
			MaxBytes:    256 * 1024 * 1024,
			MaxAgeHours: 24,
		},
		Security: SecurityConfig{
			RequireSignedConfig: false,
		},
		Limits: LimitsConfig{
			MaxProcessCount: 10,
			MaxPayloadBytes: 2 * 1024 * 1024,
		},
		Labels: map[string]string{},
	}
}

// HasEmbeddedEnrollmentToken reports whether this build carries a compiled-in
// bootstrap token. Such a build can re-enroll itself after losing its
// credential file, so the token is never fully "cleared" — only removed from
// the on-disk config.
func HasEmbeddedEnrollmentToken() bool {
	return NormalizeEnrollmentToken(embeddedEnrollmentToken) != ""
}

func ClearEnrollmentToken(path string) error {
	cfg, err := LoadForEdit(path)
	if err != nil {
		return err
	}
	if cfg.EnrollmentToken == "" {
		return nil
	}
	cfg.EnrollmentToken = ""
	return Save(path, cfg)
}

func SetEnrollmentToken(path string, token string) (Config, error) {
	cfg, err := LoadForEdit(path)
	if err != nil {
		return Config{}, err
	}
	cfg.EnrollmentToken = strings.TrimSpace(token)
	if cfg.EnrollmentToken == "" {
		return Config{}, errors.New("enrollment token is required")
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, Save(path, cfg)
}

func Load(path string) (Config, error) {
	if path == "" {
		path = DefaultConfigPath
	}
	cfg, err := LoadForEdit(path)
	if err != nil {
		return Config{}, err
	}
	if cfg.DataDir != "" && !filepath.IsAbs(cfg.DataDir) {
		base := filepath.Dir(path)
		cfg.DataDir = filepath.Clean(filepath.Join(base, cfg.DataDir))
	}
	return cfg, nil
}

func LoadForEdit(path string) (Config, error) {
	if path == "" {
		path = DefaultConfigPath
	}
	cfg := Default()
	b, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return cfg, nil
		}
		return Config{}, err
	}
	if err := yaml.Unmarshal(b, &cfg); err != nil {
		return Config{}, fmt.Errorf("parse config: %w", err)
	}
	cfg.EnrollmentToken = NormalizeEnrollmentToken(cfg.EnrollmentToken)
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func SetControllerURL(path string, rawURL string) (Config, error) {
	cfg, err := LoadForEdit(path)
	if err != nil {
		return Config{}, err
	}
	normalized, err := NormalizeControllerURL(rawURL)
	if err != nil {
		return Config{}, err
	}
	cfg.ControllerURL = normalized
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, Save(path, cfg)
}

func NormalizeControllerURL(rawURL string) (string, error) {
	rawURL = strings.TrimSpace(rawURL)
	if rawURL == "" {
		return "", errors.New("controller URL is required")
	}
	if !strings.Contains(rawURL, "://") {
		rawURL = "http://" + rawURL
	}
	parsed, err := url.ParseRequestURI(rawURL)
	if err != nil {
		return "", fmt.Errorf("invalid controller_url: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("controller_url must use http or https")
	}
	if parsed.Host == "" {
		return "", fmt.Errorf("controller_url must include a host")
	}
	parsed.Path = strings.TrimRight(parsed.Path, "/")
	return strings.TrimRight(parsed.String(), "/"), nil
}

func Save(path string, cfg Config) error {
	if path == "" {
		path = DefaultConfigPath
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := yaml.Marshal(cfg)
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o600)
}

func (c Config) Validate() error {
	if c.Version == 0 {
		c.Version = 1
	}
	if c.ControllerURL == "" {
		return errors.New("controller_url is required")
	}
	if _, err := url.ParseRequestURI(c.ControllerURL); err != nil {
		return fmt.Errorf("invalid controller_url: %w", err)
	}
	if c.HeartbeatIntervalSeconds <= 0 {
		return errors.New("heartbeat_interval_seconds must be positive")
	}
	if c.UploadIntervalSeconds <= 0 {
		return errors.New("upload_interval_seconds must be positive")
	}
	if c.CollectIntervalSeconds <= 0 {
		return errors.New("collect_interval_seconds must be positive")
	}
	if c.CollectorTimeoutSeconds <= 0 {
		return errors.New("collector_timeout_seconds must be positive")
	}
	if c.Spool.MaxBytes <= 0 {
		return errors.New("spool.max_bytes must be positive")
	}
	if c.Spool.MaxAgeHours <= 0 {
		return errors.New("spool.max_age_hours must be positive")
	}
	return nil
}

func Duration(seconds int, fallback time.Duration) time.Duration {
	if seconds <= 0 {
		return fallback
	}
	return time.Duration(seconds) * time.Second
}

func Hash(c Config) string {
	if c.ConfigETag != "" {
		return c.ConfigETag
	}
	b, _ := json.Marshal(c)
	sum := sha256.Sum256(b)
	return strings.ToLower(hex.EncodeToString(sum[:]))
}
