package enroll

import (
	"context"
	"crypto/rand"
	"encoding/base64"
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
	AuthorizationState        string
}

func Ensure(ctx context.Context, cfg config.Config, paths runtime.Paths, logf func(string, ...any)) (Result, error) {
	id, cloned, err := identity.LoadOrCreate(paths.IdentityFile, cfg.AgentID, cfg.ServerID)
	if err != nil {
		return Result{}, err
	}
	if cloned {
		logf("machine identity changed (cloned VM or golden image); regenerated agent_uid=%s and discarding stale registration state", id.AgentUID)
		_ = os.Remove(paths.CredentialFile)
		_ = os.Remove(paths.CredentialMeta)
		_ = os.Remove(paths.PendingSecret)
	}
	// Prefer the appliance-issued durable credential so an already-authorized
	// agent never creates a second identity or rotates its key on restart.
	if apiKey, err := secrets.UnprotectFromFile(paths.CredentialFile); err == nil && len(apiKey) > 0 {
		return Result{Identity: id, APIKey: string(apiKey), Enrolled: true, AuthorizationState: "authorized"}, nil
	}
	pendingSecret, err := ensurePendingSecret(paths.PendingSecret)
	if err != nil {
		return Result{Identity: id, Enrolled: false}, err
	}
	return register(ctx, cfg, paths, id, pendingSecret, logf)
}

// Recover discards credentials the controller has rejected and returns to the
// same protected pending-registration channel. The appliance decides whether
// that installation is pending, authorized, or revoked.
func Recover(ctx context.Context, cfg config.Config, paths runtime.Paths, logf func(string, ...any)) (Result, error) {
	if err := os.Remove(paths.CredentialFile); err != nil && !errors.Is(err, os.ErrNotExist) {
		return Result{}, err
	}
	if err := os.Remove(paths.CredentialMeta); err != nil && !errors.Is(err, os.ErrNotExist) {
		return Result{}, err
	}
	return Ensure(ctx, cfg, paths, logf)
}

func register(ctx context.Context, cfg config.Config, paths runtime.Paths, id identity.Identity, pendingSecret string, logf func(string, ...any)) (Result, error) {
	c, err := client.New(cfg.ControllerURL, cfg.ProxyURL, cfg.VerifyTLS, "", "")
	if err != nil {
		return Result{Identity: id}, err
	}
	req := model.EnrollmentRequest{
		PendingSecret: pendingSecret,
		AgentUID:      id.AgentUID,
		Hostname:      id.Hostname,
		Platform:      id.Platform,
		Version:       model.AgentVersion,
		FQDN:          id.FQDN,
		PrimaryIP:     id.PrimaryIP,
		OSName:        id.OSName,
		OSVersion:     id.OSVersion,
		KernelOrBuild: id.KernelOrBuild,
		Architecture:  id.Architecture,
	}
	var resp model.EnrollmentResponse
	httpResp, body, err := c.PostJSON(ctx, "/api/v1/agents/enroll", req, &resp)
	if err != nil {
		if httpResp != nil && httpResp.StatusCode == http.StatusNotFound {
			logf("enrollment deferred: controller does not currently accept Windows agent enrollment (%s)", httpResp.Status)
			return Result{Identity: id, Enrolled: false}, nil
		}
		if httpResp != nil && (httpResp.StatusCode == http.StatusUnauthorized || httpResp.StatusCode == http.StatusForbidden) {
			return Result{Identity: id}, fmt.Errorf("registration request rejected by controller (%s)", httpResp.Status)
		}
		return Result{Identity: id}, err
	}
	if resp.AgentID == "" {
		return Result{Identity: id}, fmt.Errorf("invalid enrollment response: %s", safeJSON(body))
	}
	id.AgentID = resp.AgentID
	if resp.ServerID != "" {
		id.ServerID = resp.ServerID
	}
	if err := identity.Save(paths.IdentityFile, id); err != nil {
		return Result{Identity: id}, err
	}
	authState := resp.AuthorizationState
	if authState == "" {
		authState = "authorized"
	}
	if authState != "authorized" {
		logf("registration state=%s agent_id=%s; waiting for appliance authorization", authState, id.AgentID)
		return Result{
			Identity: id, Enrolled: false, AuthorizationState: authState,
			HeartbeatIntervalSeconds:  resp.HeartbeatIntervalSeconds,
			ConfigPollIntervalSeconds: resp.ConfigPollIntervalSeconds,
			UploadIntervalSeconds:     resp.UploadIntervalSeconds,
			PolicyID:                  resp.PolicyID,
		}, nil
	}
	if resp.ServerID == "" || resp.APIKey == "" {
		return Result{Identity: id}, fmt.Errorf("invalid authorized enrollment response: %s", safeJSON(body))
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
		AuthorizationState:        "authorized",
	}, nil
}

func ensurePendingSecret(path string) (string, error) {
	if protected, err := secrets.UnprotectFromFile(path); err == nil {
		if value := string(protected); len(value) >= 32 {
			return value, nil
		}
	}
	random := make([]byte, 32)
	if _, err := rand.Read(random); err != nil {
		return "", fmt.Errorf("generate pending registration secret: %w", err)
	}
	value := "zpa_pending_" + base64.RawURLEncoding.EncodeToString(random)
	if err := secrets.ProtectToFile(path, []byte(value)); err != nil {
		return "", fmt.Errorf("protect pending registration secret: %w", err)
	}
	return value, nil
}

func Reset(paths runtime.Paths) error {
	errCred := os.Remove(paths.CredentialFile)
	errMeta := os.Remove(paths.CredentialMeta)
	errID := os.Remove(paths.IdentityFile)
	errPending := os.Remove(paths.PendingSecret)
	if errCred != nil && !errors.Is(errCred, os.ErrNotExist) {
		return errCred
	}
	if errMeta != nil && !errors.Is(errMeta, os.ErrNotExist) {
		return errMeta
	}
	if errID != nil && !errors.Is(errID, os.ErrNotExist) {
		return errID
	}
	if errPending != nil && !errors.Is(errPending, os.ErrNotExist) {
		return errPending
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
