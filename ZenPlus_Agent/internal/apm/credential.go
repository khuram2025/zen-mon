package apm

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	agentruntime "zenplus-agent/internal/runtime"
	"zenplus-agent/internal/secrets"
)

const apmCredentialMetadataVersion = 1

// credentialBinding identifies the only agent context in which an APM ingest
// credential may be reused. A credential minted for one controller, identity,
// server, or environment must never be carried into another context.
type credentialBinding struct {
	ControllerURL string
	AgentID       string
	ServerID      string
	Environment   string
}

type credentialMetadata struct {
	Version           int       `json:"version"`
	ControllerURL     string    `json:"controller_url"`
	AgentID           string    `json:"agent_id"`
	ServerID          string    `json:"server_id"`
	Environment       string    `json:"environment"`
	KeyID             string    `json:"key_id"`
	BindingHMACSHA256 string    `json:"binding_hmac_sha256"`
	UpdatedAt         time.Time `json:"updated_at"`
}

type credentialMaterial struct {
	Key         string
	KeyID       string
	Fingerprint string
}

type credentialMACPayload struct {
	Version       int    `json:"version"`
	ControllerURL string `json:"controller_url"`
	AgentID       string `json:"agent_id"`
	ServerID      string `json:"server_id"`
	Environment   string `json:"environment"`
	KeyID         string `json:"key_id"`
}

func newCredentialBinding(controllerURL, agentID, serverID, environment string) credentialBinding {
	return credentialBinding{
		ControllerURL: strings.TrimRight(strings.TrimSpace(controllerURL), "/"),
		AgentID:       strings.TrimSpace(agentID),
		ServerID:      strings.TrimSpace(serverID),
		Environment:   strings.TrimSpace(environment),
	}
}

func (b credentialBinding) validate() error {
	switch {
	case b.ControllerURL == "":
		return fmt.Errorf("APM credential binding is missing the controller URL")
	case b.AgentID == "":
		return fmt.Errorf("APM credential binding is missing the agent ID")
	case b.ServerID == "":
		return fmt.Errorf("APM credential binding is missing the server ID")
	case b.Environment == "":
		return fmt.Errorf("APM credential binding is missing the environment")
	default:
		return nil
	}
}

func newCredentialMetadata(binding credentialBinding, keyID, key string) credentialMetadata {
	meta := credentialMetadata{
		Version:       apmCredentialMetadataVersion,
		ControllerURL: binding.ControllerURL,
		AgentID:       binding.AgentID,
		ServerID:      binding.ServerID,
		Environment:   binding.Environment,
		KeyID:         strings.TrimSpace(keyID),
		UpdatedAt:     time.Now().UTC(),
	}
	meta.BindingHMACSHA256 = credentialBindingMAC(key, meta)
	return meta
}

func credentialBindingMAC(key string, meta credentialMetadata) string {
	payload, _ := json.Marshal(credentialMACPayload{
		Version:       meta.Version,
		ControllerURL: meta.ControllerURL,
		AgentID:       meta.AgentID,
		ServerID:      meta.ServerID,
		Environment:   meta.Environment,
		KeyID:         meta.KeyID,
	})
	mac := hmac.New(sha256.New, []byte(key))
	_, _ = mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}

func (m credentialMetadata) matches(binding credentialBinding, key string) bool {
	if m.Version != apmCredentialMetadataVersion ||
		m.ControllerURL != binding.ControllerURL ||
		m.AgentID != binding.AgentID ||
		m.ServerID != binding.ServerID ||
		m.Environment != binding.Environment ||
		strings.TrimSpace(m.KeyID) == "" ||
		strings.TrimSpace(m.BindingHMACSHA256) == "" {
		return false
	}
	want, err := hex.DecodeString(credentialBindingMAC(key, m))
	if err != nil {
		return false
	}
	got, err := hex.DecodeString(strings.TrimSpace(m.BindingHMACSHA256))
	return err == nil && hmac.Equal(got, want)
}

func loadBoundCredential(paths agentruntime.Paths, binding credentialBinding) (credentialMaterial, bool) {
	protected, err := secrets.UnprotectFromFile(paths.APMCredential)
	if err != nil || len(protected) == 0 {
		return credentialMaterial{}, false
	}
	key := strings.TrimSpace(string(protected))
	if !strings.HasPrefix(key, "zpi_") {
		return credentialMaterial{}, false
	}
	meta, err := readCredentialMetadata(paths.APMCredentialMeta)
	if err != nil || !meta.matches(binding, key) {
		return credentialMaterial{}, false
	}
	return credentialMaterial{Key: key, KeyID: meta.KeyID, Fingerprint: meta.BindingHMACSHA256}, true
}

func persistBoundCredential(paths agentruntime.Paths, binding credentialBinding, keyID, key string) (credentialMaterial, error) {
	meta := newCredentialMetadata(binding, keyID, key)
	if strings.TrimSpace(meta.KeyID) == "" {
		return credentialMaterial{}, fmt.Errorf("APM credential key ID is empty")
	}
	if err := protectCredentialAtomic(paths.APMCredential, []byte(key)); err != nil {
		return credentialMaterial{}, fmt.Errorf("protect APM credential: %w", err)
	}
	if err := writeCredentialMetadataAtomic(paths.APMCredentialMeta, meta); err != nil {
		return credentialMaterial{}, fmt.Errorf("persist APM credential metadata: %w", err)
	}
	return credentialMaterial{Key: key, KeyID: meta.KeyID, Fingerprint: meta.BindingHMACSHA256}, nil
}

func invalidateBoundCredential(paths agentruntime.Paths) error {
	var failures []error
	for _, path := range []string{paths.APMCredentialMeta, paths.APMCredential} {
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			failures = append(failures, fmt.Errorf("remove %s: %w", filepath.Base(path), err))
		}
	}
	return errors.Join(failures...)
}

func readCredentialMetadata(path string) (credentialMetadata, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return credentialMetadata{}, err
	}
	var meta credentialMetadata
	if err := json.Unmarshal(b, &meta); err != nil {
		return credentialMetadata{}, err
	}
	return meta, nil
}

func writeCredentialMetadataAtomic(path string, meta credentialMetadata) error {
	b, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	return writeAtomic(path, 0o600, func(tempPath string) error {
		return os.WriteFile(tempPath, b, 0o600)
	})
}

func protectCredentialAtomic(path string, plaintext []byte) error {
	return writeAtomic(path, 0o600, func(tempPath string) error {
		return secrets.ProtectToFile(tempPath, plaintext)
	})
}

func writeAtomic(path string, mode os.FileMode, write func(string) error) error {
	if strings.TrimSpace(path) == "" {
		return fmt.Errorf("atomic write path is empty")
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
	if err := temp.Close(); err != nil {
		_ = os.Remove(tempPath)
		return err
	}
	defer os.Remove(tempPath)
	if err := write(tempPath); err != nil {
		return err
	}
	if err := os.Chmod(tempPath, mode); err != nil {
		return err
	}
	file, err := os.OpenFile(tempPath, os.O_RDWR, mode)
	if err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(tempPath, path)
}
