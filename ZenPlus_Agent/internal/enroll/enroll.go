package enroll

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/secrets"
)

type Result struct {
	Identity                  identity.Identity
	APIKey                    string
	Enrolled                  bool
	Fresh                     bool
	HeartbeatIntervalSeconds  int
	ConfigPollIntervalSeconds int
	UploadIntervalSeconds     int
	PolicyID                  string
}

func Ensure(ctx context.Context, cfg config.Config, paths runtime.Paths, logf func(string, ...any)) (Result, error) {
	id, cloned, err := identity.LoadOrCreate(paths.IdentityFile, cfg.AgentID, cfg.ServerID)
	if err != nil {
		return Result{}, err
	}
	if cloned {
		logf("machine identity changed (cloned VM or golden image); regenerated agent_uid=%s and discarding stale credentials", id.AgentUID)
		_ = os.Remove(paths.CredentialFile)
		_ = os.Remove(paths.CredentialMeta)
	}
	// Prefer stored credentials: enrollment tokens are one-time bootstrap
	// material, so an already-enrolled agent must not burn token uses (or
	// mint a new api key) on every restart.
	if apiKey, err := secrets.UnprotectFromFile(paths.CredentialFile); err == nil && len(apiKey) > 0 {
		return Result{Identity: id, APIKey: string(apiKey), Enrolled: true}, nil
	}
	if cfg.EnrollmentToken != "" {
		return Enroll(ctx, cfg, paths, id, logf)
	}
	logf("no enrollment token configured; running in local spool mode as %s", id.AgentID)
	return Result{Identity: id, Enrolled: false}, nil
}

// Recover discards credentials the controller has rejected and re-enrolls
// with the configured enrollment token. Called by the runtime when uploads
// or heartbeats come back 401/403 so the agent heals itself instead of
// hammering the controller with a dead key.
func Recover(ctx context.Context, cfg config.Config, paths runtime.Paths, logf func(string, ...any)) (Result, error) {
	if cfg.EnrollmentToken == "" {
		id, _, err := identity.LoadOrCreate(paths.IdentityFile, cfg.AgentID, cfg.ServerID)
		if err != nil {
			return Result{}, err
		}
		return Result{Identity: id, Enrolled: false}, fmt.Errorf("credentials rejected and no enrollment token is configured; re-enroll with zenplus-agent enroll --token <token>")
	}
	if err := os.Remove(paths.CredentialFile); err != nil && !errors.Is(err, os.ErrNotExist) {
		return Result{}, err
	}
	if err := os.Remove(paths.CredentialMeta); err != nil && !errors.Is(err, os.ErrNotExist) {
		return Result{}, err
	}
	return Ensure(ctx, cfg, paths, logf)
}

func Enroll(ctx context.Context, cfg config.Config, paths runtime.Paths, id identity.Identity, logf func(string, ...any)) (Result, error) {
	c, err := client.New(cfg.ControllerURL, cfg.ProxyURL, cfg.VerifyTLS, "", "")
	if err != nil {
		return Result{Identity: id}, err
	}
	req := model.EnrollmentRequest{
		EnrollmentToken: cfg.EnrollmentToken,
		AgentUID:        id.AgentUID,
		Hostname:        id.Hostname,
		Platform:        id.Platform,
		Version:         model.AgentVersion,
		SiteID:          cfg.SiteID,
		PolicyID:        cfg.PolicyID,
		FQDN:            id.FQDN,
		PrimaryIP:       id.PrimaryIP,
		OSName:          id.OSName,
		OSVersion:       id.OSVersion,
		KernelOrBuild:   id.KernelOrBuild,
		Architecture:    id.Architecture,
	}
	var resp model.EnrollmentResponse
	httpResp, body, err := c.PostJSON(ctx, "/api/v1/agents/enroll", req, &resp)
	if err != nil {
		if httpResp != nil && httpResp.StatusCode == http.StatusNotFound {
			logf("enrollment deferred: controller does not currently accept Windows agent enrollment (%s)", httpResp.Status)
			return Result{Identity: id, Enrolled: false}, nil
		}
		if httpResp != nil && (httpResp.StatusCode == http.StatusUnauthorized || httpResp.StatusCode == http.StatusForbidden) {
			return Result{Identity: id}, fmt.Errorf("enrollment token rejected by controller (%s)", httpResp.Status)
		}
		return Result{Identity: id}, err
	}
	if resp.AgentID == "" || resp.ServerID == "" || resp.APIKey == "" {
		return Result{Identity: id}, fmt.Errorf("invalid enrollment response: %s", safeJSON(body))
	}
	id.AgentID = resp.AgentID
	id.ServerID = resp.ServerID
	if err := identity.Save(paths.IdentityFile, id); err != nil {
		return Result{Identity: id}, err
	}
	if err := secrets.ProtectToFile(paths.CredentialFile, []byte(resp.APIKey)); err != nil {
		return Result{Identity: id}, err
	}
	if err := secrets.WriteMetadata(paths.CredentialMeta, []byte(resp.APIKey)); err != nil {
		logf("credential metadata write failed: %v", err)
	}
	logf("enrollment succeeded: agent_id=%s server_id=%s", id.AgentID, id.ServerID)
	return Result{
		Identity:                  id,
		APIKey:                    resp.APIKey,
		Enrolled:                  true,
		Fresh:                     true,
		HeartbeatIntervalSeconds:  resp.HeartbeatIntervalSeconds,
		ConfigPollIntervalSeconds: resp.ConfigPollIntervalSeconds,
		UploadIntervalSeconds:     resp.UploadIntervalSeconds,
		PolicyID:                  resp.PolicyID,
	}, nil
}

func Reset(paths runtime.Paths) error {
	errCred := os.Remove(paths.CredentialFile)
	errMeta := os.Remove(paths.CredentialMeta)
	errID := os.Remove(paths.IdentityFile)
	if errCred != nil && !errors.Is(errCred, os.ErrNotExist) {
		return errCred
	}
	if errMeta != nil && !errors.Is(errMeta, os.ErrNotExist) {
		return errMeta
	}
	if errID != nil && !errors.Is(errID, os.ErrNotExist) {
		return errID
	}
	return nil
}

func safeJSON(b []byte) string {
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		if len(b) > 300 {
			return string(b[:300])
		}
		return string(b)
	}
	out, _ := json.Marshal(v)
	if len(out) > 300 {
		return string(out[:300])
	}
	return string(out)
}

func ContextWithEnrollmentTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, 45*time.Second)
}
