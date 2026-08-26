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
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

type Poller struct {
	client   *client.Client
	etag     string
	cache    string
	agentUID string
	agentID  string
	serverID string
}

const (
	configCacheVersion         = 2
	legacyCacheEnvelopeVersion = 1
)

// cacheEnvelope binds the last controller response to the appliance that
// issued it. The raw response is retained so the same validation and merge
// path is used after a service/agent upgrade as during a live poll.
type cacheEnvelope struct {
	Version       int             `json:"version"`
	ControllerURL string          `json:"controller_url"`
	AgentUID      string          `json:"agent_uid,omitempty"`
	AgentID       string          `json:"agent_id"`
	ServerID      string          `json:"server_id"`
	ETag          string          `json:"etag,omitempty"`
	Response      json.RawMessage `json:"response"`
}

func New(client *client.Client, cachePath, agentUID, agentID, serverID string) *Poller {
	return &Poller{
		client: client, cache: cachePath, agentUID: agentUID,
		agentID: agentID, serverID: serverID,
	}
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
	requestETag := p.etag
	if force {
		requestETag = ""
	}
	resp, body, err := p.client.GetJSON(ctx, "/api/v1/agents/config", requestETag, nil)
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusNotModified {
			return current, false, currentConfigHash(current), nil
		}
		return current, false, currentConfigHash(current), err
	}
	candidateETag := requestETag
	if resp != nil {
		if headerETag := resp.Header.Get("ETag"); headerETag != "" {
			candidateETag = headerETag
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
		next.ConfigETag = candidateETag
	}
	if next.ConfigETag != "" {
		candidateETag = next.ConfigETag
	}
	if p.cache != "" {
		if err := writeCache(
			p.cache, current.ControllerURL, p.agentUID, p.agentID, p.serverID,
			candidateETag, body,
		); err != nil {
			return current, false, currentConfigHash(current), fmt.Errorf("cache controller config: %w", err)
		}
	}
	// Commit the conditional request token only after the response has passed
	// decoding/signature checks and its last-known-good copy is durable. An
	// ETag from a rejected or unpersisted response must be retried, not hidden
	// behind a subsequent 304 response.
	p.etag = candidateETag
	return next, true, currentConfigHash(next), nil
}

// Restore applies the last successfully validated controller policy to the
// local configuration. Agent upgrades restart the Windows service; without
// this restore step the service reverted to installer defaults until its next
// successful config poll, which could temporarily disable one telemetry path
// while the APM gateway continued with durable state.
func Restore(cachePath string, current config.Config, agentUID, agentID, serverID string) (config.Config, bool, error) {
	if strings.TrimSpace(cachePath) == "" {
		return current, false, nil
	}
	body, err := os.ReadFile(cachePath)
	if err != nil {
		if os.IsNotExist(err) {
			return current, false, nil
		}
		return current, false, fmt.Errorf("read cached controller config: %w", err)
	}

	// 1.12.0 and older persisted the already-validated controller response
	// directly. Newer agents wrap that response with an appliance/machine
	// binding. Detect a real (or malformed) envelope explicitly; an invalid
	// envelope must never be retried as the more permissive legacy format.
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err == nil && looksLikeCacheEnvelope(fields) {
		var cached cacheEnvelope
		if err := json.Unmarshal(body, &cached); err != nil {
			return current, false, fmt.Errorf("parse cached controller config: %w", err)
		}
		if len(cached.Response) == 0 {
			return current, false, fmt.Errorf("cached controller config has an unsupported format")
		}
		if normalizeControllerBinding(cached.ControllerURL) != normalizeControllerBinding(current.ControllerURL) {
			return current, false, fmt.Errorf("cached controller config belongs to a different appliance")
		}
		switch cached.Version {
		case legacyCacheEnvelopeVersion:
			// Read compatibility for 1.12.1 development builds already deployed
			// before the stable machine binding was introduced.
			if cached.AgentID != strings.TrimSpace(agentID) || cached.ServerID != strings.TrimSpace(serverID) {
				return current, false, fmt.Errorf("cached controller config belongs to a different agent identity")
			}
		case configCacheVersion:
			if strings.TrimSpace(cached.AgentUID) == "" || cached.AgentUID != strings.TrimSpace(agentUID) {
				return current, false, fmt.Errorf("cached controller config belongs to a different machine identity")
			}
		default:
			return current, false, fmt.Errorf("cached controller config has an unsupported format")
		}
		return restoreResponse(cached.Response, current, cached.ETag)
	}

	// Legacy raw responses have no binding metadata. They remain acceptable
	// only through the same signature, invariant and validation checks as a
	// live poll. The next successful poll upgrades the file to a v2 envelope.
	return restoreResponse(body, current, "")
}

func looksLikeCacheEnvelope(fields map[string]json.RawMessage) bool {
	if _, ok := fields["response"]; ok {
		return true
	}
	_, hasVersion := fields["version"]
	_, hasController := fields["controller_url"]
	_, hasAgentUID := fields["agent_uid"]
	_, hasAgentID := fields["agent_id"]
	_, hasServerID := fields["server_id"]
	_, hasDataDir := fields["data_dir"]
	return hasVersion && hasController && !hasDataDir &&
		(hasAgentUID || (hasAgentID && hasServerID))
}

func restoreResponse(response []byte, current config.Config, cachedETag string) (config.Config, bool, error) {
	next, err := decodeConfig(response, current)
	if err != nil {
		return current, false, fmt.Errorf("decode cached controller config: %w", err)
	}
	if current.Security.RequireSignedConfig {
		if err := verifySignature(response, current.Security.ConfigPublicKey); err != nil {
			return current, false, fmt.Errorf("verify cached controller config: %w", err)
		}
	}
	// The cache may influence controller-managed policy, never the local
	// connection or state root used to locate the cache itself.
	if normalizeControllerBinding(next.ControllerURL) != normalizeControllerBinding(current.ControllerURL) {
		return current, false, fmt.Errorf("cached controller config attempted to change the appliance URL")
	}
	if filepath.Clean(next.DataDir) != filepath.Clean(current.DataDir) {
		return current, false, fmt.Errorf("cached controller config attempted to change the agent data directory")
	}
	if next.ConfigETag == "" {
		next.ConfigETag = cachedETag
	}
	if err := next.Validate(); err != nil {
		return current, false, fmt.Errorf("validate cached controller config: %w", err)
	}
	return next, true, nil
}

var replaceCacheFile = os.Rename

func writeCache(path, controllerURL, agentUID, agentID, serverID, etag string, response []byte) error {
	cached := cacheEnvelope{
		Version:       configCacheVersion,
		ControllerURL: normalizeControllerBinding(controllerURL),
		AgentUID:      strings.TrimSpace(agentUID),
		AgentID:       strings.TrimSpace(agentID),
		ServerID:      strings.TrimSpace(serverID),
		ETag:          etag,
		Response:      append([]byte(nil), response...),
	}
	body, err := json.Marshal(cached)
	if err != nil {
		return err
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	temp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-")
	if err != nil {
		return err
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if _, err := temp.Write(body); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tempPath, 0o600); err != nil {
		return err
	}
	// The temp file is synced and lives in the same directory. Go's Windows
	// implementation uses MoveFileEx(REPLACE_EXISTING), so replacement keeps
	// the previous complete cache available if the final rename itself fails.
	return replaceCacheFile(tempPath, path)
}

func normalizeControllerBinding(value string) string {
	return strings.ToLower(strings.TrimRight(strings.TrimSpace(value), "/"))
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
