package runtime

import (
	"os"
	"path/filepath"
)

type Paths struct {
	DataDir        string
	StateDir       string
	SpoolDB        string
	LogDir         string
	LogFile        string
	DiagDir        string
	ConfigCache    string
	IdentityFile   string
	StatusFile     string
	CredentialFile string
	CredentialMeta string
}

func NewPaths(dataDir string) Paths {
	if dataDir == "" {
		dataDir = "data"
	}
	state := filepath.Join(dataDir, "state")
	return Paths{
		DataDir:        dataDir,
		StateDir:       state,
		SpoolDB:        filepath.Join(state, "spool.db"),
		LogDir:         filepath.Join(dataDir, "logs"),
		LogFile:        filepath.Join(dataDir, "logs", "agent.log"),
		DiagDir:        filepath.Join(dataDir, "diag"),
		ConfigCache:    filepath.Join(dataDir, "config", "last-known-good.yaml"),
		IdentityFile:   filepath.Join(state, "identity.json"),
		StatusFile:     filepath.Join(state, "status.json"),
		CredentialFile: filepath.Join(state, "credential.dpapi"),
		CredentialMeta: filepath.Join(state, "credential.json"),
	}
}

func (p Paths) Ensure() error {
	for _, dir := range []string{
		p.DataDir,
		p.StateDir,
		filepath.Dir(p.SpoolDB),
		p.LogDir,
		p.DiagDir,
		filepath.Dir(p.ConfigCache),
	} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return err
		}
	}
	return nil
}
