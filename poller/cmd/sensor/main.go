package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"go.uber.org/zap"
)

const version = "sensor-0.1.0"

type state struct {
	SensorID string `json:"sensor_id"`
	APIKey   string `json:"api_key"`
	ETag     string `json:"etag"`
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
	ID                   string `json:"id"`
	Name                 string `json:"name"`
	CheckType            string `json:"check_type"`
	TargetHost           string `json:"target_host"`
	TargetPort           int    `json:"target_port"`
	TargetURL            string `json:"target_url"`
	HTTPMethod           string `json:"http_method"`
	HTTPExpectedStatuses string `json:"http_expected_statuses"`
	HTTPContentMatch     string `json:"http_content_match"`
	HTTPFollowRedirects  *bool  `json:"http_follow_redirects"`
	TLSWarnDays          int    `json:"tls_warn_days"`
	TLSCriticalDays      int    `json:"tls_critical_days"`
	CheckInterval        int    `json:"check_interval"`
	Timeout              int    `json:"timeout"`
	RetryCount           int    `json:"retry_count"`
	Enabled              bool   `json:"enabled"`
}

type client struct {
	baseURL    string
	httpClient *http.Client
	state      *state
}

func main() {
	logger := zap.Must(zap.NewProduction()).Sugar()
	defer logger.Sync()

	cfg, err := loadConfig()
	if err != nil {
		logger.Fatal(err)
	}

	st, err := loadState(cfg.statePath)
	if err != nil {
		logger.Fatal(err)
	}

	httpClient := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: !cfg.verifyTLS}, // controlled by appliance env
		},
	}
	c := &client{baseURL: strings.TrimRight(cfg.serverURL, "/"), httpClient: httpClient, state: st}

	if st.SensorID == "" || st.APIKey == "" {
		if cfg.enrollmentToken == "" {
			logger.Fatal("missing enrollment token and no saved sensor credentials")
		}
		if err := c.enroll(cfg); err != nil {
			logger.Fatalf("enroll failed: %v", err)
		}
		if err := saveState(cfg.statePath, st); err != nil {
			logger.Fatalf("save state failed: %v", err)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger.Infof("zenplus sensor started: name=%s server=%s id=%s", cfg.sensorName, cfg.serverURL, st.SensorID)
	run(ctx, logger, cfg, c)
}

type runtimeConfig struct {
	serverURL       string
	enrollmentToken string
	sensorName      string
	verifyTLS       bool
	stateDir        string
	statePath       string
	heartbeatEvery  time.Duration
	configEvery     time.Duration
	uploadEvery     time.Duration
	maxWorkers      int
	hostname        string
	operatingSystem string
	startedAt       time.Time
}

func loadConfig() (*runtimeConfig, error) {
	serverURL := env("ZENPLUS_SERVER_URL", "")
	if serverURL == "" {
		return nil, errors.New("ZENPLUS_SERVER_URL is required")
	}
	name := env("ZENPLUS_SENSOR_NAME", "")
	if name == "" {
		host, _ := os.Hostname()
		name = host
	}
	stateDir := env("ZENPLUS_SENSOR_STATE_DIR", "/var/lib/zenplus-sensor")
	host, _ := os.Hostname()
	return &runtimeConfig{
		serverURL:       serverURL,
		enrollmentToken: env("ZENPLUS_ENROLLMENT_TOKEN", ""),
		sensorName:      name,
		verifyTLS:       env("ZENPLUS_VERIFY_TLS", "1") != "0",
		stateDir:        stateDir,
		statePath:       filepath.Join(stateDir, "state.json"),
		heartbeatEvery:  secondsEnv("ZENPLUS_HEARTBEAT_INTERVAL_SECONDS", 30),
		configEvery:     secondsEnv("ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS", 60),
		uploadEvery:     secondsEnv("ZENPLUS_UPLOAD_INTERVAL_SECONDS", 10),
		maxWorkers:      intEnv("ZENPLUS_MAX_WORKERS", 100),
		hostname:        host,
		operatingSystem: fmt.Sprintf("%s/%s", runtime.GOOS, runtime.GOARCH),
		startedAt:       time.Now(),
	}, nil
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
	if err := os.MkdirAll(filepath.Dir(path), 0750); err != nil {
		return err
	}
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0600)
}

func (c *client) enroll(cfg *runtimeConfig) error {
	body := map[string]any{
		"enrollment_token": cfg.enrollmentToken,
		"hostname":         cfg.hostname,
		"os_info":          cfg.operatingSystem,
		"version":          version,
	}
	var out struct {
		SensorID string `json:"sensor_id"`
		APIKey   string `json:"api_key"`
	}
	if err := c.doJSON(context.Background(), http.MethodPost, "/api/v1/sensor/enroll", body, &out, ""); err != nil {
		return err
	}
	c.state.SensorID = out.SensorID
	c.state.APIKey = out.APIKey
	return nil
}

func (c *client) heartbeat(ctx context.Context, cfg *runtimeConfig, queueDepth, dropped int) error {
	body := map[string]any{
		"version":             version,
		"uptime_seconds":      int(time.Since(cfg.startedAt).Seconds()),
		"queue_depth":         queueDepth,
		"queue_dropped_count": dropped,
		"hostname":            cfg.hostname,
		"os_info":             cfg.operatingSystem,
	}
	return c.doJSON(ctx, http.MethodPost, "/api/v1/sensor/heartbeat", body, nil, "")
}

func (c *client) getConfig(ctx context.Context) (*configResponse, bool, error) {
	var out configResponse
	err := c.doJSON(ctx, http.MethodGet, "/api/v1/sensor/config", nil, &out, c.state.ETag)
	if err != nil {
		if strings.Contains(err.Error(), "304") {
			return nil, false, nil
		}
		return nil, false, err
	}
	c.state.ETag = out.ETag
	return &out, true, nil
}

func (c *client) postBatch(ctx context.Context, path string, items []map[string]any) error {
	if len(items) == 0 {
		return nil
	}
	return c.doJSON(ctx, http.MethodPost, path, map[string]any{
		"idempotency_key": uuid.NewString(),
		"items":           items,
	}, nil, "")
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
		return fmt.Errorf("304 not modified")
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("%s %s failed: %s %s", method, path, resp.Status, strings.TrimSpace(string(data)))
	}
	if out == nil {
		io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func run(ctx context.Context, logger *zap.SugaredLogger, cfg *runtimeConfig, c *client) {
	checkerEngine := checker.NewChecker(logger)
	var current configResponse
	lastDeviceAt := map[string]time.Time{}
	lastServiceAt := map[string]time.Time{}
	lastHeartbeat := time.Time{}
	lastConfig := time.Time{}

	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			logger.Info("sensor stopping")
			return
		case now := <-ticker.C:
			if lastHeartbeat.IsZero() || now.Sub(lastHeartbeat) >= cfg.heartbeatEvery {
				if err := c.heartbeat(ctx, cfg, 0, 0); err != nil {
					logger.Warnf("heartbeat failed: %v", err)
				}
				lastHeartbeat = now
			}
			if lastConfig.IsZero() || now.Sub(lastConfig) >= cfg.configEvery {
				next, changed, err := c.getConfig(ctx)
				if err != nil {
					logger.Warnf("config pull failed: %v", err)
				} else if changed && next != nil {
					current = *next
					logger.Infof("config loaded: devices=%d service_checks=%d etag=%s", len(current.Devices), len(current.ServiceChecks), current.ETag)
				}
				lastConfig = now
			}

			pingItems := runDeviceChecks(ctx, checkerEngine, cfg, current.Devices, lastDeviceAt, now)
			if err := c.postBatch(ctx, "/api/v1/sensor/results/ping", pingItems); err != nil {
				logger.Warnf("ping upload failed: %v", err)
			}
			serviceItems := runServiceChecks(ctx, checkerEngine, cfg, current.ServiceChecks, lastServiceAt, now)
			if err := c.postBatch(ctx, "/api/v1/sensor/results/service", serviceItems); err != nil {
				logger.Warnf("service upload failed: %v", err)
			}
		}
	}
}

func runDeviceChecks(ctx context.Context, c *checker.Checker, cfg *runtimeConfig, devices []configDevice, last map[string]time.Time, now time.Time) []map[string]any {
	items := []map[string]any{}
	for _, d := range devices {
		if !d.PingEnabled {
			continue
		}
		interval := time.Duration(d.PingInterval) * time.Second
		if interval <= 0 {
			interval = time.Minute
		}
		if prev := last[d.ID]; !prev.IsZero() && now.Sub(prev) < interval {
			continue
		}
		last[d.ID] = now

		result := c.CheckOne(ctx, &checker.ServiceCheck{
			ID:            uuid.New(),
			Name:          d.Hostname,
			CheckType:     "icmp",
			Enabled:       true,
			TargetHost:    d.IPAddress,
			Timeout:       3 * time.Second,
			CheckInterval: interval,
		}, cfg.sensorName)
		items = append(items, map[string]any{
			"device_id":  d.ID,
			"timestamp":  result.Timestamp,
			"is_up":      result.IsUp,
			"rtt_ms":     float64(result.ResponseTime.Microseconds()) / 1000.0,
			"ip_address": d.IPAddress,
		})
	}
	return items
}

func runServiceChecks(ctx context.Context, c *checker.Checker, cfg *runtimeConfig, checks []configServiceCheck, last map[string]time.Time, now time.Time) []map[string]any {
	items := []map[string]any{}
	for _, sc := range checks {
		if !sc.Enabled {
			continue
		}
		interval := time.Duration(sc.CheckInterval) * time.Second
		if interval <= 0 {
			interval = time.Minute
		}
		if prev := last[sc.ID]; !prev.IsZero() && now.Sub(prev) < interval {
			continue
		}
		last[sc.ID] = now

		id, err := uuid.Parse(sc.ID)
		if err != nil {
			continue
		}
		followRedirects := true
		if sc.HTTPFollowRedirects != nil {
			followRedirects = *sc.HTTPFollowRedirects
		}
		timeout := time.Duration(sc.Timeout) * time.Second
		if timeout <= 0 {
			timeout = 10 * time.Second
		}
		result := c.CheckOne(ctx, &checker.ServiceCheck{
			ID:                   id,
			Name:                 sc.Name,
			CheckType:            sc.CheckType,
			Enabled:              sc.Enabled,
			TargetHost:           sc.TargetHost,
			TargetPort:           sc.TargetPort,
			TargetURL:            sc.TargetURL,
			HTTPMethod:           defaultString(sc.HTTPMethod, "GET"),
			HTTPExpectedStatus:   200,
			HTTPExpectedStatuses: sc.HTTPExpectedStatuses,
			HTTPContentMatch:     sc.HTTPContentMatch,
			HTTPFollowRedirects:  followRedirects,
			TLSWarnDays:          sc.TLSWarnDays,
			TLSCriticalDays:      sc.TLSCriticalDays,
			CheckInterval:        interval,
			Timeout:              timeout,
			RetryCount:           sc.RetryCount,
		}, cfg.sensorName)
		items = append(items, map[string]any{
			"service_check_id": sc.ID,
			"timestamp":        result.Timestamp,
			"check_type":       result.CheckType,
			"is_up":            result.IsUp,
			"response_ms":      float64(result.ResponseTime.Microseconds()) / 1000.0,
			"status_code":      result.StatusCode,
			"error":            result.Error,
		})
	}
	return items
}

func defaultString(v, fallback string) string {
	if v == "" {
		return fallback
	}
	return v
}
