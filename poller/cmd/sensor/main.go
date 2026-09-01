package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/sensorspool"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

var (
	version   = "sensor-0.1.0"
	commit    = "unknown"
	buildDate = "unknown"
)

var errNotModified = errors.New("not modified")

type completedCommand struct {
	ID        string    `json:"id"`
	Verb      string    `json:"verb"`
	EventType string    `json:"event_type"`
	Message   string    `json:"message,omitempty"`
	Version   string    `json:"version,omitempty"`
	HandledAt time.Time `json:"handled_at"`
}

type state struct {
	SensorID             string             `json:"sensor_id"`
	APIKey               string             `json:"api_key"`
	ETag                 string             `json:"etag"`
	LastGoodConfig       *configResponse    `json:"last_good_config,omitempty"`
	AuthorizationBlocked bool               `json:"authorization_blocked,omitempty"`
	ControllerCASHA256   string             `json:"controller_ca_sha256,omitempty"`
	CompletedCommands    []completedCommand `json:"completed_commands,omitempty"`
}

type configResponse struct {
	ETag          string               `json:"etag"`
	SensorID      string               `json:"sensor_id"`
	SensorName    string               `json:"sensor_name"`
	Devices       []configDevice       `json:"devices"`
	ServiceChecks []configServiceCheck `json:"service_checks"`
}

type configDevice struct {
	ID           string `json:"id"`
	Hostname     string `json:"hostname"`
	IPAddress    string `json:"ip_address"`
	PingEnabled  bool   `json:"ping_enabled"`
	PingInterval int    `json:"ping_interval"`
	SNMPEnabled  bool   `json:"snmp_enabled"`
}

type configServiceCheck struct {
	ID                    string            `json:"id"`
	Name                  string            `json:"name"`
	CheckType             string            `json:"check_type"`
	TargetHost            string            `json:"target_host"`
	TargetPort            int               `json:"target_port"`
	TargetURL             string            `json:"target_url"`
	HTTPMethod            string            `json:"http_method"`
	HTTPHeaders           map[string]string `json:"http_headers"`
	HTTPBody              string            `json:"http_body"`
	HTTPExpectedStatus    int               `json:"http_expected_status"`
	HTTPExpectedStatuses  string            `json:"http_expected_statuses"`
	HTTPContentMatch      string            `json:"http_content_match"`
	HTTPFollowRedirects   *bool             `json:"http_follow_redirects"`
	HTTPIgnoreTLSErrors   bool              `json:"http_ignore_tls_errors"`
	HTTPAllowInsecureAuth bool              `json:"http_allow_insecure_auth"`
	Config                map[string]any    `json:"config"`
	TLSWarnDays           int               `json:"tls_warn_days"`
	TLSCriticalDays       int               `json:"tls_critical_days"`
	CheckInterval         int               `json:"check_interval"`
	Timeout               int               `json:"timeout"`
	RetryCount            int               `json:"retry_count"`
	RetryDelayS           int               `json:"retry_delay_s"`
	Enabled               bool              `json:"enabled"`
}

type client struct {
	baseURL    string
	httpClient *http.Client
	state      *state
}

type heartbeatCommand struct {
	ID      string          `json:"id"`
	Verb    string          `json:"verb"`
	Payload json.RawMessage `json:"payload"`
}

type heartbeatResponse struct {
	OK          bool               `json:"ok"`
	HasCommands bool               `json:"has_commands"`
	Commands    []heartbeatCommand `json:"commands"`
	ConfigETag  string             `json:"config_etag"`
	MinVersion  string             `json:"min_supported_version"`
}

// configCache publishes immutable last-known-good snapshots to the scheduler.
// An ETag is exposed only when its matching payload is present, which lets
// sensors upgrade safely from the legacy credentials-only state file.
type configCache struct {
	mu     sync.RWMutex
	config *configResponse
	etag   string
}

func newConfigCache(st *state) *configCache {
	cache := &configCache{}
	if st != nil && st.LastGoodConfig != nil {
		if st.LastGoodConfig.SensorID != "" && st.SensorID != "" && st.LastGoodConfig.SensorID != st.SensorID {
			return cache
		}
		copy := cloneConfig(*st.LastGoodConfig)
		cache.config = &copy
		cache.etag = st.ETag
		if cache.etag == "" {
			cache.etag = copy.ETag
		}
	}
	return cache
}

func (c *configCache) Snapshot() (configResponse, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.config == nil {
		return configResponse{}, false
	}
	return cloneConfig(*c.config), true
}

func (c *configCache) ETag() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.etag
}

func (c *configCache) Store(next configResponse) {
	copy := cloneConfig(next)
	c.mu.Lock()
	c.config = &copy
	c.etag = next.ETag
	c.mu.Unlock()
}

func cloneConfig(in configResponse) configResponse {
	out := in
	out.Devices = append([]configDevice(nil), in.Devices...)
	out.ServiceChecks = append([]configServiceCheck(nil), in.ServiceChecks...)
	for i := range out.ServiceChecks {
		out.ServiceChecks[i].HTTPHeaders = cloneStringMap(in.ServiceChecks[i].HTTPHeaders)
		out.ServiceChecks[i].Config = cloneJSONMap(in.ServiceChecks[i].Config)
		if in.ServiceChecks[i].HTTPFollowRedirects != nil {
			follow := *in.ServiceChecks[i].HTTPFollowRedirects
			out.ServiceChecks[i].HTTPFollowRedirects = &follow
		}
	}
	return out
}

type persistedState struct {
	mu      sync.Mutex
	path    string
	current *state
}

func newPersistedState(path string, current *state) *persistedState {
	return &persistedState{path: path, current: current}
}

func (s *persistedState) Snapshot() state {
	s.mu.Lock()
	defer s.mu.Unlock()
	return cloneState(*s.current)
}

func (s *persistedState) Update(update func(*state)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	next := cloneState(*s.current)
	update(&next)
	// Runtime updates never rotate identity or transport trust. Keeping those
	// fields immutable also lets request hot paths read them lock-free.
	if next.SensorID != s.current.SensorID || next.APIKey != s.current.APIKey || next.ControllerCASHA256 != s.current.ControllerCASHA256 {
		return errors.New("runtime state update attempted to change immutable credentials")
	}
	if err := saveState(s.path, &next); err != nil {
		return err
	}
	s.current.ETag = next.ETag
	s.current.LastGoodConfig = next.LastGoodConfig
	s.current.AuthorizationBlocked = next.AuthorizationBlocked
	s.current.CompletedCommands = next.CompletedCommands
	return nil
}

func cloneState(in state) state {
	out := in
	out.CompletedCommands = append([]completedCommand(nil), in.CompletedCommands...)
	if in.LastGoodConfig != nil {
		config := cloneConfig(*in.LastGoodConfig)
		out.LastGoodConfig = &config
	}
	return out
}

func cloneStringMap(in map[string]string) map[string]string {
	if in == nil {
		return nil
	}
	out := make(map[string]string, len(in))
	for key, value := range in {
		out[key] = value
	}
	return out
}

func cloneJSONMap(in map[string]any) map[string]any {
	if in == nil {
		return nil
	}
	out := make(map[string]any, len(in))
	for key, value := range in {
		out[key] = cloneJSONValue(value)
	}
	return out
}

func cloneJSONValue(in any) any {
	switch value := in.(type) {
	case map[string]any:
		return cloneJSONMap(value)
	case []any:
		out := make([]any, len(value))
		for i := range value {
			out[i] = cloneJSONValue(value[i])
		}
		return out
	default:
		return value
	}
}

func newSensorHTTPClient(st *state) (*http.Client, error) {
	pin, err := normalizeCertificateSHA256(st.ControllerCASHA256)
	if err != nil {
		return nil, err
	}
	st.ControllerCASHA256 = pin
	// Normal WebPKI/private-CA validation is mandatory for enrollment and for
	// every authenticated request. A controller trust-anchor pin augments that
	// validation; it never replaces or weakens it.
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	tlsConfig.VerifyConnection = func(connection tls.ConnectionState) error {
		return verifyPinnedPeer(st.ControllerCASHA256, connection)
	}
	return &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			Proxy:           http.ProxyFromEnvironment,
			TLSClientConfig: tlsConfig,
			IdleConnTimeout: 90 * time.Second,
		},
	}, nil
}

func normalizeCertificateSHA256(value string) (string, error) {
	normalized, err := normalizeSHA256(value)
	if err != nil {
		return "", fmt.Errorf("controller CA SHA-256 pin: %w", err)
	}
	return normalized, nil
}

func normalizeSHA256(value string) (string, error) {
	value = strings.ToLower(strings.ReplaceAll(strings.TrimSpace(value), ":", ""))
	if value == "" {
		return "", nil
	}
	digest, err := hex.DecodeString(value)
	if err != nil || len(digest) != sha256.Size {
		return "", errors.New("must be a 32-byte hexadecimal digest")
	}
	return hex.EncodeToString(digest), nil
}

func verifyPinnedPeer(pin string, connection tls.ConnectionState) error {
	if pin == "" {
		return nil
	}
	expected, err := hex.DecodeString(pin)
	if err != nil || len(expected) != sha256.Size {
		return errors.New("controller certificate pin is invalid")
	}
	if len(connection.VerifiedChains) == 0 {
		return errors.New("controller certificate was not verified by the configured CA trust")
	}
	matched := 0
	for _, chain := range connection.VerifiedChains {
		for _, certificate := range chain {
			actual := sha256.Sum256(certificate.Raw)
			matched |= subtle.ConstantTimeCompare(expected, actual[:])
		}
	}
	if matched != 1 {
		return errors.New("controller CA SHA-256 pin mismatch")
	}
	return nil
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println(sensorVersion())
		return
	}
	logLevel := zap.NewAtomicLevelAt(zap.InfoLevel)
	logConfig := zap.NewProductionConfig()
	logConfig.Level = logLevel
	baseLogger := zap.Must(logConfig.Build())
	logger := baseLogger.Sugar()
	defer baseLogger.Sync()

	cfg, err := loadConfig()
	if err != nil {
		logger.Fatal(err)
	}

	st, err := loadState(cfg.statePath)
	if err != nil {
		logger.Fatal(err)
	}

	httpClient, err := newSensorHTTPClient(st)
	if err != nil {
		logger.Fatalf("configure controller TLS: %v", err)
	}
	c := &client{baseURL: strings.TrimRight(cfg.serverURL, "/"), httpClient: httpClient, state: st}

	if st.SensorID == "" || st.APIKey == "" {
		if cfg.enrollmentToken == "" {
			logger.Fatal("missing enrollment token and no saved sensor credentials")
		}
		// A partial legacy state cannot prove that its assignments belong to
		// the identity about to be enrolled.
		st.ETag = ""
		st.LastGoodConfig = nil
		if err := c.enroll(cfg); err != nil {
			logger.Fatalf("enroll failed: %v", err)
		}
		if err := saveState(cfg.statePath, st); err != nil {
			logger.Fatalf("save state failed: %v", err)
		}
		if err := clearEnrollmentToken(cfg.envFile); err != nil {
			logger.Warnf("could not clear enrollment token from env file: %v", err)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	spool, err := sensorspool.Open(filepath.Join(cfg.stateDir, "wal"), sensorspool.Options{
		MaxBytes: cfg.spoolMaxBytes,
		MaxAge:   cfg.spoolMaxAge,
	})
	if err != nil {
		logger.Fatalf("open result spool: %v", err)
	}
	configCache := newConfigCache(st)
	if cached, ok := configCache.Snapshot(); ok {
		logger.Infof("loaded cached config: devices=%d service_checks=%d etag=%s", len(cached.Devices), len(cached.ServiceChecks), configCache.ETag())
	}

	logger.Infof("zenplus sensor started: name=%s server=%s id=%s", cfg.sensorName, cfg.serverURL, st.SensorID)
	run(ctx, stop, logger, logLevel, cfg, c, st, configCache, spool)
}

type runtimeConfig struct {
	serverURL       string
	enrollmentToken string
	sensorName      string
	stateDir        string
	statePath       string
	envFile         string
	heartbeatEvery  time.Duration
	configEvery     time.Duration
	uploadEvery     time.Duration
	maxWorkers      int
	spoolMaxBytes   int64
	spoolMaxAge     time.Duration
	hostname        string
	operatingSystem string
	startedAt       time.Time
}

func loadConfig() (*runtimeConfig, error) {
	serverURL := strings.TrimSpace(env("ZENPLUS_SERVER_URL", ""))
	if serverURL == "" {
		return nil, errors.New("ZENPLUS_SERVER_URL is required")
	}
	if err := validateServerURL(serverURL); err != nil {
		return nil, err
	}
	name := env("ZENPLUS_SENSOR_NAME", "")
	if name == "" {
		host, _ := os.Hostname()
		name = host
	}
	stateDir := env("ZENPLUS_SENSOR_STATE_DIR", "/var/lib/zenplus-sensor")
	host, _ := os.Hostname()
	spoolMaxMB := intEnv("ZENPLUS_SPOOL_MAX_MB", 512)
	spoolRetentionHours := intEnv("ZENPLUS_SPOOL_RETENTION_HOURS", 72)
	return &runtimeConfig{
		serverURL:       serverURL,
		enrollmentToken: env("ZENPLUS_ENROLLMENT_TOKEN", ""),
		sensorName:      name,
		stateDir:        stateDir,
		statePath:       filepath.Join(stateDir, "state.json"),
		envFile:         env("ZENPLUS_SENSOR_ENV_FILE", "/etc/zenplus-sensor/sensor.env"),
		heartbeatEvery:  secondsEnv("ZENPLUS_HEARTBEAT_INTERVAL_SECONDS", 30),
		configEvery:     secondsEnv("ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS", 60),
		uploadEvery:     secondsEnv("ZENPLUS_UPLOAD_INTERVAL_SECONDS", 10),
		maxWorkers:      intEnv("ZENPLUS_MAX_WORKERS", 100),
		spoolMaxBytes:   int64(spoolMaxMB) * 1024 * 1024,
		spoolMaxAge:     time.Duration(spoolRetentionHours) * time.Hour,
		hostname:        host,
		operatingSystem: fmt.Sprintf("%s/%s", runtime.GOOS, runtime.GOARCH),
		startedAt:       time.Now(),
	}, nil
}

func validateServerURL(rawURL string) error {
	controller, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil || controller.Hostname() == "" || !controller.IsAbs() || controller.User != nil {
		return errors.New("ZENPLUS_SERVER_URL must be an absolute HTTPS controller URL")
	}
	if !strings.EqualFold(controller.Scheme, "https") {
		return errors.New("ZENPLUS_SERVER_URL must use HTTPS")
	}
	if controller.RawQuery != "" || controller.Fragment != "" || (controller.Path != "" && controller.Path != "/") {
		return errors.New("ZENPLUS_SERVER_URL must contain only the HTTPS controller origin")
	}
	return nil
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func intEnv(key string, fallback int) int {
	v, err := strconv.Atoi(env(key, ""))
	if err != nil || v <= 0 {
		return fallback
	}
	return v
}

func secondsEnv(key string, fallback int) time.Duration {
	return time.Duration(intEnv(key, fallback)) * time.Second
}

func loadState(path string) (*state, error) {
	st := &state{}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return st, nil
	}
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return st, nil
	}
	return st, json.Unmarshal(data, st)
}

func saveState(path string, st *state) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".state-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if err := tmp.Chmod(0600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, path); err != nil {
		// The appliance is Linux, where rename-over-existing is atomic. This
		// fallback keeps development and tests functional on Windows.
		if removeErr := os.Remove(path); removeErr != nil && !errors.Is(removeErr, os.ErrNotExist) {
			return err
		}
		if renameErr := os.Rename(tmpPath, path); renameErr != nil {
			return renameErr
		}
	}
	return syncStateDirectory(filepath.Dir(path))
}

func syncStateDirectory(path string) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func clearEnrollmentToken(path string) error {
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	lines := strings.Split(string(data), "\n")
	changed := false
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "ZENPLUS_ENROLLMENT_TOKEN=") {
			lines[i] = "ZENPLUS_ENROLLMENT_TOKEN=''"
			changed = true
		}
	}
	if !changed {
		return nil
	}
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".sensor-env-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if err := preserveFileOwnership(path, tmpPath); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(info.Mode().Perm()); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write([]byte(strings.Join(lines, "\n"))); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return err
	}
	return syncStateDirectory(filepath.Dir(path))
}

func sensorVersion() string {
	if commit == "" || commit == "unknown" {
		return version
	}
	return fmt.Sprintf("%s+%s", version, commit)
}

func (c *client) enroll(cfg *runtimeConfig) error {
	body := map[string]any{
		"enrollment_token": cfg.enrollmentToken,
		"hostname":         cfg.hostname,
		"os_info":          cfg.operatingSystem,
		"version":          sensorVersion(),
	}
	var out struct {
		SensorID           string `json:"sensor_id"`
		APIKey             string `json:"api_key"`
		ControllerCASHA256 string `json:"controller_ca_sha256"`
	}
	if err := c.doJSON(context.Background(), http.MethodPost, "/api/v1/sensor/enroll", body, &out, ""); err != nil {
		return err
	}
	pin := c.state.ControllerCASHA256
	if out.ControllerCASHA256 != "" {
		normalized, err := normalizeCertificateSHA256(out.ControllerCASHA256)
		if err != nil {
			return fmt.Errorf("invalid controller certificate pin in enrollment response: %w", err)
		}
		pin = normalized
	}
	c.state.SensorID = out.SensorID
	c.state.APIKey = out.APIKey
	c.state.ControllerCASHA256 = pin
	return nil
}

func (c *client) heartbeat(ctx context.Context, cfg *runtimeConfig, queueDepth, dropped int) (*heartbeatResponse, error) {
	body := map[string]any{
		"version":             sensorVersion(),
		"uptime_seconds":      int(time.Since(cfg.startedAt).Seconds()),
		"queue_depth":         queueDepth,
		"queue_dropped_count": dropped,
		"hostname":            cfg.hostname,
		"os_info":             cfg.operatingSystem,
	}
	var response heartbeatResponse
	if err := c.doJSON(ctx, http.MethodPost, "/api/v1/sensor/heartbeat", body, &response, ""); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *client) getConfig(ctx context.Context, etag string) (*configResponse, bool, error) {
	var out configResponse
	err := c.doJSON(ctx, http.MethodGet, "/api/v1/sensor/config", nil, &out, etag)
	if err != nil {
		if errors.Is(err, errNotModified) {
			return nil, false, nil
		}
		return nil, false, err
	}
	return &out, true, nil
}

func (c *client) postBatch(ctx context.Context, path, idempotencyKey string, items []json.RawMessage) error {
	if len(items) == 0 {
		return nil
	}
	return c.doJSON(ctx, http.MethodPost, path, map[string]any{
		"idempotency_key": idempotencyKey,
		"items":           items,
	}, nil, "")
}

type responseError struct {
	method     string
	path       string
	status     string
	statusCode int
	retryAfter time.Duration
	body       string
}

func (e *responseError) Error() string {
	return fmt.Sprintf("%s %s failed: %s %s", e.method, e.path, e.status, e.body)
}

func (c *client) doJSON(ctx context.Context, method, path string, body any, out any, etag string) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.state.SensorID != "" {
		req.Header.Set("X-Sensor-Id", c.state.SensorID)
	}
	if c.state.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.state.APIKey)
	}
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotModified {
		return errNotModified
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return &responseError{
			method: method, path: path, status: resp.Status, statusCode: resp.StatusCode,
			retryAfter: parseRetryAfter(resp.Header.Get("Retry-After"), time.Now()),
			body:       strings.TrimSpace(string(data)),
		}
	}
	if out == nil {
		io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func parseRetryAfter(value string, now time.Time) time.Duration {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0
	}
	if seconds, err := strconv.Atoi(value); err == nil && seconds >= 0 {
		return time.Duration(seconds) * time.Second
	}
	if at, err := http.ParseTime(value); err == nil && at.After(now) {
		return at.Sub(now)
	}
	return 0
}

const (
	pingResultsPath          = "/api/v1/sensor/results/ping"
	serviceResultsPath       = "/api/v1/sensor/results/service"
	eventsPath               = "/api/v1/sensor/events"
	uploadBatchSize          = 500
	maxDrainBatches          = 100
	defaultServiceRetryDelay = 30 * time.Second
	maxServiceRetryDelay     = 10 * time.Minute
	maxServiceCheckTimeout   = 60 * time.Second
	completedCommandLimit    = 128
	maxUpdateManifestBytes   = int64(1 * 1024 * 1024)
	maxUpdateBinaryBytes     = int64(256 * 1024 * 1024)
	releasePublicKeyPEM      = `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAhwZpk2+cPN57lhIbcsPAI3Xtx9MyfMPM5m3Ny81swF8=
-----END PUBLIC KEY-----`
)

func run(
	ctx context.Context,
	stop context.CancelFunc,
	logger *zap.SugaredLogger,
	logLevel zap.AtomicLevel,
	cfg *runtimeConfig,
	c *client,
	st *state,
	cache *configCache,
	spool *sensorspool.Spool,
) {
	stateStore := newPersistedState(cfg.statePath, st)
	authorization := newRuntimeAuthorization(st.AuthorizationBlocked, func(blocked bool) error {
		return stateStore.Update(func(next *state) {
			next.AuthorizationBlocked = blocked
		})
	})
	if authorization.Blocked() {
		logger.Warn("sensor authorization was previously rejected; cached probes remain paused until a fresh config response succeeds")
	}
	scheduler := newCheckScheduler(
		checker.NewChecker(logger),
		cfg.sensorName,
		cfg.maxWorkers,
		newResultEnqueuer(spool),
		logger,
	)
	reloadConfig := make(chan struct{}, 1)
	flushBuffer := make(chan struct{}, 1)
	commandBatches := make(chan []heartbeatCommand, 8)
	commands := newCommandProcessor(logger, logLevel, c, stateStore, newResultEnqueuer(spool), reloadConfig, flushBuffer, stop)

	var loops sync.WaitGroup
	loops.Add(5)
	go func() {
		defer loops.Done()
		heartbeatLoop(ctx, logger, cfg, c, spool, authorization, commandBatches)
	}()
	go func() {
		defer loops.Done()
		commandLoop(ctx, commands, commandBatches)
	}()
	go func() {
		defer loops.Done()
		configLoop(ctx, logger, cfg, c, stateStore, cache, authorization, reloadConfig)
	}()
	go func() {
		defer loops.Done()
		schedulerLoop(ctx, cache, scheduler, authorization)
	}()
	go func() {
		defer loops.Done()
		uploadLoop(ctx, logger, cfg, c, spool, authorization, flushBuffer)
	}()

	<-ctx.Done()
	loops.Wait()
	if !scheduler.Wait(65 * time.Second) {
		logger.Warn("timed out waiting for probe workers to stop")
	}
	logger.Info("sensor stopped")
}

func heartbeatLoop(ctx context.Context, logger *zap.SugaredLogger, cfg *runtimeConfig, c *client, spool *sensorspool.Spool, authorization *runtimeAuthorization, commandBatches chan<- []heartbeatCommand) {
	runPeriodic(ctx, cfg.heartbeatEvery, func(callCtx context.Context) {
		request := authorization.Begin()
		stats := spool.Stats()
		response, err := c.heartbeat(callCtx, cfg, stats.Depth, uint64ToInt(stats.Dropped))
		if err != nil {
			authorization.Observe(request, err, false, logger)
			logger.Warnf("heartbeat failed: %v", err)
		} else {
			authorization.Observe(request, nil, false, logger)
			if response != nil && len(response.Commands) > 0 {
				batch := append([]heartbeatCommand(nil), response.Commands...)
				select {
				case commandBatches <- batch:
				default:
					// The controller keeps incomplete commands available, so a full
					// local queue is safe to retry without delaying heartbeats.
					logger.Warnf("sensor command queue full; deferring %d command(s)", len(batch))
				}
			}
		}
	})
}

func commandLoop(ctx context.Context, processor *commandProcessor, batches <-chan []heartbeatCommand) {
	for {
		select {
		case <-ctx.Done():
			return
		case batch := <-batches:
			processor.Process(ctx, batch)
		}
	}
}

func configLoop(ctx context.Context, logger *zap.SugaredLogger, cfg *runtimeConfig, c *client, stateStore *persistedState, cache *configCache, authorization *runtimeAuthorization, trigger <-chan struct{}) {
	runTriggeredPeriodic(ctx, cfg.configEvery, trigger, func(callCtx context.Context) {
		request := authorization.Begin()
		next, changed, err := c.getConfig(callCtx, cache.ETag())
		if err != nil {
			authorization.Observe(request, err, true, logger)
			logger.Warnf("config pull failed; continuing with last-known-good config: %v", err)
			return
		}
		if !changed || next == nil {
			authorization.Observe(request, nil, true, logger)
			return
		}
		currentState := stateStore.Snapshot()
		if next.SensorID != "" && next.SensorID != currentState.SensorID {
			logger.Errorf("rejected config for sensor %s (this sensor is %s)", next.SensorID, currentState.SensorID)
			return
		}

		persisted := cloneConfig(*next)
		if err := stateStore.Update(func(proposed *state) {
			proposed.ETag = next.ETag
			proposed.LastGoodConfig = &persisted
		}); err != nil {
			logger.Errorf("persist config failed; keeping previous config active: %v", err)
			return
		}
		cache.Store(*next)
		authorization.Observe(request, nil, true, logger)
		logger.Infof("config loaded: devices=%d service_checks=%d etag=%s", len(next.Devices), len(next.ServiceChecks), next.ETag)
	})
}

func schedulerLoop(ctx context.Context, cache *configCache, scheduler *checkScheduler, authorization *runtimeAuthorization) {
	schedule := func(now time.Time) {
		if authorization.Blocked() {
			return
		}
		if current, ok := cache.Snapshot(); ok {
			scheduler.Schedule(ctx, current, now)
		}
	}
	schedule(time.Now())
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			schedule(now)
		}
	}
}

func uploadLoop(ctx context.Context, logger *zap.SugaredLogger, cfg *runtimeConfig, c *client, spool *sensorspool.Spool, authorization *runtimeAuthorization, flush <-chan struct{}) {
	failures := 0
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		case <-flush:
		}

		request := authorization.Begin()
		retryAfter, err := drainSpool(ctx, cfg.uploadEvery, c, spool, logger)
		delay := cfg.uploadEvery
		if err != nil && ctx.Err() == nil {
			authorization.Observe(request, err, false, logger)
			failures++
			delay = uploadRetryDelay(cfg.uploadEvery, failures, retryAfter)
			logger.Warnf("result upload failed; retrying in %s: %v", delay.Round(time.Second), err)
		} else {
			failures = 0
		}
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(delay)
	}
}

// runtimeAuthorization stops new probing after an authoritative controller
// rejection while heartbeat/config retries continue. The blocked state is
// persisted across restart and only a fresh successful config response clears
// it; heartbeat/upload success is deliberately insufficient.
type runtimeAuthorization struct {
	blocked atomic.Bool

	mu      sync.Mutex
	epoch   uint64
	persist func(bool) error
}

type authorizationRequest struct {
	epoch          uint64
	startedBlocked bool
}

func newRuntimeAuthorization(blocked bool, persist func(bool) error) *runtimeAuthorization {
	authorization := &runtimeAuthorization{persist: persist}
	authorization.blocked.Store(blocked)
	return authorization
}

func (a *runtimeAuthorization) Blocked() bool {
	return a.blocked.Load()
}

func (a *runtimeAuthorization) Begin() authorizationRequest {
	a.mu.Lock()
	request := authorizationRequest{epoch: a.epoch, startedBlocked: a.blocked.Load()}
	a.mu.Unlock()
	return request
}

func (a *runtimeAuthorization) Observe(request authorizationRequest, err error, allowRecovery bool, logger *zap.SugaredLogger) {
	var responseErr *responseError
	blockedResponse := errors.As(err, &responseErr) && isAuthorizationRejection(responseErr)
	success := err == nil
	if !blockedResponse && !success {
		return
	}

	a.mu.Lock()
	becameBlocked := false
	becameAllowed := false
	var persistErr error
	if blockedResponse {
		// Any explicit authorization rejection wins over concurrent successes.
		// Incrementing the epoch invalidates every recovery request that began
		// before this rejection was observed.
		a.epoch++
		if !a.blocked.Load() {
			a.blocked.Store(true)
			becameBlocked = true
		}
		// Retry persistence on every authoritative rejection so a transient
		// state-disk failure cannot make the restart guard permanently lossy.
		if a.persist != nil {
			persistErr = a.persist(true)
		}
	} else if allowRecovery && request.startedBlocked && request.epoch == a.epoch && a.blocked.Load() {
		if a.persist != nil {
			persistErr = a.persist(false)
		}
		if persistErr == nil {
			a.blocked.Store(false)
			becameAllowed = true
		}
	}
	a.mu.Unlock()

	if persistErr != nil {
		logger.Errorf("persist sensor authorization state failed: %v", persistErr)
	}
	if becameBlocked {
		logger.Error("controller rejected sensor authorization; pausing new probes while authentication retries continue")
	}
	if becameAllowed {
		logger.Info("sensor authorization restored; resuming probes")
	}
}

func isAuthorizationRejection(err *responseError) bool {
	if err == nil {
		return false
	}
	body := strings.ToLower(err.body)
	return err.statusCode == http.StatusUnauthorized ||
		(err.statusCode == http.StatusForbidden &&
			(strings.Contains(body, "sensor disabled") || strings.Contains(body, "sensor revoked")))
}

// drainSpool bounds each upload turn by both time and batch count. This keeps a
// recovered WAN backlog from monopolizing the process while still allowing up
// to 50,000 results to drain per configured upload interval.
func drainSpool(ctx context.Context, uploadEvery time.Duration, c *client, spool *sensorspool.Spool, logger *zap.SugaredLogger) (time.Duration, error) {
	budget := uploadEvery / 2
	if budget < 2*time.Second {
		budget = 2 * time.Second
	}
	if budget > 30*time.Second {
		budget = 30 * time.Second
	}
	turnDeadline := time.Now().Add(budget)

	for batches := 0; batches < maxDrainBatches; batches++ {
		if err := ctx.Err(); err != nil {
			return 0, err
		}
		// The turn deadline is checked only between completed requests. A slow
		// but healthy branch WAN may take longer than half an upload interval;
		// the HTTP client's independent 30-second timeout remains authoritative.
		if batches > 0 && time.Now().After(turnDeadline) {
			return 0, nil
		}
		batch, err := spool.NextBatch(uploadBatchSize)
		if err != nil {
			return 0, err
		}
		if batch == nil {
			return 0, nil
		}
		if err := c.postBatch(ctx, batch.Path, batch.Key, batch.Items); err != nil {
			var responseErr *responseError
			if errors.As(err, &responseErr) {
				if isPermanentResultRejection(responseErr) {
					if len(batch.Items) > 1 {
						if splitErr := spool.SplitInflight(batch); splitErr != nil {
							return 0, splitErr
						}
						logger.Warnf("controller rejected result batch with %d items (%d); splitting to isolate invalid result", len(batch.Items), responseErr.statusCode)
						continue
					}
					if dropErr := spool.DropInflight(batch); dropErr != nil {
						return 0, dropErr
					}
					logger.Errorf("dropped permanently rejected sensor result: path=%s status=%d response=%s", batch.Path, responseErr.statusCode, responseErr.body)
					continue
				}
				return responseErr.retryAfter, err
			}
			return 0, err
		}
		if err := spool.Ack(batch); err != nil {
			return 0, err
		}
	}
	return 0, nil
}

func isPermanentResultRejection(err *responseError) bool {
	if err == nil {
		return false
	}
	switch err.statusCode {
	case http.StatusBadRequest, http.StatusRequestEntityTooLarge, http.StatusUnprocessableEntity:
		return true
	case http.StatusForbidden:
		body := strings.ToLower(err.body)
		return strings.Contains(body, "not assigned") || strings.Contains(body, "assignment_mismatch")
	default:
		return false
	}
}

func runPeriodic(ctx context.Context, every time.Duration, fn func(context.Context)) {
	runTriggeredPeriodic(ctx, every, nil, fn)
}

func runTriggeredPeriodic(ctx context.Context, every time.Duration, trigger <-chan struct{}, fn func(context.Context)) {
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			fn(ctx)
			timer.Reset(every)
		case <-trigger:
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			fn(ctx)
			timer.Reset(every)
		}
	}
}

func uploadRetryDelay(base time.Duration, failures int, retryAfter time.Duration) time.Duration {
	if base < time.Second {
		base = time.Second
	}
	delay := base
	for i := 1; i < failures && delay < 5*time.Minute; i++ {
		delay *= 2
		if delay > 5*time.Minute {
			delay = 5 * time.Minute
		}
	}
	if retryAfter > delay {
		delay = retryAfter
	}
	if delay > 15*time.Minute {
		return 15 * time.Minute
	}
	return delay
}

func uint64ToInt(value uint64) int {
	maxInt := uint64(^uint(0) >> 1)
	if value > maxInt {
		return int(maxInt)
	}
	return int(value)
}

type commandProcessor struct {
	logger       *zap.SugaredLogger
	logLevel     zap.AtomicLevel
	client       *client
	state        *persistedState
	enqueue      func(string, any) error
	reloadConfig chan<- struct{}
	flushBuffer  chan<- struct{}
	stop         context.CancelFunc
	applyUpdate  func(context.Context, heartbeatCommand) (string, error)
}

func newCommandProcessor(
	logger *zap.SugaredLogger,
	logLevel zap.AtomicLevel,
	client *client,
	state *persistedState,
	enqueue func(string, any) error,
	reloadConfig chan<- struct{},
	flushBuffer chan<- struct{},
	stop context.CancelFunc,
) *commandProcessor {
	processor := &commandProcessor{
		logger: logger, logLevel: logLevel, client: client, state: state, enqueue: enqueue,
		reloadConfig: reloadConfig, flushBuffer: flushBuffer, stop: stop,
	}
	processor.applyUpdate = processor.installUpdate
	return processor
}

func (p *commandProcessor) Process(ctx context.Context, commands []heartbeatCommand) {
	for _, command := range commands {
		if ctx.Err() != nil {
			return
		}
		command.ID = strings.TrimSpace(command.ID)
		command.Verb = strings.ToLower(strings.TrimSpace(command.Verb))
		if command.ID == "" {
			p.logger.Error("ignored sensor command without an id")
			continue
		}
		if prior, ok := p.completed(command.ID); ok {
			if err := p.emitOutcome(prior); err != nil {
				p.logger.Warnf("re-emit sensor command outcome %s: %v", command.ID, err)
			}
			continue
		}

		outcome := completedCommand{
			ID: command.ID, Verb: command.Verb, EventType: "command_completed", HandledAt: time.Now().UTC(),
		}
		restart := false
		switch command.Verb {
		case "reload_config":
			notifyCommandChannel(p.reloadConfig)
			outcome.Message = "configuration refresh requested"
		case "flush_buffer":
			notifyCommandChannel(p.flushBuffer)
			outcome.Message = "result buffer flush requested"
		case "set_log_level":
			var payload struct {
				Level string `json:"level"`
			}
			if err := decodeCommandPayload(command, &payload); err != nil {
				outcome.EventType, outcome.Message = "command_failed", err.Error()
				break
			}
			var level zapcore.Level
			if err := level.UnmarshalText([]byte(strings.ToLower(strings.TrimSpace(payload.Level)))); err != nil || level < zapcore.DebugLevel || level > zapcore.ErrorLevel {
				outcome.EventType, outcome.Message = "command_failed", "log level must be debug, info, warn, or error"
				break
			}
			p.logLevel.SetLevel(level)
			outcome.Message = "log level set to " + level.String()
		case "update":
			installedVersion, err := p.applyUpdate(ctx, command)
			if err != nil {
				outcome.EventType, outcome.Message = "command_failed", err.Error()
				break
			}
			outcome.Version = installedVersion
			outcome.Message = "sensor binary updated; restarting"
			restart = true
		default:
			outcome.EventType = "command_failed"
			outcome.Message = "unsupported command verb"
		}

		if err := p.remember(outcome); err != nil {
			p.logger.Errorf("persist sensor command outcome %s: %v", command.ID, err)
			// A successful update has already atomically replaced the running
			// executable. Restart even if the outcome cache cannot be persisted;
			// the new binary treats a same-version manifest as an idempotent
			// success when the controller redelivers the command.
			if restart {
				p.stop()
				return
			}
			continue
		}
		if err := p.emitOutcome(outcome); err != nil {
			p.logger.Warnf("persist sensor command event %s: %v", command.ID, err)
		}
		if restart {
			p.stop()
			return
		}
	}
}

func decodeCommandPayload(command heartbeatCommand, destination any) error {
	if len(command.Payload) == 0 || string(command.Payload) == "null" {
		return errors.New("command payload is required")
	}
	if err := json.Unmarshal(command.Payload, destination); err != nil {
		return fmt.Errorf("invalid command payload: %w", err)
	}
	return nil
}

func notifyCommandChannel(channel chan<- struct{}) {
	select {
	case channel <- struct{}{}:
	default:
	}
}

func (p *commandProcessor) completed(id string) (completedCommand, bool) {
	state := p.state.Snapshot()
	for _, outcome := range state.CompletedCommands {
		if outcome.ID == id {
			return outcome, true
		}
	}
	return completedCommand{}, false
}

func (p *commandProcessor) remember(outcome completedCommand) error {
	return p.state.Update(func(next *state) {
		for i, existing := range next.CompletedCommands {
			if existing.ID == outcome.ID {
				next.CompletedCommands[i] = outcome
				return
			}
		}
		next.CompletedCommands = append(next.CompletedCommands, outcome)
		if len(next.CompletedCommands) > completedCommandLimit {
			next.CompletedCommands = append([]completedCommand(nil), next.CompletedCommands[len(next.CompletedCommands)-completedCommandLimit:]...)
		}
	})
}

func (p *commandProcessor) emitOutcome(outcome completedCommand) error {
	return p.enqueue(eventsPath, map[string]any{
		"type": outcome.EventType, "timestamp": time.Now().UTC(),
		"data": map[string]any{
			"command_id": outcome.ID, "verb": outcome.Verb, "message": outcome.Message,
			"version": outcome.Version, "handled_at": outcome.HandledAt,
		},
	})
}

type updateCommandPayload struct {
	ManifestURL string `json:"manifest_url"`
	Version     string `json:"version"`
}

type signedUpdateManifestEnvelope struct {
	SignedManifest string `json:"signed_manifest"`
	Signature      string `json:"signature"`
}

type sensorUpdateManifest struct {
	Version   string `json:"version"`
	OS        string `json:"os"`
	Arch      string `json:"arch"`
	Platform  string `json:"platform"`
	BinaryURL string `json:"binary_url"`
	URL       string `json:"url"`
	SHA256    string `json:"sha256"`
	Checksum  string `json:"checksum"`
}

func (p *commandProcessor) installUpdate(ctx context.Context, command heartbeatCommand) (string, error) {
	var payload updateCommandPayload
	if err := decodeCommandPayload(command, &payload); err != nil {
		return "", err
	}
	if strings.TrimSpace(payload.ManifestURL) == "" {
		return "", errors.New("update manifest_url is required")
	}
	manifestData, err := p.client.downloadControllerBytes(ctx, payload.ManifestURL, maxUpdateManifestBytes)
	if err != nil {
		return "", fmt.Errorf("download update manifest: %w", err)
	}
	publicKey, err := releasePublicKey()
	if err != nil {
		return "", err
	}
	manifest, err := verifySignedUpdateManifest(manifestData, publicKey)
	if err != nil {
		return "", err
	}
	canonicalizeUpdateManifest(&manifest)
	manifest.BinaryURL, err = resolveUpdateBinaryURL(payload.ManifestURL, manifest.BinaryURL)
	if err != nil {
		return "", err
	}
	if err := validateUpdateManifestMetadata(manifest, payload.Version, runtime.GOOS, runtime.GOARCH); err != nil {
		return "", err
	}
	if err := validateControllerDownloadURL(p.client.baseURL, manifest.BinaryURL); err != nil {
		return "", err
	}
	if compareNumericVersions(manifest.Version, sensorVersion()) == 0 {
		// This covers the narrow crash window where the executable was swapped
		// but the completed-command cache could not be persisted. The manifest
		// was still fetched over the authenticated controller connection and no
		// replacement occurs unless the version is newer.
		return manifest.Version, nil
	}
	if err := validateUpdateManifest(manifest, payload.Version, sensorVersion(), runtime.GOOS, runtime.GOARCH); err != nil {
		return "", err
	}
	response, err := p.client.openControllerDownload(ctx, manifest.BinaryURL)
	if err != nil {
		return "", fmt.Errorf("download update binary: %w", err)
	}
	defer response.Body.Close()
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve current executable: %w", err)
	}
	if resolved, resolveErr := filepath.EvalSymlinks(executable); resolveErr == nil {
		executable = resolved
	}
	if err := installUpdateBinary(ctx, executable, response.Body, manifest.SHA256, manifest.Version); err != nil {
		return "", err
	}
	return manifest.Version, nil
}

func releasePublicKey() (ed25519.PublicKey, error) {
	block, rest := pem.Decode([]byte(releasePublicKeyPEM))
	if block == nil || block.Type != "PUBLIC KEY" || len(bytes.TrimSpace(rest)) != 0 {
		return nil, errors.New("embedded release public key is invalid")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse embedded release public key: %w", err)
	}
	publicKey, ok := parsed.(ed25519.PublicKey)
	if !ok || len(publicKey) != ed25519.PublicKeySize {
		return nil, errors.New("embedded release public key is not Ed25519")
	}
	return publicKey, nil
}

func verifySignedUpdateManifest(envelopeData []byte, publicKey ed25519.PublicKey) (sensorUpdateManifest, error) {
	var envelope signedUpdateManifestEnvelope
	if err := json.Unmarshal(envelopeData, &envelope); err != nil {
		return sensorUpdateManifest{}, fmt.Errorf("decode signed update envelope: %w", err)
	}
	if strings.TrimSpace(envelope.SignedManifest) == "" || strings.TrimSpace(envelope.Signature) == "" {
		return sensorUpdateManifest{}, errors.New("update envelope requires signed_manifest and signature")
	}
	signedManifest, err := decodeBase64(envelope.SignedManifest)
	if err != nil {
		return sensorUpdateManifest{}, fmt.Errorf("decode signed update manifest: %w", err)
	}
	signature, err := decodeBase64(envelope.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize {
		return sensorUpdateManifest{}, errors.New("decode update manifest signature: invalid Ed25519 signature")
	}
	if len(publicKey) != ed25519.PublicKeySize || !ed25519.Verify(publicKey, signedManifest, signature) {
		return sensorUpdateManifest{}, errors.New("update manifest Ed25519 signature verification failed")
	}
	var manifest sensorUpdateManifest
	if err := json.Unmarshal(signedManifest, &manifest); err != nil {
		return sensorUpdateManifest{}, fmt.Errorf("decode verified update manifest: %w", err)
	}
	return manifest, nil
}

func decodeBase64(value string) ([]byte, error) {
	value = strings.TrimSpace(value)
	decoded, err := base64.StdEncoding.DecodeString(value)
	if err == nil {
		return decoded, nil
	}
	return base64.RawStdEncoding.DecodeString(value)
}

func canonicalizeUpdateManifest(manifest *sensorUpdateManifest) {
	if manifest.BinaryURL == "" {
		manifest.BinaryURL = manifest.URL
	}
	if manifest.SHA256 == "" {
		manifest.SHA256 = manifest.Checksum
	}
	if (manifest.OS == "" || manifest.Arch == "") && manifest.Platform != "" {
		parts := strings.SplitN(strings.ToLower(strings.TrimSpace(manifest.Platform)), "-", 2)
		if len(parts) == 2 {
			if manifest.OS == "" {
				manifest.OS = parts[0]
			}
			if manifest.Arch == "" {
				manifest.Arch = parts[1]
			}
		}
	}
}

func validateUpdateManifest(manifest sensorUpdateManifest, requestedVersion, currentVersion, goos, goarch string) error {
	if err := validateUpdateManifestMetadata(manifest, requestedVersion, goos, goarch); err != nil {
		return err
	}
	if compareNumericVersions(manifest.Version, currentVersion) <= 0 {
		return fmt.Errorf("update version %s is not newer than current %s", manifest.Version, currentVersion)
	}
	return nil
}

func validateUpdateManifestMetadata(manifest sensorUpdateManifest, requestedVersion, goos, goarch string) error {
	manifest.Version = strings.TrimSpace(manifest.Version)
	if manifest.Version == "" || strings.TrimSpace(manifest.BinaryURL) == "" || strings.TrimSpace(manifest.SHA256) == "" {
		return errors.New("update manifest requires version, binary_url, and sha256")
	}
	if len(numericVersionParts(manifest.Version)) == 0 {
		return errors.New("update manifest version must contain a numeric component")
	}
	if !strings.EqualFold(strings.TrimSpace(manifest.OS), goos) || !strings.EqualFold(strings.TrimSpace(manifest.Arch), goarch) {
		return fmt.Errorf("update platform %s/%s does not match runtime %s/%s", manifest.OS, manifest.Arch, goos, goarch)
	}
	if requestedVersion != "" && compareNumericVersions(manifest.Version, requestedVersion) != 0 {
		return fmt.Errorf("manifest version %s does not match requested version %s", manifest.Version, requestedVersion)
	}
	if _, err := normalizeSHA256(manifest.SHA256); err != nil {
		return fmt.Errorf("invalid update checksum: %w", err)
	}
	return nil
}

func compareNumericVersions(left, right string) int {
	leftParts := numericVersionParts(left)
	rightParts := numericVersionParts(right)
	length := max(len(leftParts), len(rightParts))
	for i := 0; i < length; i++ {
		leftPart, rightPart := 0, 0
		if i < len(leftParts) {
			leftPart = leftParts[i]
		}
		if i < len(rightParts) {
			rightPart = rightParts[i]
		}
		if leftPart < rightPart {
			return -1
		}
		if leftPart > rightPart {
			return 1
		}
	}
	return 0
}

func numericVersionParts(value string) []int {
	parts := make([]int, 0, 4)
	current := -1
	for _, char := range value {
		if char >= '0' && char <= '9' {
			if current < 0 {
				current = 0
			}
			current = current*10 + int(char-'0')
			continue
		}
		if current >= 0 {
			parts = append(parts, current)
			if len(parts) == 3 {
				return parts
			}
			current = -1
		}
	}
	if current >= 0 {
		parts = append(parts, current)
	}
	if len(parts) > 3 {
		parts = parts[:3]
	}
	return parts
}

func (c *client) downloadControllerBytes(ctx context.Context, rawURL string, limit int64) ([]byte, error) {
	response, err := c.openControllerDownload(ctx, rawURL)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("download exceeds %d bytes", limit)
	}
	return data, nil
}

func (c *client) openControllerDownload(ctx context.Context, rawURL string) (*http.Response, error) {
	if err := validateControllerDownloadURL(c.baseURL, rawURL); err != nil {
		return nil, err
	}
	downloader := *c.httpClient
	downloader.Timeout = 10 * time.Minute
	downloader.CheckRedirect = func(request *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return errors.New("too many update download redirects")
		}
		if err := validateControllerDownloadURL(c.baseURL, request.URL.String()); err != nil {
			return err
		}
		request.Header.Set("Accept", "application/octet-stream, application/json")
		request.Header.Set("X-Sensor-Id", c.state.SensorID)
		request.Header.Set("Authorization", "Bearer "+c.state.APIKey)
		return nil
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/octet-stream, application/json")
	request.Header.Set("X-Sensor-Id", c.state.SensorID)
	request.Header.Set("Authorization", "Bearer "+c.state.APIKey)
	response, err := downloader.Do(request)
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		defer response.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return nil, fmt.Errorf("GET %s failed: %s %s", request.URL.Redacted(), response.Status, strings.TrimSpace(string(body)))
	}
	return response, nil
}

func validateControllerDownloadURL(controllerURL, candidateURL string) error {
	controller, err := url.Parse(controllerURL)
	if err != nil || controller.Hostname() == "" || controller.User != nil {
		return errors.New("controller URL is invalid")
	}
	candidate, err := url.Parse(candidateURL)
	if err != nil || candidate.Hostname() == "" || candidate.User != nil {
		return errors.New("update download URL is invalid")
	}
	if !strings.EqualFold(candidate.Scheme, "https") {
		return errors.New("update downloads require HTTPS")
	}
	if !sameURLOrigin(controller, candidate) {
		return errors.New("update download URL must use the controller origin")
	}
	return nil
}

func resolveUpdateBinaryURL(manifestURL, binaryURL string) (string, error) {
	manifest, err := url.Parse(strings.TrimSpace(manifestURL))
	if err != nil || manifest.Hostname() == "" || !manifest.IsAbs() {
		return "", errors.New("update manifest URL is invalid")
	}
	binary, err := url.Parse(strings.TrimSpace(binaryURL))
	if err != nil || strings.TrimSpace(binaryURL) == "" {
		return "", errors.New("update binary URL is invalid")
	}
	resolved := manifest.ResolveReference(binary)
	if !resolved.IsAbs() || resolved.Hostname() == "" {
		return "", errors.New("update binary URL could not be resolved")
	}
	return resolved.String(), nil
}

func sameURLOrigin(left, right *url.URL) bool {
	if !strings.EqualFold(left.Scheme, right.Scheme) || !strings.EqualFold(left.Hostname(), right.Hostname()) {
		return false
	}
	port := func(parsed *url.URL) string {
		if parsed.Port() != "" {
			return parsed.Port()
		}
		if strings.EqualFold(parsed.Scheme, "https") {
			return "443"
		}
		if strings.EqualFold(parsed.Scheme, "http") {
			return "80"
		}
		return ""
	}
	return port(left) == port(right)
}

func installUpdateBinary(ctx context.Context, executable string, body io.Reader, checksum, targetVersion string) (returnErr error) {
	info, err := os.Stat(executable)
	if err != nil {
		return fmt.Errorf("stat current executable: %w", err)
	}
	if !info.Mode().IsRegular() {
		return errors.New("current executable is not a regular file")
	}
	tmp, err := os.CreateTemp(filepath.Dir(executable), ".zenplus-sensor-update-*.tmp")
	if err != nil {
		return fmt.Errorf("create update file: %w", err)
	}
	tmpPath := tmp.Name()
	defer func() {
		tmp.Close()
		os.Remove(tmpPath)
	}()
	if err := preserveFileOwnership(executable, tmpPath); err != nil {
		return fmt.Errorf("preserve update ownership: %w", err)
	}
	if err := tmp.Chmod(info.Mode().Perm()); err != nil {
		return fmt.Errorf("preserve update mode: %w", err)
	}
	hasher := sha256.New()
	written, err := io.Copy(io.MultiWriter(tmp, hasher), io.LimitReader(body, maxUpdateBinaryBytes+1))
	if err != nil {
		return fmt.Errorf("write update binary: %w", err)
	}
	if written > maxUpdateBinaryBytes {
		return fmt.Errorf("update binary exceeds %d bytes", maxUpdateBinaryBytes)
	}
	expected, err := normalizeSHA256(checksum)
	if err != nil {
		return fmt.Errorf("invalid update checksum: %w", err)
	}
	actual := hasher.Sum(nil)
	expectedBytes, _ := hex.DecodeString(expected)
	if subtle.ConstantTimeCompare(expectedBytes, actual) != 1 {
		return errors.New("update binary SHA-256 mismatch")
	}
	if err := tmp.Sync(); err != nil {
		return fmt.Errorf("sync update binary: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close update binary: %w", err)
	}
	if err := validateUpdateExecutable(ctx, tmpPath, targetVersion); err != nil {
		return err
	}

	backupPath := executable + ".previous"
	if err := os.Remove(backupPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove previous update backup: %w", err)
	}
	if err := os.Link(executable, backupPath); err != nil {
		return fmt.Errorf("create rollback link: %w", err)
	}
	if err := os.Rename(tmpPath, executable); err != nil {
		return fmt.Errorf("atomically install update: %w", err)
	}
	if err := syncStateDirectory(filepath.Dir(executable)); err != nil {
		if rollbackErr := os.Rename(backupPath, executable); rollbackErr != nil {
			return errors.Join(fmt.Errorf("sync installed update: %w", err), fmt.Errorf("rollback update: %w", rollbackErr))
		}
		_ = syncStateDirectory(filepath.Dir(executable))
		return fmt.Errorf("sync installed update: %w", err)
	}
	return nil
}

func validateUpdateExecutable(ctx context.Context, executable, targetVersion string) error {
	testContext, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	output, err := exec.CommandContext(testContext, executable, "--version").CombinedOutput()
	if err != nil {
		return fmt.Errorf("validate updated executable: %w", err)
	}
	if compareNumericVersions(strings.TrimSpace(string(output)), targetVersion) != 0 {
		return fmt.Errorf("updated executable reports version %q, expected %q", strings.TrimSpace(string(output)), targetVersion)
	}
	return nil
}

type probeRunner interface {
	CheckOne(context.Context, *checker.ServiceCheck, string) *checker.ServiceCheckResult
}

type targetSchedule struct {
	lastStarted time.Time
	notBefore   time.Time
	initialized bool
	running     bool
}

type checkScheduler struct {
	runner     probeRunner
	sensorName string
	enqueue    func(string, any) error
	logger     *zap.SugaredLogger
	sem        chan struct{}
	jitter     func(string, time.Duration) time.Duration

	mu      sync.Mutex
	targets map[string]*targetSchedule
	cursor  int
	workers sync.WaitGroup
}

type scheduledProbe struct {
	key      string
	interval time.Duration
	run      func()
}

func newCheckScheduler(runner probeRunner, sensorName string, maxWorkers int, enqueue func(string, any) error, logger *zap.SugaredLogger) *checkScheduler {
	if maxWorkers <= 0 {
		maxWorkers = 1
	}
	return &checkScheduler{
		runner: runner, sensorName: sensorName, enqueue: enqueue, logger: logger,
		sem: make(chan struct{}, maxWorkers), targets: make(map[string]*targetSchedule),
		jitter: func(key string, interval time.Duration) time.Duration {
			return stableProbeJitter(sensorName, key, interval)
		},
	}
}

// stableProbeJitter spreads a target's first run across ten percent of its
// interval (up to 30 seconds). Sensor identity and target key make the offset
// deterministic across config refreshes and restarts while differing across
// appliances that start together.
func stableProbeJitter(sensorName, key string, interval time.Duration) time.Duration {
	window := interval / 10
	if window > 30*time.Second {
		window = 30 * time.Second
	}
	if window <= 0 {
		return 0
	}
	hash := fnv.New64a()
	_, _ = hash.Write([]byte(sensorName))
	_, _ = hash.Write([]byte{0})
	_, _ = hash.Write([]byte(key))
	return time.Duration(hash.Sum64() % uint64(window))
}

// Schedule starts every due check for which worker capacity is immediately
// available. A target is marked at start (not completion), and cannot overlap
// itself even when its execution time exceeds its configured interval.
func (s *checkScheduler) Schedule(ctx context.Context, current configResponse, now time.Time) int {
	if ctx.Err() != nil {
		return 0
	}
	probes := make([]scheduledProbe, 0, len(current.Devices)+len(current.ServiceChecks))
	for _, configured := range current.Devices {
		device := configured
		if !device.PingEnabled || device.ID == "" {
			continue
		}
		interval := secondsOrDefault(device.PingInterval, time.Minute)
		probes = append(probes, scheduledProbe{
			key: "device:" + device.ID, interval: interval,
			run: func() { s.runDevice(ctx, device, interval) },
		})
	}
	for _, configured := range current.ServiceChecks {
		service := configured
		if !service.Enabled || service.ID == "" {
			continue
		}
		id, err := uuid.Parse(service.ID)
		if err != nil {
			s.logger.Warnf("skipping service check with invalid id %q", service.ID)
			continue
		}
		interval := secondsOrDefault(service.CheckInterval, time.Minute)
		probes = append(probes, scheduledProbe{
			key: "service:" + service.ID, interval: interval,
			run: func() { s.runService(ctx, service, id, interval) },
		})
	}
	if len(probes) == 0 {
		return 0
	}

	// Resume scanning where the previous pass actually stopped making
	// progress. Advancing by a fixed pool width can cycle over only a subset of
	// targets when the pool and target counts share a divisor.
	s.mu.Lock()
	start := s.cursor % len(probes)
	s.mu.Unlock()

	started := 0
	lastStarted := -1
	for offset := 0; offset < len(probes); offset++ {
		index := (start + offset) % len(probes)
		probe := probes[index]
		if s.tryStart(ctx, probe.key, probe.interval, now, probe.run) {
			started++
			lastStarted = index
		}
	}
	s.mu.Lock()
	if lastStarted >= 0 {
		s.cursor = (lastStarted + 1) % len(probes)
	} else {
		s.cursor = (start + 1) % len(probes)
	}
	s.mu.Unlock()
	return started
}

func (s *checkScheduler) tryStart(ctx context.Context, key string, interval time.Duration, now time.Time, task func()) bool {
	s.mu.Lock()
	state := s.targets[key]
	if state == nil {
		state = &targetSchedule{}
		s.targets[key] = state
	}
	if !state.initialized {
		state.initialized = true
		state.notBefore = now.Add(s.jitter(key, interval))
	}
	if now.Before(state.notBefore) {
		s.mu.Unlock()
		return false
	}
	if state.running || (!state.lastStarted.IsZero() && now.Sub(state.lastStarted) < interval) {
		s.mu.Unlock()
		return false
	}
	select {
	case s.sem <- struct{}{}:
		state.running = true
		state.lastStarted = now
		s.workers.Add(1)
	default:
		s.mu.Unlock()
		return false
	}
	s.mu.Unlock()

	go func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				s.logger.Errorf("probe worker panic for %s: %v", key, recovered)
			}
			s.mu.Lock()
			s.targets[key].running = false
			s.mu.Unlock()
			<-s.sem
			s.workers.Done()
		}()
		task()
	}()
	return true
}

func (s *checkScheduler) runDevice(ctx context.Context, d configDevice, interval time.Duration) {
	check := &checker.ServiceCheck{
		ID: uuid.New(), Name: d.Hostname, CheckType: "icmp", Enabled: true,
		TargetHost: d.IPAddress, Timeout: 3 * time.Second, CheckInterval: interval,
		Config: map[string]any{"count": 3},
	}
	result := s.runWithRetries(ctx, check, 2) // initial ICMP probe plus two confirmations
	if result == nil {
		s.logger.Errorf("device probe %s returned no result", d.ID)
		return
	}
	if result.Timestamp.IsZero() {
		result.Timestamp = time.Now().UTC()
	}
	payload := map[string]any{
		"device_id": d.ID, "timestamp": result.Timestamp, "is_up": result.IsUp,
		"rtt_ms": float64(result.ResponseTime.Microseconds()) / 1000.0, "ip_address": d.IPAddress,
	}
	if result.PacketsSent > 0 {
		payload["packet_loss"] = result.PacketLoss
		payload["jitter_ms"] = float64(result.Jitter.Microseconds()) / 1000.0
		payload["min_rtt_ms"] = float64(result.MinRTT.Microseconds()) / 1000.0
		payload["max_rtt_ms"] = float64(result.MaxRTT.Microseconds()) / 1000.0
		payload["packets_sent"] = result.PacketsSent
		payload["packets_received"] = result.PacketsReceived
	}
	if err := s.enqueue(pingResultsPath, payload); err != nil {
		s.logger.Errorf("persist ping result for %s: %v", d.ID, err)
	}
}

func (s *checkScheduler) runService(ctx context.Context, sc configServiceCheck, id uuid.UUID, interval time.Duration) {
	followRedirects := true
	if sc.HTTPFollowRedirects != nil {
		followRedirects = *sc.HTTPFollowRedirects
	}
	timeout := secondsOrDefault(sc.Timeout, 10*time.Second)
	if timeout > maxServiceCheckTimeout {
		timeout = maxServiceCheckTimeout
	}
	expectedStatus := sc.HTTPExpectedStatus
	if expectedStatus == 0 {
		expectedStatus = http.StatusOK
	}
	retryDelay := secondsOrDefault(sc.RetryDelayS, defaultServiceRetryDelay)
	if retryDelay > maxServiceRetryDelay {
		retryDelay = maxServiceRetryDelay
	}
	check := &checker.ServiceCheck{
		ID: id, Name: sc.Name, CheckType: sc.CheckType, Enabled: sc.Enabled,
		TargetHost: sc.TargetHost, TargetPort: sc.TargetPort, TargetURL: sc.TargetURL,
		HTTPMethod: defaultString(sc.HTTPMethod, "GET"), HTTPHeaders: cloneStringMap(sc.HTTPHeaders), HTTPBody: sc.HTTPBody,
		HTTPExpectedStatus:   expectedStatus,
		HTTPExpectedStatuses: sc.HTTPExpectedStatuses, HTTPContentMatch: sc.HTTPContentMatch,
		HTTPFollowRedirects: followRedirects, HTTPIgnoreTLSErrors: sc.HTTPIgnoreTLSErrors,
		HTTPAllowInsecureAuth: sc.HTTPAllowInsecureAuth, Config: cloneJSONMap(sc.Config),
		TLSWarnDays: sc.TLSWarnDays, TLSCriticalDays: sc.TLSCriticalDays,
		CheckInterval: interval, Timeout: timeout, RetryCount: sc.RetryCount, RetryDelay: retryDelay,
	}
	result := s.runServiceWithRetries(ctx, check)
	if result == nil {
		s.logger.Errorf("service probe %s returned no result", sc.ID)
		return
	}
	if result.Timestamp.IsZero() {
		result.Timestamp = time.Now().UTC()
	}
	payload := map[string]any{
		"service_check_id": sc.ID, "timestamp": result.Timestamp, "check_type": result.CheckType,
		"is_up": result.IsUp, "response_ms": float64(result.ResponseTime.Microseconds()) / 1000.0,
		"error":              result.Error,
		"tls_days_remaining": result.TLSDaysRemaining, "tls_valid": result.TLSValid,
		"tls_expiry_date": result.TLSExpiry, "tls_issuer": result.TLSIssuer, "tls_subject": result.TLSSubject,
		"content_matched": result.ContentMatched,
	}
	if result.StatusCode >= 100 && result.StatusCode <= 599 {
		payload["status_code"] = result.StatusCode
	}
	if err := s.enqueue(serviceResultsPath, payload); err != nil {
		s.logger.Errorf("persist service result for %s: %v", sc.ID, err)
	}
}

func (s *checkScheduler) runServiceWithRetries(ctx context.Context, check *checker.ServiceCheck) *checker.ServiceCheckResult {
	attempts := check.RetryCount
	if attempts < 1 {
		attempts = 1
	}
	if attempts > 10 {
		attempts = 10
	}
	return s.runWithRetries(ctx, check, attempts-1)
}

func (s *checkScheduler) runWithRetries(ctx context.Context, check *checker.ServiceCheck, retries int) *checker.ServiceCheckResult {
	var last *checker.ServiceCheckResult
	for attempt := 0; attempt <= retries; attempt++ {
		last = s.runner.CheckOne(ctx, check, s.sensorName)
		if last != nil && last.IsUp {
			return last
		}
		if attempt == retries || ctx.Err() != nil {
			break
		}
		delay := check.RetryDelay
		if delay <= 0 {
			delay = 250 * time.Millisecond * time.Duration(1<<min(attempt, 3))
		} else if delay > maxServiceRetryDelay {
			delay = maxServiceRetryDelay
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				<-timer.C
			}
			return last
		case <-timer.C:
		}
	}
	return last
}

func (s *checkScheduler) Wait(timeout time.Duration) bool {
	done := make(chan struct{})
	go func() {
		s.workers.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-time.After(timeout):
		return false
	}
}

func newResultEnqueuer(spool *sensorspool.Spool) func(string, any) error {
	return func(path string, payload any) error {
		data, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		retained, err := spool.Enqueue(sensorspool.Item{Path: path, Payload: data})
		if err != nil {
			return err
		}
		if !retained {
			return errors.New("result exceeded spool limits and was evicted")
		}
		return nil
	}
}

func secondsOrDefault(seconds int, fallback time.Duration) time.Duration {
	if seconds <= 0 {
		return fallback
	}
	return time.Duration(seconds) * time.Second
}

func defaultString(v, fallback string) string {
	if v == "" {
		return fallback
	}
	return v
}
