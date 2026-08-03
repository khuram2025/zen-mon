package agent

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"zenplus-agent/internal/backoff"
	"zenplus-agent/internal/client"
	"zenplus-agent/internal/collectors"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/configpoller"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/netcapture"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/selfupdate"
	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

// supervisor gates all controller traffic: exponential backoff with jitter
// during outages, a hard stop on rejected credentials (with automatic
// re-enrollment when a token is available), and controller backpressure.
type supervisor struct {
	comm            *backoff.Backoff
	enrollBO        *backoff.Backoff
	notBefore       time.Time // no controller traffic before this
	enrollNotBefore time.Time // no enrollment attempt before this
	authFailed      bool
	lastSkewLogged  time.Duration
}

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
	ControllerURL   string `json:"controller_url"`
	EnrollmentToken string `json:"enrollment_token"`
	SiteID          string `json:"site_id"`
	PolicyID        string `json:"policy_id"`
	ProxyURL        string `json:"proxy_url"`
	VerifyTLS       bool   `json:"verify_tls"`
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

	enrollCtx, cancel := enroll.ContextWithEnrollmentTimeout(ctx)
	enrollment, err := enroll.Ensure(enrollCtx, cfg, paths, log.Printf)
	cancel()
	if err != nil {
		log.Printf("enrollment error: %v", err)
	}
	applyEnrollment(&cfg, enrollment)
	if enrollment.Fresh && cfg.EnrollmentToken != "" {
		if err := config.ClearEnrollmentToken(opts.ConfigPath); err != nil {
			log.Printf("unable to clear enrollment token from config: %v", err)
		} else {
			if config.HasEmbeddedEnrollmentToken() {
				log.Printf("enrollment token removed from bootstrap config; this build keeps a compiled-in token as a re-enrollment fallback")
			} else {
				log.Printf("enrollment token cleared from bootstrap config")
			}
			cfg.EnrollmentToken = ""
		}
	}

	up, poller, err := newRuntimeClients(cfg, paths, store, enrollment)
	if err != nil {
		return err
	}
	status := model.Status{
		AgentID:         enrollment.Identity.AgentID,
		ServerID:        enrollment.Identity.ServerID,
		ControllerURL:   cfg.ControllerURL,
		AgentVersion:    model.AgentVersion,
		StartedAt:       time.Now().UTC(),
		CollectorErrors: map[string]string{},
	}

	ctx, stop := signal.NotifyContext(ctx, os.Interrupt)
	defer stop()
	if opts.Duration > 0 {
		var timeout context.CancelFunc
		ctx, timeout = context.WithTimeout(ctx, opts.Duration)
		defer timeout()
	}

	sup := newSupervisor()
	runCollection(ctx, cfg, paths, store, &status, enrollment, log.Printf)
	tickHeartbeat(ctx, opts.ConfigPath, &cfg, paths, store, &up, &poller, &status, &enrollment, sup, log.Printf)
	tickUpload(ctx, store, up, &status, &enrollment, sup, log.Printf)
	syncAuthStatus(&status, enrollment, sup)
	_ = writeStatus(paths.StatusFile, status)
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
	defer collectTicker.Stop()
	defer heartbeatTicker.Stop()
	defer uploadTicker.Stop()
	defer configTicker.Stop()
	defer localConfigTicker.Stop()
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
			tickHeartbeat(ctx, opts.ConfigPath, &cfg, paths, store, &up, &poller, &status, &enrollment, sup, log.Printf)
		case <-uploadTicker.C:
			tickUpload(ctx, store, up, &status, &enrollment, sup, log.Printf)
		case <-configTicker.C:
			if enrollment.Enrolled && sup.mayTalk(time.Now().UTC()) {
				pollConfig(ctx, &cfg, poller, &status, log.Printf)
			}
		case <-localConfigTicker.C:
			rebuilt := refreshLocalRuntime(ctx, opts.ConfigPath, &cfg, paths, store, &enrollment, &up, &poller, &status, &localHash, &authStamp, log.Printf)
			if rebuilt && enrollment.Enrolled && sup.authFailed {
				// Operator supplied new credentials or a token: resume traffic.
				sup.authFailed = false
				sup.enrollBO.Reset()
				sup.onSuccess(&status)
			}
		}
		syncTickers()
		status.ControllerURL = cfg.ControllerURL
		status.AgentID = enrollment.Identity.AgentID
		status.ServerID = enrollment.Identity.ServerID
		syncAuthStatus(&status, enrollment, sup)
		_ = writeStatus(paths.StatusFile, status)
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
		cfg.EnrollmentToken = diskCfg.EnrollmentToken
		cfg.SiteID = diskCfg.SiteID
		cfg.PolicyID = diskCfg.PolicyID
		cfg.ProxyURL = diskCfg.ProxyURL
		cfg.VerifyTLS = diskCfg.VerifyTLS
		if connectionChanged {
			cfg.ConfigETag = ""
			cfg.ConfigVersion = 0
		}
		*localHash = nextLocalHash
		logf("local settings applied: controller=%s site_id=%s policy_id=%s token_configured=%t", cfg.ControllerURL, cfg.SiteID, cfg.PolicyID, cfg.EnrollmentToken != "")
	}

	var (
		enrollCtx context.Context
		cancel    context.CancelFunc
	)
	if cfg.EnrollmentToken != "" {
		enrollCtx, cancel = enroll.ContextWithEnrollmentTimeout(ctx)
	} else {
		enrollCtx, cancel = context.WithTimeout(ctx, 15*time.Second)
	}
	nextEnrollment, err := enroll.Ensure(enrollCtx, *cfg, paths, logf)
	cancel()
	if err != nil {
		status.LastConfigError = "local enrollment refresh failed: " + err.Error()
		logf("%s", status.LastConfigError)
		return false
	}
	if nextEnrollment.Fresh && cfg.EnrollmentToken != "" {
		if err := config.ClearEnrollmentToken(configPath); err != nil {
			logf("unable to clear enrollment token from config: %v", err)
		} else {
			cfg.EnrollmentToken = ""
			*localHash = localSettingsFingerprint(*cfg)
		}
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
	return writeStatus(paths.StatusFile, status)
}

func EnrollNow(ctx context.Context, configPath string, token string) (enroll.Result, error) {
	cfg, err := config.Load(configPath)
	if err != nil {
		return enroll.Result{}, err
	}
	if token != "" {
		cfg.EnrollmentToken = strings.TrimSpace(token)
		if err := cfg.Validate(); err != nil {
			return enroll.Result{}, err
		}
	}
	if cfg.EnrollmentToken == "" {
		return enroll.Result{}, fmt.Errorf("enrollment_token is required; pass --token or set enrollment_token in %s", configPath)
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
		return enrollment, fmt.Errorf("enrollment was not accepted by the controller")
	}
	if enrollment.Fresh {
		if err := config.ClearEnrollmentToken(configPath); err != nil {
			return enrollment, err
		}
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
			"last_error":      firstNonEmpty(status.LastUploadError, status.LastHeartbeatError, status.LastConfigError),
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

// tickHeartbeat drives the heartbeat cadence. When the agent has no valid
// credentials (never enrolled, or the controller rejected the key) it does
// not touch the controller at all except for a backoff-gated enrollment
// attempt — no more unauthenticated 401 storms.
func tickHeartbeat(ctx context.Context, configPath string, cfg *config.Config, paths runtime.Paths, store *spool.Store, up **uploader.Uploader, poller **configpoller.Poller, status *model.Status, enrollment *enroll.Result, sup *supervisor, logf func(string, ...any)) {
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
	handleHeartbeatResponse(ctx, hb, cfg, *poller, paths, store, *up, status, *enrollment, sup, logf)
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

// tryRecoverAuth attempts (re-)enrollment, gated by exponential backoff.
func tryRecoverAuth(ctx context.Context, configPath string, cfg *config.Config, paths runtime.Paths, store *spool.Store, up **uploader.Uploader, poller **configpoller.Poller, status *model.Status, enrollment *enroll.Result, sup *supervisor, logf func(string, ...any)) {
	now := time.Now().UTC()
	if now.Before(sup.enrollNotBefore) {
		return
	}
	if cfg.EnrollmentToken == "" {
		// Nothing to enroll with; wait quietly until an operator supplies a
		// token or fresh credentials (picked up by the local config watcher).
		wait := sup.enrollBO.Next()
		sup.enrollNotBefore = now.Add(wait)
		if sup.enrollBO.Attempts() == 1 {
			logf("no credentials and no enrollment token; agent will spool locally until re-enrolled")
		}
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
	if err != nil || !res.Enrolled {
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
	if res.Fresh && cfg.EnrollmentToken != "" {
		if err := config.ClearEnrollmentToken(configPath); err != nil {
			logf("unable to clear enrollment token from config: %v", err)
		}
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
	sup.authFailed = false
	sup.enrollBO.Reset()
	sup.enrollNotBefore = time.Time{}
	sup.onSuccess(status)
	status.LastHeartbeatError = ""
	status.AgentID = res.Identity.AgentID
	status.ServerID = res.Identity.ServerID
	logf("enrollment recovered: agent_id=%s server_id=%s", res.Identity.AgentID, res.Identity.ServerID)
}

func syncAuthStatus(status *model.Status, enrollment enroll.Result, sup *supervisor) {
	status.Enrolled = enrollment.Enrolled
	switch {
	case sup.authFailed:
		status.AuthState = "unauthorized"
	case !enrollment.Enrolled:
		status.AuthState = "unenrolled"
	default:
		status.AuthState = "ok"
	}
}

func sendHeartbeat(ctx context.Context, cfg config.Config, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, sup *supervisor, logf func(string, ...any)) (*model.HeartbeatResponse, error) {
	st, _ := store.Stats()
	now := time.Now().UTC()
	hb := model.Heartbeat{
		Version:          model.AgentVersion,
		UptimeSeconds:    uptimeSeconds(enrollment.Identity.BootTime, now),
		QueueDepth:       st.Depth,
		SpoolBytes:       st.Bytes,
		ConfigHash:       config.Hash(cfg),
		ConfigApplyError: status.LastConfigError,
	}
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

func handleHeartbeatResponse(ctx context.Context, hb *model.HeartbeatResponse, cfg *config.Config, poller *configpoller.Poller, paths runtime.Paths, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, sup *supervisor, logf func(string, ...any)) {
	sup.applyBackpressure(hb.Backpressure, status, logf)
	if hb.ConfigETag != "" && hb.ConfigETag != cfg.ConfigETag {
		pollConfig(ctx, cfg, poller, status, logf)
	}
	if hb.DesiredVersion != nil && *hb.DesiredVersion != "" && *hb.DesiredVersion != model.AgentVersion {
		logf("controller requested desired_version=%s (running %s); starting self-update", *hb.DesiredVersion, model.AgentVersion)
		status.UpgradeState = "requested " + *hb.DesiredVersion
		startSelfUpdate(ctx, up, cfg.UpdateRing, logf)
	}
	if hb.HasCommands {
		handleCommands(ctx, cfg, poller, paths, store, up, status, enrollment, logf)
	}
}

// startSelfUpdate checks the controller's package manifest and, when a newer
// version is published, downloads + verifies + installs it in the background.
// selfupdate itself serializes attempts and enforces a per-version cooldown.
func startSelfUpdate(ctx context.Context, up *uploader.Uploader, channel string, logf func(string, ...any)) {
	go func() {
		m, err := selfupdate.FetchManifest(ctx, up.Client(), channel)
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

func handleCommands(ctx context.Context, cfg *config.Config, poller *configpoller.Poller, paths runtime.Paths, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, logf func(string, ...any)) {
	commands, err := up.PollCommands(ctx)
	if err != nil {
		logf("command poll failed: %v", err)
		return
	}
	for _, cmd := range commands {
		result := executeCommand(ctx, cmd, cfg, poller, paths, store, up, status, enrollment, logf)
		if err := up.SendCommandResult(ctx, cmd.ID, result); err != nil {
			logf("command result failed command_id=%s: %v", cmd.ID, err)
		}
	}
}

func executeCommand(ctx context.Context, cmd model.Command, cfg *config.Config, poller *configpoller.Poller, paths runtime.Paths, store *spool.Store, up *uploader.Uploader, status *model.Status, enrollment enroll.Result, logf func(string, ...any)) model.CommandResult {
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
		if !captureMu.TryLock() {
			return model.CommandResult{Success: false,
				ErrorMessage: "a network capture is already running on this host"}
		}
		opts := netcapture.Options{
			Duration:       time.Duration(paramInt(cmd.Params, "duration_s", 300)) * time.Second,
			SampleInterval: time.Duration(paramInt(cmd.Params, "sample_interval_s", 2)) * time.Second,
			MaxFlows:       paramInt(cmd.Params, "max_flows", 5000),
		}
		if iface, ok := cmd.Params["interface"].(string); ok {
			opts.Interface = iface
		}
		// The capture outlives this command result: the controller marks the
		// command succeeded once the run starts, then follows progress through
		// the streamed uploads.
		go func() {
			defer captureMu.Unlock()
			runNetworkCapture(context.Background(), captureID, opts, up, logf)
		}()
		return model.CommandResult{Success: true, Output: map[string]any{
			"capture_id": captureID,
			"duration_s": int(opts.Duration.Seconds()),
			"interface":  opts.Interface,
			"status":     "running",
		}}
	case "rotate_certificate":
		return model.CommandResult{Success: false, ErrorMessage: "rotate_certificate is reserved for the future mTLS contract and is not enabled in this build"}
	case "restart_agent":
		return model.CommandResult{Success: false, ErrorMessage: "restart_agent requires service-manager integration and is not enabled in this foreground command handler"}
	case "upgrade_agent":
		manifest, err := selfupdate.FetchManifest(ctx, up.Client(), cfg.UpdateRing)
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

// Only one capture at a time: concurrent runs would double the sampling cost
// and interleave ESTATS enablement on the same connections.
var captureMu sync.Mutex

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

func toModelFlows(flows []netcapture.Flow) []model.NetworkFlow {
	out := make([]model.NetworkFlow, 0, len(flows))
	for _, f := range flows {
		out = append(out, model.NetworkFlow{
			Protocol: f.Protocol, LocalIP: f.LocalIP, LocalPort: f.LocalPort,
			RemoteIP: f.RemoteIP, RemotePort: f.RemotePort,
			PID: f.PID, ProcessName: f.ProcessName, ServiceName: f.ServiceName,
			State: f.State, BytesSent: f.BytesSent, BytesReceived: f.BytesReceived,
			BytesKnown: f.BytesKnown, FirstSeen: f.FirstSeen, LastSeen: f.LastSeen,
			Samples: f.Samples,
		})
	}
	return out
}

func runNetworkCapture(ctx context.Context, captureID string, opts netcapture.Options, up *uploader.Uploader, logf func(string, ...any)) {
	logf("network capture %s starting: duration=%s interval=%s interface=%q",
		captureID, opts.Duration, opts.SampleInterval, opts.Interface)

	send := func(flows []netcapture.Flow, final bool, st netcapture.Stats, errMsg string) {
		status := "running"
		if final {
			status = "completed"
		}
		if errMsg != "" {
			status = "failed"
		}
		payload := model.NetworkCaptureUpload{
			CaptureID: captureID, Status: status, Interface: opts.Interface,
			StartedAt: st.StartedAt, EndsAt: st.EndsAt, Samples: st.Samples,
			Truncated: st.Truncated, BytesAvailable: st.BytesAvailable,
			Note: st.Note, ErrorMessage: errMsg, Flows: toModelFlows(flows),
		}
		sendCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
		defer cancel()
		if err := up.SendNetworkCapture(sendCtx, payload); err != nil {
			logf("network capture %s upload failed: %v", captureID, err)
		}
	}

	stats, err := netcapture.Run(ctx, opts, func(flows []netcapture.Flow, final bool, st netcapture.Stats) {
		send(flows, final, st, "")
	}, logf)
	if err != nil && !errors.Is(err, context.Canceled) {
		logf("network capture %s failed: %v", captureID, err)
		send(nil, true, stats, err.Error())
		return
	}
	logf("network capture %s finished: %d samples, %d flows", captureID, stats.Samples, stats.FlowCount)
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
	b, _ := json.MarshalIndent(cfg, "", "  ")
	fmt.Println(string(b))
	return nil
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
		ControllerURL:   cfg.ControllerURL,
		EnrollmentToken: strings.TrimSpace(cfg.EnrollmentToken),
		SiteID:          cfg.SiteID,
		PolicyID:        cfg.PolicyID,
		ProxyURL:        cfg.ProxyURL,
		VerifyTLS:       cfg.VerifyTLS,
	})
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func authFileStamp(paths runtime.Paths) string {
	return fileStamp(paths.IdentityFile) + "|" + fileStamp(paths.CredentialFile) + "|" + fileStamp(paths.CredentialMeta)
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
