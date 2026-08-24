package agent

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"time"

	apmruntime "zenplus-agent/internal/apm"
	"zenplus-agent/internal/backoff"
	"zenplus-agent/internal/client"
	"zenplus-agent/internal/collectors"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/configpoller"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/netcapture"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/selfupdate"
	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

// supervisor gates all controller traffic: exponential backoff with jitter
// during outages, a hard stop on rejected credentials (with automatic
// return to appliance-managed authorization), and controller backpressure.
type supervisor struct {
	comm            *backoff.Backoff
	enrollBO        *backoff.Backoff
	notBefore       time.Time // no controller traffic before this
	enrollNotBefore time.Time // no enrollment attempt before this
	authFailed      bool
	lastSkewLogged  time.Duration
}

const pendingAuthorizationPollInterval = 30 * time.Second

func newSupervisor() *supervisor {
	return &supervisor{
		comm:     backoff.New(5*time.Second, 10*time.Minute),
		enrollBO: backoff.New(30*time.Second, 30*time.Minute),
	}
}

func (s *supervisor) mayTalk(now time.Time) bool {
	return !s.authFailed && now.After(s.notBefore)
}

func (s *supervisor) onSuccess(status *model.Status) {
	s.comm.Reset()
	s.notBefore = time.Time{}
	status.NextRetryAt = nil
	if !s.authFailed {
		status.AuthState = "ok"
	}
}

// onError classifies a controller failure. 401/403 means our key is dead:
// stop all traffic and wait for re-enrollment. Anything else is an outage:
// back off exponentially with jitter.
func (s *supervisor) onError(err error, status *model.Status, logf func(string, ...any)) {
	now := time.Now().UTC()
	if client.IsUnauthorized(err) {
		if !s.authFailed {
			logf("controller rejected credentials: %v; suspending heartbeats/uploads until re-enrollment succeeds", err)
		}
		s.authFailed = true
		status.AuthState = "unauthorized"
		return
	}
	wait := s.comm.Next()
	s.notBefore = now.Add(wait)
	next := s.notBefore
	status.NextRetryAt = &next
	if attempts := s.comm.Attempts(); attempts <= 3 || attempts%10 == 0 {
		logf("controller unreachable (attempt %d): %v; next retry in %s", attempts, err, wait.Round(time.Second))
	}
}

func (s *supervisor) applyBackpressure(bp *model.Backpressure, status *model.Status, logf func(string, ...any)) {
	if bp == nil || bp.RetryAfterSeconds <= 0 {
		return
	}
	until := time.Now().UTC().Add(time.Duration(bp.RetryAfterSeconds) * time.Second)
	if until.After(s.notBefore) {
		s.notBefore = until
		next := until
		status.NextRetryAt = &next
		logf("controller requested backpressure: pausing uploads for %ds (%s)", bp.RetryAfterSeconds, bp.Reason)
	}
}

type Options struct {
	ConfigPath string
	Once       bool
	Duration   time.Duration
	Foreground bool
}

type localSettings struct {
	ControllerURL string `json:"controller_url"`
	ProxyURL      string `json:"proxy_url"`
	VerifyTLS     bool   `json:"verify_tls"`
	APMEnabled    bool   `json:"apm_enabled"`
}

func Run(ctx context.Context, opts Options) error {
	cfg, err := config.Load(opts.ConfigPath)
	if err != nil {
		return err
	}
	paths := runtime.NewPaths(cfg.DataDir)
	if err := paths.Ensure(); err != nil {
		return err
	}
	log, err := runtime.NewLogger(paths.LogFile, opts.Foreground)
	if err != nil {
		return err
	}
	defer log.Close()
	log.Printf("ZenPlus Agent %s starting controller=%s data_dir=%s", model.AgentVersion, cfg.ControllerURL, cfg.DataDir)

	store, err := spool.Open(paths.SpoolDB)
	if err != nil {
		return err
	}
	defer store.Close()
	startedAt := time.Now().UTC()
	// Publish a safe startup view before enrollment or the first collection can
	// spend time waiting on the controller. This keeps the all-users dashboard
	// responsive during outages and on first install.
	_ = runtime.WriteMachineDashboardSnapshot(cfg, identity.Identity{}, model.Status{
		ControllerURL:   cfg.ControllerURL,
		AgentVersion:    model.AgentVersion,
		StartedAt:       startedAt,
		CollectorErrors: map[string]string{},
		LocalAPM:        probeLocalAPM(cfg),
	})

	enrollCtx, cancel := enroll.ContextWithEnrollmentTimeout(ctx)
	enrollment, err := enroll.Ensure(enrollCtx, cfg, paths, log.Printf)
	cancel()
	if err != nil {
		log.Printf("enrollment error: %v", err)
	}
	applyEnrollment(&cfg, enrollment)

	up, poller, err := newRuntimeClients(cfg, paths, store, enrollment)
	if err != nil {
		return err
	}
	status := model.Status{
		AgentID:         enrollment.Identity.AgentID,
		ServerID:        enrollment.Identity.ServerID,
		ControllerURL:   cfg.ControllerURL,
		AgentVersion:    model.AgentVersion,
		StartedAt:       startedAt,
		CollectorErrors: map[string]string{},
		LocalAPM:        probeLocalAPM(cfg),
	}
	apmManager := apmruntime.New(paths, log.Printf)
	defer apmManager.Close()

	ctx, stop := signal.NotifyContext(ctx, os.Interrupt)
	defer stop()
	if opts.Duration > 0 {
		var timeout context.CancelFunc
		ctx, timeout = context.WithTimeout(ctx, opts.Duration)
		defer timeout()
	}
	if enrollment.Enrolled {
		apmManager.Reconcile(ctx, cfg, enrollment.Identity.AgentID, enrollment.Identity.ServerID, up.Client())
		status.LocalAPM = apmManager.Snapshot()
	}

	sup := newSupervisor()
	if !enrollment.Enrolled && enrollment.AuthorizationState != "" {
		sup.enrollNotBefore = time.Now().UTC().Add(pendingAuthorizationPollInterval)
		next := sup.enrollNotBefore
		status.NextRetryAt = &next
	}
	runCollection(ctx, cfg, paths, store, &status, enrollment, log.Printf)
	tickHeartbeat(ctx, opts.ConfigPath, &cfg, paths, store, &up, &poller, &status, &enrollment, apmManager, sup, log.Printf)
	tickUpload(ctx, store, up, &status, &enrollment, sup, log.Printf)
	syncAuthStatus(&status, enrollment, sup)
	_ = writeStatus(paths.StatusFile, status)
	_ = runtime.WriteMachineDashboardSnapshot(cfg, enrollment.Identity, status)
	if opts.Once {
		log.Printf("one-shot run complete")
		return nil
	}

	collectInterval := config.Duration(cfg.CollectIntervalSeconds, time.Minute)
	heartbeatInterval := config.Duration(cfg.HeartbeatIntervalSeconds, 30*time.Second)
	uploadInterval := config.Duration(cfg.UploadIntervalSeconds, 30*time.Second)
	configInterval := config.Duration(cfg.ConfigIntervalSeconds, 2*time.Minute)
	collectTicker := time.NewTicker(collectInterval)
	heartbeatTicker := time.NewTicker(heartbeatInterval)
	uploadTicker := time.NewTicker(uploadInterval)
	configTicker := time.NewTicker(configInterval)
	localConfigTicker := time.NewTicker(5 * time.Second)
	apmTicker := time.NewTicker(10 * time.Second)
	defer collectTicker.Stop()
	defer heartbeatTicker.Stop()
	defer uploadTicker.Stop()
	defer configTicker.Stop()
	defer localConfigTicker.Stop()
	defer apmTicker.Stop()
	localHash := localSettingsFingerprint(cfg)
	authStamp := authFileStamp(paths)
	syncTickers := func() {
		if next := config.Duration(cfg.CollectIntervalSeconds, time.Minute); next != collectInterval {
			collectTicker.Reset(next)
			collectInterval = next
			log.Printf("collection interval updated to %s", next)
		}
		if next := config.Duration(cfg.HeartbeatIntervalSeconds, 30*time.Second); next != heartbeatInterval {
			heartbeatTicker.Reset(next)
			heartbeatInterval = next
			log.Printf("heartbeat interval updated to %s", next)
		}
		if next := config.Duration(cfg.UploadIntervalSeconds, 30*time.Second); next != uploadInterval {
			uploadTicker.Reset(next)
			uploadInterval = next
			log.Printf("upload interval updated to %s", next)
		}
		if next := config.Duration(cfg.ConfigIntervalSeconds, 2*time.Minute); next != configInterval {
			configTicker.Reset(next)
			configInterval = next
			log.Printf("config interval updated to %s", next)
		}
	}

	for {
		select {
		case <-ctx.Done():
			log.Printf("agent stopping: %v", ctx.Err())
			return nil
		case <-collectTicker.C:
			runCollection(ctx, cfg, paths, store, &status, enrollment, log.Printf)
		case <-heartbeatTicker.C:
			tickHeartbeat(ctx, opts.ConfigPath, &cfg, paths, store, &up, &poller, &status, &enrollment, apmManager, sup, log.Printf)
		case <-uploadTicker.C:
			tickUpload(ctx, store, up, &status, &enrollment, sup, log.Printf)
		case <-configTicker.C:
			if enrollment.Enrolled && sup.mayTalk(time.Now().UTC()) {
				pollConfig(ctx, &cfg, poller, &status, log.Printf)
			}
		case <-localConfigTicker.C:
			rebuilt := refreshLocalRuntime(ctx, opts.ConfigPath, &cfg, paths, store, &enrollment, &up, &poller, &status, &localHash, &authStamp, log.Printf)
			if rebuilt && enrollment.Enrolled && sup.authFailed {
				// The appliance issued a new credential: resume traffic.
				sup.authFailed = false
				sup.enrollBO.Reset()
				sup.onSuccess(&status)
			}
		case <-apmTicker.C:
			var apmClient *client.Client
			if enrollment.Enrolled && up != nil {
				apmClient = up.Client()
			}
			apmManager.Reconcile(ctx, cfg, enrollment.Identity.AgentID, enrollment.Identity.ServerID, apmClient)
			status.LocalAPM = apmManager.Snapshot()
		}
		syncTickers()
		status.ControllerURL = cfg.ControllerURL
		status.AgentID = enrollment.Identity.AgentID
		status.ServerID = enrollment.Identity.ServerID
		syncAuthStatus(&status, enrollment, sup)
		_ = writeStatus(paths.StatusFile, status)
		_ = runtime.WriteMachineDashboardSnapshot(cfg, enrollment.Identity, status)
	}
}

func newRuntimeClients(cfg config.Config, paths runtime.Paths, store *spool.Store, enrollment enroll.Result) (*uploader.Uploader, *configpoller.Poller, error) {
	api, err := client.New(cfg.ControllerURL, cfg.ProxyURL, cfg.VerifyTLS, enrollment.Identity.AgentID, enrollment.APIKey)
	if err != nil {
		return nil, nil, err
	}
	return uploader.New(api, store, enrollment.Identity.AgentID, enrollment.Identity.ServerID), configpoller.New(api, paths.ConfigCache), nil
}

func refreshLocalRuntime(ctx context.Context, configPath string, cfg *config.Config, paths runtime.Paths, store *spool.Store, enrollment *enroll.Result, up **uploader.Uploader, poller **configpoller.Poller, status *model.Status, localHash *string, authStamp *string, logf func(string, ...any)) bool {
	diskCfg, err := config.Load(configPath)
	if err != nil {
		status.LastConfigError = "local config reload failed: " + err.Error()
		logf("%s", status.LastConfigError)
		return false
	}
	nextLocalHash := localSettingsFingerprint(diskCfg)
	nextAuthStamp := authFileStamp(paths)
	settingsChanged := nextLocalHash != *localHash
	authChanged := nextAuthStamp != *authStamp
	if !settingsChanged && !authChanged {
		return false
	}
	if settingsChanged {
		if filepath.Clean(diskCfg.DataDir) != filepath.Clean(paths.DataDir) {
			status.LastConfigError = "data_dir changed on disk; restart the agent to apply the new data directory"
			logf("%s", status.LastConfigError)
			*localHash = nextLocalHash
			return false
		}
		connectionChanged := cfg.ControllerURL != diskCfg.ControllerURL || cfg.ProxyURL != diskCfg.ProxyURL || cfg.VerifyTLS != diskCfg.VerifyTLS
		cfg.ControllerURL = diskCfg.ControllerURL
		cfg.ProxyURL = diskCfg.ProxyURL
		cfg.VerifyTLS = diskCfg.VerifyTLS
		cfg.APM.Enabled = diskCfg.APM.Enabled
		if connectionChanged {
			cfg.ConfigETag = ""
			cfg.ConfigVersion = 0
		}
		*localHash = nextLocalHash
		logf("local connection settings applied: controller=%s", cfg.ControllerURL)
	}

	enrollCtx, cancel := enroll.ContextWithEnrollmentTimeout(ctx)
	nextEnrollment, err := enroll.Ensure(enrollCtx, *cfg, paths, logf)
	cancel()
	if err != nil {
		status.LastConfigError = "local enrollment refresh failed: " + err.Error()
		logf("%s", status.LastConfigError)
		return false
	}
	applyEnrollment(cfg, nextEnrollment)
	enrollmentChanged := !sameEnrollment(*enrollment, nextEnrollment)
	*enrollment = nextEnrollment
	nextUp, nextPoller, err := newRuntimeClients(*cfg, paths, store, nextEnrollment)
	if err != nil {
		status.LastConfigError = "runtime client rebuild failed: " + err.Error()
		logf("%s", status.LastConfigError)
		return false
	}
	*up = nextUp
	*poller = nextPoller
	*authStamp = authFileStamp(paths)
	status.LastConfigError = ""
	status.ControllerURL = cfg.ControllerURL
	status.AgentID = nextEnrollment.Identity.AgentID
	status.ServerID = nextEnrollment.Identity.ServerID
	if settingsChanged || authChanged || enrollmentChanged {
		logf("runtime client refreshed: controller=%s agent_id=%s server_id=%s", cfg.ControllerURL, nextEnrollment.Identity.AgentID, nextEnrollment.Identity.ServerID)
	}
	return true
}

func CollectNow(ctx context.Context, configPath string) error {
	cfg, err := config.Load(configPath)
	if err != nil {
		return err
	}
	paths := runtime.NewPaths(cfg.DataDir)
	if err := paths.Ensure(); err != nil {
		return err
	}
	store, err := spool.Open(paths.SpoolDB)
	if err != nil {
		return err
	}
	defer store.Close()
	enrollment, err := enroll.Ensure(ctx, cfg, paths, func(string, ...any) {})
	if err != nil {
		return err
	}
	applyEnrollment(&cfg, enrollment)
	status := model.Status{
		AgentID:         enrollment.Identity.AgentID,
		ServerID:        enrollment.Identity.ServerID,
		ControllerURL:   cfg.ControllerURL,
		AgentVersion:    model.AgentVersion,
		StartedAt:       time.Now().UTC(),
		CollectorErrors: map[string]string{},
	}
	runCollection(ctx, cfg, paths, store, &status, enrollment, func(string, ...any) {})
	if err := writeStatus(paths.StatusFile, status); err != nil {
		return err
	}
	return runtime.WriteMachineDashboardSnapshot(cfg, enrollment.Identity, status)
}

func RegisterNow(ctx context.Context, configPath string) (enroll.Result, error) {
	cfg, err := config.Load(configPath)
	if err != nil {
		return enroll.Result{}, err
	}
	paths := runtime.NewPaths(cfg.DataDir)
	if err := paths.Ensure(); err != nil {
		return enroll.Result{}, err
	}
	if err := os.Remove(paths.CredentialFile); err != nil && !os.IsNotExist(err) {
		return enroll.Result{}, err
	}
	if err := os.Remove(paths.CredentialMeta); err != nil && !os.IsNotExist(err) {
		return enroll.Result{}, err
	}
	enrollment, err := enroll.Ensure(ctx, cfg, paths, func(string, ...any) {})
	if err != nil {
		return enrollment, err
	}
	if !enrollment.Enrolled {
		return enrollment, nil
	}
	return enrollment, nil
}

func runCollection(ctx context.Context, cfg config.Config, paths runtime.Paths, store *spool.Store, status *model.Status, enrollment enroll.Result, logf func(string, ...any)) {
	timeout := config.Duration(cfg.CollectorTimeoutSeconds, 20*time.Second)
	collectCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	result := collectors.Collect(collectCtx, cfg)
	now := time.Now().UTC()
	st, _ := store.Stats()
	health := model.Health{
		Status:          "ok",
		QueueDepth:      st.Depth,
		SpoolBytes:      st.Bytes,
		CollectorErrors: result.Errors,
	}
	if len(result.Errors) > 0 {
		health.Status = "degraded"
	}
	result.Metrics = append(result.Metrics, model.Metric{
		Kind:      "agent_health",
		Timestamp: now,
		Data: map[string]any{
			"queue_depth":     st.Depth,
			"spool_bytes":     st.Bytes,
			"config_apply_ok": status.LastConfigError == "",
			"last_error":      firstNonEmpty(status.LastUploadError, status.LastHeartbeatError, status.LastConfigError, collectorErrorSummary(result.Errors)),
		},
	})
	seq := uint64(time.Now().UnixNano())
	seqEnd := seq
	if len(result.Metrics) > 0 {
		seqEnd = seq + uint64(len(result.Metrics)) - 1
	}
	batch := model.Batch{
		AgentID:       enrollment.Identity.AgentID,
		ServerID:      enrollment.Identity.ServerID,
		BatchID:       randomUUID(),
		SequenceStart: seq,
		SequenceEnd:   seqEnd,
		ConfigHash:    config.Hash(cfg),
		AgentVersion:  model.AgentVersion,
		CollectedAt:   now,
		Metrics:       result.Metrics,
		Inventory:     result.Inventory,
		Events:        result.Events,
		Health:        health,
	}
	payload, err := json.Marshal(batch)
	if err != nil {
		logf("collection marshal failed: %v", err)
		return
	}
	if cfg.Limits.MaxPayloadBytes > 0 && len(payload) > cfg.Limits.MaxPayloadBytes {
		logf("collection payload too large: %d bytes", len(payload))
		return
	}
	if _, err := store.Enqueue(batch.BatchID, payload, cfg.Spool.MaxBytes); err != nil {
		logf("spool enqueue failed: %v", err)
		return
	}
	status.LastCollection = &now
	status.CollectorErrors = result.Errors
	_ = store.Prune(time.Duration(cfg.Spool.MaxAgeHours)*time.Hour, cfg.Spool.MaxBytes)
	st, _ = store.Stats()
	status.QueueDepth = st.Depth
	status.SpoolBytes = st.Bytes
	logf("collected %d metrics, %d event summaries; queue_depth=%d spool_bytes=%d", len(result.Metrics), len(result.Events), st.Depth, st.Bytes)
	_ = paths
}

func collectorErrorSummary(collectorErrors map[string]string) string {
	if len(collectorErrors) == 0 {
		return ""
	}
	keys := make([]string, 0, len(collectorErrors))
	for name := range collectorErrors {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, name := range keys {
		message := strings.TrimSpace(collectorErrors[name])
		if message == "" {
			message = "collection failed"
		}
		parts = append(parts, name+": "+message)
	}
	return strings.Join(parts, "; ")
}

// tickHeartbeat drives the heartbeat cadence. When the agent has no valid
// credentials (never enrolled, or the controller rejected the key) it does
// not touch the controller at all except for a backoff-gated enrollment
// attempt — no more unauthenticated 401 storms.
func tickHeartbeat(ctx context.Context, configPath string, cfg *config.Config, paths runtime.Paths, store *spool.Store, up **uploader.Uploader, poller **configpoller.Poller, status *model.Status, enrollment *enroll.Result, apmManager *apmruntime.Manager, sup *supervisor, logf func(string, ...any)) {
	now := time.Now().UTC()
	if !enrollment.Enrolled || sup.authFailed {
		tryRecoverAuth(ctx, configPath, cfg, paths, store, up, poller, status, enrollment, sup, logf)
		return
	}
	if !sup.mayTalk(now) {
		return
	}
	hb, err := sendHeartbeat(ctx, *cfg, store, *up, status, *enrollment, sup, logf)
	if err != nil {
		sup.onError(err, status, logf)
		return
	}
	sup.onSuccess(status)
	handleHeartbeatResponse(ctx, hb, cfg, *poller, paths, store, *up, status, *enrollment, apmManager, sup, logf)
}

func tickUpload(ctx context.Context, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment *enroll.Result, sup *supervisor, logf func(string, ...any)) {
	if !enrollment.Enrolled || !sup.mayTalk(time.Now().UTC()) {
		return
	}
	if err := drain(ctx, store, up, status, logf); err != nil {
		sup.onError(err, status, logf)
	} else {
		sup.onSuccess(status)
	}
}

// tryRecoverAuth polls the controller-managed authorization state, gated by
// exponential backoff. The agent never needs an operator-supplied token.
func tryRecoverAuth(ctx context.Context, configPath string, cfg *config.Config, paths runtime.Paths, store *spool.Store, up **uploader.Uploader, poller **configpoller.Poller, status *model.Status, enrollment *enroll.Result, sup *supervisor, logf func(string, ...any)) {
	now := time.Now().UTC()
	if now.Before(sup.enrollNotBefore) {
		return
	}
	enrollCtx, cancel := enroll.ContextWithEnrollmentTimeout(ctx)
	var (
		res enroll.Result
		err error
	)
	if sup.authFailed {
		res, err = enroll.Recover(enrollCtx, *cfg, paths, logf)
	} else {
		res, err = enroll.Ensure(enrollCtx, *cfg, paths, logf)
	}
	cancel()
	if err != nil {
		wait := sup.enrollBO.Next()
		sup.enrollNotBefore = now.Add(wait)
		next := sup.enrollNotBefore
		status.NextRetryAt = &next
		if err != nil {
			status.LastHeartbeatError = "enrollment failed: " + err.Error()
			if attempts := sup.enrollBO.Attempts(); attempts <= 3 || attempts%10 == 0 {
				logf("enrollment attempt %d failed: %v; next attempt in %s", attempts, err, wait.Round(time.Second))
			}
		}
		return
	}
	applyEnrollment(cfg, res)
	nextUp, nextPoller, cerr := newRuntimeClients(*cfg, paths, store, res)
	if cerr != nil {
		status.LastConfigError = "runtime client rebuild failed: " + cerr.Error()
		logf("%s", status.LastConfigError)
		return
	}
	*enrollment = res
	*up = nextUp
	*poller = nextPoller
	status.AgentID = res.Identity.AgentID
	status.ServerID = res.Identity.ServerID
	if !res.Enrolled {
		// Pending is a valid controller response, not an outage. Poll on a
		// predictable cadence so an administrator's approval is picked up
		// promptly without turning into a request storm.
		wait := pendingAuthorizationPollInterval
		sup.enrollBO.Reset()
		sup.enrollNotBefore = now.Add(wait)
		next := sup.enrollNotBefore
		status.NextRetryAt = &next
		status.LastHeartbeatError = ""
		syncAuthStatus(status, res, sup)
		return
	}
	sup.authFailed = false
	sup.enrollBO.Reset()
	sup.enrollNotBefore = time.Time{}
	sup.onSuccess(status)
	status.LastHeartbeatError = ""
	logf("enrollment recovered: agent_id=%s server_id=%s", res.Identity.AgentID, res.Identity.ServerID)
}

func syncAuthStatus(status *model.Status, enrollment enroll.Result, sup *supervisor) {
	status.Enrolled = enrollment.Enrolled
	switch {
	case enrollment.AuthorizationState == "revoked":
		status.AuthState = "revoked"
	case sup.authFailed:
		status.AuthState = "unauthorized"
	case !enrollment.Enrolled:
		status.AuthState = "pending"
	default:
		status.AuthState = "ok"
	}
}

func sendHeartbeat(ctx context.Context, cfg config.Config, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, sup *supervisor, logf func(string, ...any)) (*model.HeartbeatResponse, error) {
	st, _ := store.Stats()
	now := time.Now().UTC()
	localAPM := status.LocalAPM
	if localAPM == nil {
		localAPM = probeLocalAPM(cfg)
	}
	hb := model.Heartbeat{
		Version:          model.AgentVersion,
		Capabilities:     capabilitiesForConfig(cfg, canManageApplicationInstrumentation()),
		UptimeSeconds:    uptimeSeconds(enrollment.Identity.BootTime, now),
		QueueDepth:       st.Depth,
		SpoolBytes:       st.Bytes,
		ConfigHash:       config.Hash(cfg),
		ConfigApplyError: status.LastConfigError,
		APM:              localAPM,
	}
	status.LocalAPM = hb.APM
	resp, err := up.SendHeartbeat(ctx, hb)
	status.LastHeartbeat = &now
	if err != nil {
		status.LastHeartbeatError = err.Error()
		return nil, err
	}
	status.LastHeartbeatError = ""
	if !resp.ServerTime.IsZero() {
		skew := now.Sub(resp.ServerTime)
		status.ClockSkewSeconds = skew.Seconds()
		if skew > 2*time.Minute || skew < -2*time.Minute {
			status.LastHeartbeatError = fmt.Sprintf("clock skew: local clock differs from controller by %s; check this host's NTP/time sync", skew.Round(time.Second))
			if delta := skew - sup.lastSkewLogged; delta > 30*time.Second || delta < -30*time.Second {
				sup.lastSkewLogged = skew
				logf("%s", status.LastHeartbeatError)
			}
		} else {
			sup.lastSkewLogged = 0
		}
	}
	return &resp, nil
}

func capabilitiesForConfig(cfg config.Config, privileged bool) []string {
	allowInstrumentation := privileged && cfg.APM.Enabled && cfg.APM.Profile != "infrastructure"
	capabilities := make([]string, 0, len(model.AgentCapabilities))
	for _, capability := range model.AgentCapabilities {
		if !allowInstrumentation && (capability == "apm_iis_instrumentation_v1" || capability == "apm_windows_service_instrumentation_v1") {
			continue
		}
		capabilities = append(capabilities, capability)
	}
	return capabilities
}

func probeLocalAPM(cfg config.Config) *model.AgentAPMHeartbeat {
	now := time.Now().UTC()
	result := &model.AgentAPMHeartbeat{
		Enabled:   cfg.APM.Enabled,
		Gateway:   model.APMGatewayStatus{GRPCPort: 4317, HTTPPort: 4318},
		CheckedAt: now,
	}
	if !cfg.APM.Enabled {
		return result
	}
	grpc := localPortListening(4317)
	http := localPortListening(4318)
	result.Gateway.Listening = grpc || http
	if grpc {
		result.Bundles = map[string]string{"otlp_grpc": "listening"}
	}
	if http {
		if result.Bundles == nil {
			result.Bundles = map[string]string{}
		}
		result.Bundles["otlp_http"] = "listening"
	}
	return result
}

func localPortListening(port int) bool {
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 250*time.Millisecond)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

func drain(ctx context.Context, store *spool.Store, up *uploader.Uploader, status *model.Status, logf func(string, ...any)) error {
	count, err := up.Drain(ctx, 10)
	st, _ := store.Stats()
	status.QueueDepth = st.Depth
	status.SpoolBytes = st.Bytes
	if err != nil {
		status.LastUploadError = err.Error()
		logf("upload failed after %d batch(es): %v", count, err)
		return err
	}
	if count > 0 {
		now := time.Now().UTC()
		status.LastUpload = &now
		status.LastUploadError = ""
		logf("uploaded %d batch(es); queue_depth=%d", count, st.Depth)
	}
	return nil
}

func pollConfig(ctx context.Context, cfg *config.Config, poller *configpoller.Poller, status *model.Status, logf func(string, ...any)) {
	pollConfigWithForce(ctx, cfg, poller, status, false, logf)
}

func pollConfigWithForce(ctx context.Context, cfg *config.Config, poller *configpoller.Poller, status *model.Status, force bool, logf func(string, ...any)) {
	now := time.Now().UTC()
	var (
		next    config.Config
		changed bool
		hash    string
		err     error
	)
	if force {
		next, changed, hash, err = poller.PollForce(ctx, *cfg)
	} else {
		next, changed, hash, err = poller.Poll(ctx, *cfg)
	}
	status.LastConfigPoll = &now
	if err != nil {
		status.LastConfigError = err.Error()
		logf("config poll failed: %v", err)
		return
	}
	status.LastConfigError = ""
	if changed {
		*cfg = next
		// New policy may carry new per-collector intervals; let them take
		// effect from the next tick rather than the next full interval.
		collectors.ResetSchedule()
		logf("config updated: %s", hash)
	}
}

func handleHeartbeatResponse(ctx context.Context, hb *model.HeartbeatResponse, cfg *config.Config, poller *configpoller.Poller, paths runtime.Paths, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, apmManager *apmruntime.Manager, sup *supervisor, logf func(string, ...any)) {
	sup.applyBackpressure(hb.Backpressure, status, logf)
	if hb.APM != nil {
		status.APM = hb.APM
	}
	if hb.ConfigETag != "" && hb.ConfigETag != cfg.ConfigETag {
		pollConfig(ctx, cfg, poller, status, logf)
	}
	if hb.DesiredVersion != nil && *hb.DesiredVersion != "" && *hb.DesiredVersion != model.AgentVersion {
		logf("controller requested desired_version=%s (running %s); starting self-update", *hb.DesiredVersion, model.AgentVersion)
		status.UpgradeState = "requested " + *hb.DesiredVersion
		startSelfUpdate(ctx, up, cfg.UpdateRing, logf)
	}
	if hb.HasCommands {
		handleCommands(ctx, cfg, poller, paths, store, up, status, enrollment, apmManager, logf)
	}
}

// startSelfUpdate checks the controller's package manifest and, when a newer
// version is published, downloads + verifies + installs it in the background.
// selfupdate itself serializes attempts and enforces a per-version cooldown.
func startSelfUpdate(ctx context.Context, up *uploader.Uploader, channel string, logf func(string, ...any)) {
	go func() {
		m, err := selfupdate.FetchPublishedManifest(ctx, up.Client(), channel)
		if err != nil {
			logf("self-update: manifest fetch failed: %v", err)
			return
		}
		if m.LatestVersion == model.AgentVersion {
			logf("self-update: already running the published version %s", model.AgentVersion)
			return
		}
		if err := selfupdate.Apply(ctx, up.Client(), m, model.AgentVersion, logf); err != nil {
			logf("self-update to %s failed: %v", m.LatestVersion, err)
		}
	}()
}

func handleCommands(ctx context.Context, cfg *config.Config, poller *configpoller.Poller, paths runtime.Paths, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, apmManager *apmruntime.Manager, logf func(string, ...any)) {
	commands, err := up.PollCommands(ctx)
	if err != nil {
		logf("command poll failed: %v", err)
		return
	}
	for _, cmd := range commands {
		result := executeCommand(ctx, cmd, cfg, poller, paths, store, up, status, enrollment, apmManager, logf)
		if err := up.SendCommandResult(ctx, cmd.ID, result); err != nil {
			logf("command result failed command_id=%s: %v", cmd.ID, err)
		}
	}
}

func executeCommand(ctx context.Context, cmd model.Command, cfg *config.Config, poller *configpoller.Poller, paths runtime.Paths, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, apmManager *apmruntime.Manager, logf func(string, ...any)) model.CommandResult {
	if cmd.ExpiresAt != nil && time.Now().UTC().After(*cmd.ExpiresAt) {
		return model.CommandResult{Success: false, ErrorMessage: "command expired before execution"}
	}
	switch cmd.Command {
	case "status":
		return model.CommandResult{Success: true, Output: map[string]any{
			"agent_id":             status.AgentID,
			"server_id":            status.ServerID,
			"version":              model.AgentVersion,
			"queue_depth":          status.QueueDepth,
			"spool_bytes":          status.SpoolBytes,
			"last_upload_error":    status.LastUploadError,
			"last_heartbeat_error": status.LastHeartbeatError,
			"last_config_error":    status.LastConfigError,
		}}
	case "collect_now":
		runCollection(ctx, *cfg, paths, store, status, enrollment, logf)
		count, err := up.Drain(ctx, 10)
		if err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error(), Output: map[string]any{"uploaded_batches": count}}
		}
		return model.CommandResult{Success: true, Output: map[string]any{"uploaded_batches": count}}
	case "refresh_config":
		pollConfigWithForce(ctx, cfg, poller, status, true, logf)
		if status.LastConfigError != "" {
			return model.CommandResult{Success: false, ErrorMessage: status.LastConfigError}
		}
		return model.CommandResult{Success: true, Output: map[string]any{"config_hash": cfg.ConfigETag}}
	case "upload_diagnostics":
		req, err := diagnosticsRequest(paths, enrollment, cmd)
		if err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error()}
		}
		if err := up.RegisterDiagnostics(ctx, req); err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error()}
		}
		return model.CommandResult{Success: true, Output: map[string]any{"file_name": req.FileName, "sha256": req.SHA256}}
	case "start_network_capture":
		captureID, _ := cmd.Params["capture_id"].(string)
		if captureID == "" {
			captureID = cmd.ID
		}
		opts := netcapture.NormalizeOptions(netcapture.Options{
			Duration:       time.Duration(paramInt(cmd.Params, "duration_s", 300)) * time.Second,
			SampleInterval: time.Duration(paramInt(cmd.Params, "sample_interval_s", 2)) * time.Second,
			FlushInterval:  time.Duration(paramInt(cmd.Params, "flush_interval_s", 10)) * time.Second,
			MaxFlows:       paramInt(cmd.Params, "max_flows", 5000),
		})
		if iface, ok := cmd.Params["interface"].(string); ok {
			opts.Interface = strings.TrimSpace(iface)
		}
		if err := netcapture.ValidateInterface(opts.Interface); err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error()}
		}
		started, err := networkCaptures.Start(ctx, captureID, opts, up, logf)
		if err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error()}
		}
		return model.CommandResult{Success: true, Output: map[string]any{
			"capture_id": started.CaptureID,
			"duration_s": int(opts.Duration.Seconds()),
			"interface":  opts.Interface,
			"status":     started.Status,
			"duplicate":  started.Duplicate,
		}}
	case "stop_network_capture":
		captureID, _ := cmd.Params["capture_id"].(string)
		stopped, err := networkCaptures.Stop(ctx, captureID)
		if err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error()}
		}
		return model.CommandResult{Success: true, Output: map[string]any{
			"capture_id": stopped.CaptureID,
			"status":     stopped.Status,
			"duplicate":  stopped.Duplicate,
		}}
	case "apm_instrument", "apm_uninstrument", "apm_restart_target":
		if cmd.Command != "apm_uninstrument" && (cfg == nil || !cfg.APM.Enabled || cfg.APM.Profile == "infrastructure") {
			return model.CommandResult{
				Success:      false,
				ErrorMessage: "managed instrumentation is unavailable while the agent monitoring profile has APM disabled",
			}
		}
		if !canManageApplicationInstrumentation() {
			return model.CommandResult{
				Success:      false,
				ErrorMessage: "managed instrumentation requires the all-users Windows service installation",
			}
		}
		if apmManager == nil {
			return model.CommandResult{Success: false, ErrorMessage: "local APM manager is unavailable"}
		}
		enabled := cmd.Command != "apm_uninstrument"
		restart := paramBool(cmd.Params, "restart", false) || cmd.Command == "apm_restart_target"
		result, err := apmManager.Instrument(ctx, apmruntime.InstrumentationRequest{
			Enabled: enabled, Runtime: paramString(cmd.Params, "runtime"),
			ProcessKey: paramString(cmd.Params, "process_key"),
			TargetKind: paramString(cmd.Params, "target_kind"), TargetName: paramString(cmd.Params, "target_name"),
			ServiceName: paramString(cmd.Params, "service_name"), Environment: paramString(cmd.Params, "environment"),
			Restart: restart,
		})
		if err != nil {
			return model.CommandResult{Success: false, ErrorMessage: err.Error(), Output: map[string]any{
				"process_key": paramString(cmd.Params, "process_key"), "target_name": paramString(cmd.Params, "target_name"),
				"instrumentation_state": "failed",
			}}
		}
		encoded, _ := json.Marshal(result)
		output := map[string]any{}
		_ = json.Unmarshal(encoded, &output)
		output["process_key"] = paramString(cmd.Params, "process_key")
		return model.CommandResult{Success: true, Output: output}
	case "apm_set_config":
		return model.CommandResult{Success: false, ErrorMessage: "per-process APM sampling and log-source controls are planned for P2"}
	case "rotate_certificate":
		return model.CommandResult{Success: false, ErrorMessage: "rotate_certificate is reserved for the future mTLS contract and is not enabled in this build"}
	case "restart_agent":
		return model.CommandResult{Success: false, ErrorMessage: "restart_agent requires service-manager integration and is not enabled in this foreground command handler"}
	case "upgrade_agent":
		manifest, err := selfupdate.FetchPublishedManifest(ctx, up.Client(), cfg.UpdateRing)
		if err != nil {
			return model.CommandResult{Success: false, ErrorMessage: "package manifest unavailable: " + err.Error()}
		}
		if manifest.LatestVersion == model.AgentVersion {
			return model.CommandResult{Success: true, Output: map[string]any{
				"status":  "already at published version",
				"version": model.AgentVersion,
			}}
		}
		if target, ok := cmd.Params["version"].(string); ok && target != "" && target != manifest.LatestVersion {
			logf("upgrade_agent requested version %s but the published manifest serves %s; installing the published version", target, manifest.LatestVersion)
		}
		status.UpgradeState = "installing " + manifest.LatestVersion
		go func() {
			if err := selfupdate.Apply(ctx, up.Client(), manifest, model.AgentVersion, logf); err != nil {
				logf("self-update to %s failed: %v", manifest.LatestVersion, err)
			}
		}()
		return model.CommandResult{Success: true, Output: map[string]any{
			"status":       "upgrade started",
			"from_version": model.AgentVersion,
			"to_version":   manifest.LatestVersion,
			"file_name":    manifest.FileName,
		}}
	default:
		return model.CommandResult{Success: false, ErrorMessage: "unsupported command: " + cmd.Command}
	}
}

func paramInt(params map[string]any, key string, fallback int) int {
	switch v := params[key].(type) {
	case float64: // JSON numbers decode as float64
		if v > 0 {
			return int(v)
		}
	case int:
		if v > 0 {
			return v
		}
	}
	return fallback
}

func paramString(params map[string]any, key string) string {
	value, _ := params[key].(string)
	return strings.TrimSpace(value)
}

func paramBool(params map[string]any, key string, fallback bool) bool {
	value, ok := params[key].(bool)
	if !ok {
		return fallback
	}
	return value
}

func toModelFlows(flows []netcapture.Flow) []model.NetworkFlow {
	out := make([]model.NetworkFlow, 0, len(flows))
	for _, f := range flows {
		out = append(out, model.NetworkFlow{
			Protocol: f.Protocol, Kind: f.Kind, Direction: f.Direction,
			LocalIP: f.LocalIP, LocalPort: f.LocalPort,
			RemoteIP: f.RemoteIP, RemotePort: f.RemotePort,
			PID: f.PID, ProcessName: f.ProcessName, ServiceName: f.ServiceName,
			State: f.State, BytesSent: f.BytesSent, BytesReceived: f.BytesReceived,
			BytesKnown: f.BytesKnown, FirstSeen: f.FirstSeen, LastSeen: f.LastSeen,
			Samples: f.Samples,
		})
	}
	return out
}

func toModelInterfaces(samples []netcapture.InterfaceTraffic) []model.NetworkInterfaceTraffic {
	out := make([]model.NetworkInterfaceTraffic, 0, len(samples))
	for _, sample := range samples {
		out = append(out, model.NetworkInterfaceTraffic{
			Interface: sample.Interface, InterfaceIndex: sample.InterfaceIndex,
			Timestamp: sample.Timestamp, RXBytes: sample.RXBytes, TXBytes: sample.TXBytes,
			RXBPS: sample.RXBPS, TXBPS: sample.TXBPS,
			PeakRXBPS: sample.PeakRXBPS, PeakTXBPS: sample.PeakTXBPS,
			LinkSpeedBPS:         sample.LinkSpeedBPS,
			ReceiveLinkSpeedBPS:  sample.ReceiveLinkSpeedBPS,
			TransmitLinkSpeedBPS: sample.TransmitLinkSpeedBPS,
			RXUtilizationPct:     sample.RXUtilizationPct,
			TXUtilizationPct:     sample.TXUtilizationPct,
		})
	}
	return out
}

func runNetworkCapture(ctx context.Context, captureID string, opts netcapture.Options, up captureSender, logf func(string, ...any)) string {
	logf("network capture %s starting: duration=%s interval=%s interface=%q",
		captureID, opts.Duration, opts.SampleInterval, opts.Interface)

	send := func(flows []netcapture.Flow, final bool, st netcapture.Stats, errMsg string) {
		status := "running"
		if final {
			status = "completed"
		}
		if final && ctx.Err() != nil {
			status = "cancelled"
		}
		if errMsg != "" {
			status = "failed"
		}
		payload := model.NetworkCaptureUpload{
			CaptureID: captureID, Status: status, Interface: opts.Interface,
			StartedAt: st.StartedAt, EndsAt: st.EndsAt, Samples: st.Samples,
			Truncated: st.Truncated, BytesAvailable: st.BytesAvailable,
			Note: st.Note, ErrorMessage: errMsg, Flows: toModelFlows(flows),
			Interfaces: toModelInterfaces(st.Interfaces),
		}
		// A final cancelled status still has to leave the host after the capture
		// context is cancelled, so uploads use a short independent deadline.
		baseCtx := ctx
		if final && ctx.Err() != nil {
			baseCtx = context.WithoutCancel(ctx)
		}
		sendCtx, cancel := context.WithTimeout(baseCtx, 30*time.Second)
		defer cancel()
		if err := up.SendNetworkCapture(sendCtx, payload); err != nil {
			logf("network capture %s upload failed: %v", captureID, err)
		}
	}

	stats, err := netcapture.Run(ctx, opts, func(flows []netcapture.Flow, final bool, st netcapture.Stats) {
		send(flows, final, st, "")
	}, logf)
	if err != nil && ctx.Err() == nil {
		logf("network capture %s failed: %v", captureID, err)
		send(nil, true, stats, err.Error())
		return "failed"
	}
	if err != nil && ctx.Err() != nil {
		logf("network capture %s cancelled: %d samples, %d flows", captureID, stats.Samples, stats.FlowCount)
		return "cancelled"
	}
	logf("network capture %s finished: %d samples, %d flows", captureID, stats.Samples, stats.FlowCount)
	return "completed"
}

func diagnosticsRequest(paths runtime.Paths, enrollment enroll.Result, cmd model.Command) (model.DiagnosticsRequest, error) {
	info, err := os.Stat(paths.LogFile)
	if err != nil {
		return model.DiagnosticsRequest{}, err
	}
	sum, err := sha256File(paths.LogFile)
	if err != nil {
		return model.DiagnosticsRequest{}, err
	}
	req := model.DiagnosticsRequest{
		AgentID:  enrollment.Identity.AgentID,
		FileName: filepath.Base(paths.LogFile),
		FileSize: info.Size(),
		SHA256:   sum,
		Notes:    "redacted local agent log metadata; bundle upload transport is pending in the controller contract",
	}
	if value, ok := cmd.Params["diagnostic_id"].(string); ok {
		req.DiagnosticID = value
	}
	return req, nil
}

func sha256File(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

func applyEnrollment(cfg *config.Config, result enroll.Result) {
	if result.HeartbeatIntervalSeconds > 0 {
		cfg.HeartbeatIntervalSeconds = result.HeartbeatIntervalSeconds
	}
	if result.ConfigPollIntervalSeconds > 0 {
		cfg.ConfigIntervalSeconds = result.ConfigPollIntervalSeconds
	}
	if result.UploadIntervalSeconds > 0 {
		cfg.UploadIntervalSeconds = result.UploadIntervalSeconds
	}
	if result.PolicyID != "" {
		cfg.PolicyID = result.PolicyID
	}
}

func uptimeSeconds(boot time.Time, now time.Time) int64 {
	if boot.IsZero() || now.Before(boot) {
		return 0
	}
	return int64(now.Sub(boot).Seconds())
}

func writeStatus(path string, status model.Status) error {
	b, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o600)
}

func ReadStatus(configPath string) (model.Status, error) {
	cfg, err := config.Load(configPath)
	if err != nil {
		return model.Status{}, err
	}
	paths := runtime.NewPaths(cfg.DataDir)
	b, err := os.ReadFile(paths.StatusFile)
	if err != nil {
		return model.Status{}, err
	}
	var status model.Status
	if err := json.Unmarshal(b, &status); err != nil {
		return model.Status{}, err
	}
	return status, nil
}

func PrintConfig(configPath string) error {
	cfg, err := config.Load(configPath)
	if err != nil {
		return err
	}
	b, _ := json.MarshalIndent(printableConfig(cfg), "", "  ")
	fmt.Println(string(b))
	return nil
}

func printableConfig(cfg config.Config) config.Config {
	return cfg
}

func ResetEnrollment(configPath string) error {
	cfg, err := config.Load(configPath)
	if err != nil {
		return err
	}
	paths := runtime.NewPaths(cfg.DataDir)
	return enroll.Reset(paths)
}

func localSettingsFingerprint(cfg config.Config) string {
	b, _ := json.Marshal(localSettings{
		ControllerURL: cfg.ControllerURL,
		ProxyURL:      cfg.ProxyURL,
		VerifyTLS:     cfg.VerifyTLS,
		APMEnabled:    cfg.APM.Enabled,
	})
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func authFileStamp(paths runtime.Paths) string {
	// Registration polling refreshes identity inventory on disk. Identity
	// mtime alone must not trigger a second enrollment request from the local
	// settings watcher; credential state changes still trigger immediately.
	return fileStamp(paths.CredentialFile) + "|" + fileStamp(paths.CredentialMeta) + "|" + fileStamp(paths.PendingSecret)
}

func fileStamp(path string) string {
	info, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "missing:" + path
		}
		return "error:" + path + ":" + err.Error()
	}
	return fmt.Sprintf("%s:%d:%d", path, info.Size(), info.ModTime().UnixNano())
}

func sameEnrollment(a enroll.Result, b enroll.Result) bool {
	return a.Identity.AgentID == b.Identity.AgentID &&
		a.Identity.ServerID == b.Identity.ServerID &&
		a.APIKey == b.APIKey &&
		a.Enrolled == b.Enrolled
}

func randomUUID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex.EncodeToString(b[0:4]),
		hex.EncodeToString(b[4:6]),
		hex.EncodeToString(b[6:8]),
		hex.EncodeToString(b[8:10]),
		hex.EncodeToString(b[10:16]),
	)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
