package runtime

import (
	"os"
	"path/filepath"
)

type Paths struct {
	DataDir                 string
	StateDir                string
	SpoolDB                 string
	LogDir                  string
	LogFile                 string
	DiagDir                 string
	ConfigCache             string
	IdentityFile            string
	StatusFile              string
	CredentialFile          string
	CredentialMeta          string
	PendingSecret           string
	APMCredential           string
	APMCredentialMeta       string
	APMDir                  string
	APMConfig               string
	APMStorage              string
	APMLog                  string
	APMInstrumentationState string
}

func NewPaths(dataDir string) Paths {
	if dataDir == "" {
		dataDir = "data"
	}
	state := filepath.Join(dataDir, "state")
	apmDir := filepath.Join(dataDir, "apm")
	return Paths{
		DataDir:                 dataDir,
		StateDir:                state,
		SpoolDB:                 filepath.Join(state, "spool.db"),
		LogDir:                  filepath.Join(dataDir, "logs"),
		LogFile:                 filepath.Join(dataDir, "logs", "agent.log"),
		DiagDir:                 filepath.Join(dataDir, "diag"),
		ConfigCache:             filepath.Join(dataDir, "config", "last-known-good.yaml"),
		IdentityFile:            filepath.Join(state, "identity.json"),
		StatusFile:              filepath.Join(state, "status.json"),
		CredentialFile:          filepath.Join(state, "credential.dpapi"),
		CredentialMeta:          filepath.Join(state, "credential.json"),
		PendingSecret:           filepath.Join(state, "pending-secret.dpapi"),
		APMCredential:           filepath.Join(state, "apm-credential.dpapi"),
		APMCredentialMeta:       filepath.Join(state, "apm-credential.json"),
		APMDir:                  apmDir,
		APMConfig:               filepath.Join(apmDir, "collector.yaml"),
		APMStorage:              filepath.Join(apmDir, "storage"),
		APMLog:                  filepath.Join(dataDir, "logs", "telemetry-gateway.log"),
		APMInstrumentationState: filepath.Join(state, "apm-instrumentation.json"),
	}
}

func (p Paths) Ensure() error {
	for _, dir := range []string{
		p.DataDir,
		p.StateDir,
		filepath.Dir(p.SpoolDB),
		p.LogDir,
		p.DiagDir,
		p.APMDir,
		p.APMStorage,
		filepath.Dir(p.ConfigCache),
	} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return err
		}
	}
	return nil
}
