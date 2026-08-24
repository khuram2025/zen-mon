package configpoller

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"

	"gopkg.in/yaml.v3"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

type Poller struct {
	client *client.Client
	etag   string
	cache  string
}

func New(client *client.Client, cachePath string) *Poller {
	return &Poller{client: client, cache: cachePath}
}

type envelope struct {
	Config    config.Config `json:"config" yaml:"config"`
	Signature string        `json:"signature" yaml:"signature"`
}

func (p *Poller) Poll(ctx context.Context, current config.Config) (config.Config, bool, string, error) {
	return p.poll(ctx, current, false)
}

func (p *Poller) PollForce(ctx context.Context, current config.Config) (config.Config, bool, string, error) {
	return p.poll(ctx, current, true)
}

func (p *Poller) poll(ctx context.Context, current config.Config, force bool) (config.Config, bool, string, error) {
	if p.etag == "" {
		p.etag = current.ConfigETag
	}
	if force {
		p.etag = ""
	}
	resp, body, err := p.client.GetJSON(ctx, "/api/v1/agents/config", p.etag, nil)
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusNotModified {
			return current, false, currentConfigHash(current), nil
		}
		return current, false, currentConfigHash(current), err
	}
	if resp != nil {
		if headerETag := resp.Header.Get("ETag"); headerETag != "" {
			p.etag = headerETag
		}
	}
	next, err := decodeConfig(body, current)
	if err != nil {
		return current, false, currentConfigHash(current), err
	}
	if current.Security.RequireSignedConfig {
		if err := verifySignature(body, current.Security.ConfigPublicKey); err != nil {
			return current, false, currentConfigHash(current), err
		}
	}
	if next.ConfigETag == "" {
		next.ConfigETag = p.etag
	}
	if next.ConfigETag != "" {
		p.etag = next.ConfigETag
	}
	if p.cache != "" {
		_ = os.WriteFile(p.cache, body, 0o600)
	}
	return next, true, currentConfigHash(next), nil
}

func decodeConfig(body []byte, fallback config.Config) (config.Config, error) {
	var contract model.AgentConfigResponse
	if err := json.Unmarshal(body, &contract); err == nil && (contract.ETag != "" || contract.ConfigVersion != 0 || contract.PolicyID != "") {
		return applyControllerConfig(fallback, contract), nil
	}
	var env envelope
	if err := yaml.Unmarshal(body, &env); err == nil && env.Config.ControllerURL != "" {
		return env.Config, nil
	}
	next := fallback
	if err := yaml.Unmarshal(body, &next); err != nil {
		return fallback, err
	}
	return next, next.Validate()
}

func applyControllerConfig(current config.Config, remote model.AgentConfigResponse) config.Config {
	next := current
	next.ConfigVersion = remote.ConfigVersion
	next.ConfigETag = remote.ETag
	if remote.PolicyID != "" {
		next.PolicyID = remote.PolicyID
	}
	if remote.MetricIntervalS > 0 {
		next.CollectIntervalSeconds = remote.MetricIntervalS
		// The controller contract exposes one host-metric cadence rather than
		// separate intervals for every collector. Keep the scheduler switches in
		// step with the top-level ticker; otherwise a 30-second policy still runs
		// collectors at their local 60/120-second defaults.
		next.Collectors.CPU.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.Memory.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.Filesystem.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.DiskIO.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.Network.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.Processes.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.Services.IntervalSeconds = remote.MetricIntervalS
		next.Collectors.EventLog.IntervalSeconds = remote.MetricIntervalS
	}
	if remote.UploadIntervalS > 0 {
		next.UploadIntervalSeconds = remote.UploadIntervalS
	}
	if remote.ProcessTopN > 0 {
		next.Collectors.Processes.TopN = remote.ProcessTopN
		next.Limits.MaxProcessCount = remote.ProcessTopN
	}
	// A non-nil empty slice is an explicit controller value and must clear a
	// previous/local list. A nil slice means the field was absent (for backward
	// compatibility with older controllers) and leaves the current value alone.
	if remote.ServiceWatchlist != nil {
		next.Collectors.Services.Watchlist = append([]string(nil), remote.ServiceWatchlist...)
	}
	if remote.ProcessWatchlist != nil {
		next.Collectors.Processes.Watchlist = append([]string(nil), remote.ProcessWatchlist...)
	}
	if remote.EventLogFilters != nil {
		filters := make([]config.EventLogFilter, len(remote.EventLogFilters))
		for i, filter := range remote.EventLogFilters {
			filters[i] = config.EventLogFilter{
				Channel: filter.Channel,
				Levels:  cloneStrings(filter.Levels),
				IDs:     cloneInts(filter.IDs),
			}
		}
		next.Collectors.EventLog.Filters = &filters
		// The filter list is authoritative when supplied by the controller. Clear
		// the legacy projection so the persisted config cannot suggest a broader
		// channel/level policy than the collector actually applies.
		next.Collectors.EventLog.Channels = nil
		next.Collectors.EventLog.Levels = nil
	}
	if remote.DiskIgnore != nil {
		next.DiskIgnore = append([]string(nil), remote.DiskIgnore...)
	}
	if remote.NetworkIgnore != nil {
		next.NetworkIgnore = append([]string(nil), remote.NetworkIgnore...)
	}
	if remote.UpdateRing != "" {
		next.UpdateRing = remote.UpdateRing
	}
	if maxProcesses := remote.CardinalityLimits["max_processes"]; maxProcesses > 0 {
		next.Limits.MaxProcessCount = maxProcesses
		if next.Collectors.Processes.TopN > maxProcesses {
			next.Collectors.Processes.TopN = maxProcesses
		}
	}
	applyFeatureFlag(remote.FeatureFlags, "cpu", &next.Collectors.CPU.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_cpu", &next.Collectors.CPU.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "memory", &next.Collectors.Memory.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_memory", &next.Collectors.Memory.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "filesystem", &next.Collectors.Filesystem.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_filesystem", &next.Collectors.Filesystem.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "disk_io", &next.Collectors.DiskIO.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_disk_io", &next.Collectors.DiskIO.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "network", &next.Collectors.Network.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_network", &next.Collectors.Network.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "process", &next.Collectors.Processes.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_process", &next.Collectors.Processes.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "service_state", &next.Collectors.Services.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_service_state", &next.Collectors.Services.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "event_log", &next.Collectors.EventLog.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_event_log", &next.Collectors.EventLog.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "inventory", &next.Collectors.Inventory.Enabled)
	applyFeatureFlag(remote.FeatureFlags, "collect_inventory", &next.Collectors.Inventory.Enabled)
	if err := next.Validate(); err != nil {
		return current
	}
	return next
}

func applyFeatureFlag(flags map[string]bool, key string, target *bool) {
	if flags == nil {
		return
	}
	if value, ok := flags[key]; ok {
		*target = value
	}
}

func cloneStrings(values []string) []string {
	if values == nil {
		return nil
	}
	return append([]string{}, values...)
}

func cloneInts(values []int) []int {
	if values == nil {
		return nil
	}
	return append([]int{}, values...)
}

func verifySignature(body []byte, publicKey string) error {
	if publicKey == "" {
		return fmt.Errorf("signed config required but no public key is configured")
	}
	var env envelope
	if err := yaml.Unmarshal(body, &env); err != nil {
		return err
	}
	if env.Signature == "" {
		return fmt.Errorf("signed config required but signature is missing")
	}
	pub, err := base64.StdEncoding.DecodeString(publicKey)
	if err != nil {
		return fmt.Errorf("decode config public key: %w", err)
	}
	sig, err := base64.StdEncoding.DecodeString(env.Signature)
	if err != nil {
		return fmt.Errorf("decode config signature: %w", err)
	}
	unsigned := env
	unsigned.Signature = ""
	payload, _ := json.Marshal(unsigned.Config)
	if !ed25519.Verify(ed25519.PublicKey(pub), payload, sig) {
		return fmt.Errorf("config signature verification failed")
	}
	return nil
}

func hashConfig(cfg config.Config) string {
	b, _ := json.Marshal(cfg)
	sum := sha256.Sum256(b)
	return "sha256:" + strings.ToLower(hex.EncodeToString(sum[:]))
}

func currentConfigHash(cfg config.Config) string {
	if cfg.ConfigETag != "" {
		return cfg.ConfigETag
	}
	return hashConfig(cfg)
}
