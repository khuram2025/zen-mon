package config

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

const (
	DefaultControllerURL = "https://192.168.8.221"
	DefaultConfigPath    = "config/agent.yaml"
)

// Baked in at build time via
//
//	-ldflags "-X zenplus-agent/internal/config.embeddedControllerURL=..."
//
// The controller URL remains overridable through agent.yaml or MSI properties.
var embeddedControllerURL string

type Config struct {
	Version                  int               `yaml:"version" json:"version"`
	ConfigVersion            int               `yaml:"config_version,omitempty" json:"config_version,omitempty"`
	ConfigETag               string            `yaml:"config_etag,omitempty" json:"config_etag,omitempty"`
	AgentID                  string            `yaml:"agent_id,omitempty" json:"agent_id,omitempty"`
	ServerID                 string            `yaml:"server_id,omitempty" json:"server_id,omitempty"`
	AgentName                string            `yaml:"agent_name,omitempty" json:"agent_name,omitempty"`
	ControllerURL            string            `yaml:"controller_url" json:"controller_url"`
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
	APM                      APMConfig         `yaml:"apm" json:"apm"`
	Collectors               CollectorConfig   `yaml:"collectors" json:"collectors"`
	Spool                    SpoolConfig       `yaml:"spool" json:"spool"`
	Security                 SecurityConfig    `yaml:"security" json:"security"`
	Limits                   LimitsConfig      `yaml:"limits" json:"limits"`
	Labels                   map[string]string `yaml:"labels,omitempty" json:"labels,omitempty"`
	Extra                    map[string]any    `yaml:"extra,omitempty" json:"extra,omitempty"`
}

type APMConfig struct {
	// Enabled controls the managed local telemetry gateway. The appliance
	// remains the authority for authorization and its scoped ingest credential.
	Enabled     bool   `yaml:"enabled" json:"enabled"`
	Profile     string `yaml:"profile,omitempty" json:"profile,omitempty"`
	Environment string `yaml:"environment,omitempty" json:"environment,omitempty"`
	BindAddress string `yaml:"bind_address,omitempty" json:"bind_address,omitempty"`
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
	Enabled         bool              `yaml:"enabled" json:"enabled"`
	IntervalSeconds int               `yaml:"interval_seconds,omitempty" json:"interval_seconds,omitempty"`
	Filters         *[]EventLogFilter `yaml:"filters,omitempty" json:"filters,omitempty"`
	Channels        []string          `yaml:"channels" json:"channels"`
	Levels          []string          `yaml:"levels" json:"levels"`
	LookbackMinutes int               `yaml:"lookback_minutes" json:"lookback_minutes"`
}

// EventLogFilter is the controller policy representation retained in the
// local config. A pointer to the filter slice above distinguishes an absent
// filters field (legacy channels/levels configuration) from an explicit empty
// controller filter list.
type EventLogFilter struct {
	Channel string   `yaml:"channel" json:"channel"`
	Levels  []string `yaml:"levels" json:"levels"`
	IDs     []int    `yaml:"ids,omitempty" json:"ids,omitempty"`
}

type SpoolConfig struct {
	MaxBytes    int64 `yaml:"max_bytes" json:"max_bytes"`
	MaxAgeHours int   `yaml:"max_age_hours" json:"max_age_hours"`
}

type SecurityConfig struct {
	RequireSignedConfig    bool   `yaml:"require_signed_config" json:"require_signed_config"`
	ConfigPublicKey        string `yaml:"config_public_key,omitempty" json:"config_public_key,omitempty"`
	ControllerCAFile       string `yaml:"controller_ca_file,omitempty" json:"controller_ca_file,omitempty"`
	AllowInsecureTransport bool   `yaml:"allow_insecure_transport,omitempty" json:"allow_insecure_transport,omitempty"`
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
		VerifyTLS:                true,
		DataDir:                  "data",
		HeartbeatIntervalSeconds: 30,
		UploadIntervalSeconds:    60,
		ConfigIntervalSeconds:    60,
		CollectIntervalSeconds:   60,
		CollectorTimeoutSeconds:  20,
		UpdateRing:               "stable",
		CompressUploads:          false,
		APM: APMConfig{
			Enabled: true, Profile: "combined", Environment: "prod", BindAddress: "127.0.0.1",
		},
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
	if cfg.Security.ControllerCAFile != "" && !filepath.IsAbs(cfg.Security.ControllerCAFile) {
		base := filepath.Dir(path)
		cfg.Security.ControllerCAFile = filepath.Clean(filepath.Join(base, cfg.Security.ControllerCAFile))
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
	// Releases before TLS enforcement commonly stored an http:// appliance URL
	// and relied on nginx to redirect every agent request to HTTPS. Rejecting
	// that legacy file prevents the upgraded service from starting at all.
	// When insecure transport was not explicitly allowed, upgrade the scheme;
	// certificate trust remains enforced by the Windows trust store plus any
	// explicitly configured controller CA bundle.
	if migrated, ok := migrateLegacyControllerURL(cfg.ControllerURL, cfg.Security.AllowInsecureTransport); ok {
		cfg.ControllerURL = migrated
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func migrateLegacyControllerURL(rawURL string, allowInsecure bool) (string, bool) {
	if allowInsecure {
		return rawURL, false
	}
	parsed, err := url.ParseRequestURI(strings.TrimSpace(rawURL))
	if err != nil || !strings.EqualFold(parsed.Scheme, "http") || parsed.Host == "" {
		return rawURL, false
	}
	host := parsed.Hostname()
	isLoopback := strings.EqualFold(host, "localhost")
	if ip := net.ParseIP(host); ip != nil {
		isLoopback = ip.IsLoopback()
	}
	if isLoopback {
		return rawURL, false
	}
	parsed.Scheme = "https"
	return parsed.String(), true
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
		rawURL = "https://" + rawURL
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
	if err := cfg.Validate(); err != nil {
		return err
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

func (c *Config) Validate() error {
	if c == nil {
		return errors.New("config is required")
	}
	if c.Version == 0 {
		c.Version = 1
	}
	// Managed instrumentation has historically exported to IPv4 loopback.
	// Normalize the legacy IPv6 option so old configs keep working without
	// splitting the gateway receiver from every injected runtime endpoint.
	c.APM.BindAddress = strings.TrimSpace(c.APM.BindAddress)
	if c.APM.BindAddress == "" || c.APM.BindAddress == "::1" {
		c.APM.BindAddress = "127.0.0.1"
	}
	if c.ControllerURL == "" {
		return errors.New("controller_url is required")
	}
	parsed, err := url.ParseRequestURI(c.ControllerURL)
	if err != nil {
		return fmt.Errorf("invalid controller_url: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return errors.New("controller_url must use http or https")
	}
	if parsed.Host == "" {
		return errors.New("controller_url must include a host")
	}
	isLoopback := strings.EqualFold(parsed.Hostname(), "localhost")
	if ip := net.ParseIP(parsed.Hostname()); ip != nil {
		isLoopback = ip.IsLoopback()
	}
	if parsed.Scheme == "http" && !isLoopback && !c.Security.AllowInsecureTransport {
		return errors.New("controller_url must use https for non-loopback connections")
	}
	if parsed.Scheme == "https" && !c.VerifyTLS && !c.Security.AllowInsecureTransport {
		return errors.New("verify_tls cannot be disabled unless security.allow_insecure_transport is explicitly enabled")
	}
	if strings.TrimSpace(c.Security.ControllerCAFile) != "" && !c.VerifyTLS {
		return errors.New("security.controller_ca_file requires verify_tls to be enabled")
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
	if c.APM.Profile != "" && c.APM.Profile != "infrastructure" && c.APM.Profile != "apm" && c.APM.Profile != "combined" {
		return errors.New("apm.profile must be infrastructure, apm, or combined")
	}
	if c.APM.BindAddress != "127.0.0.1" {
		return errors.New("apm.bind_address must be 127.0.0.1")
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
