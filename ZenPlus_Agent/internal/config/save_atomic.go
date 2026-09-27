package config

import (
	"os"
	"path/filepath"
)

// SaveAtomic writes cfg beside path and renames it into place, so the running
// agent never reads half a file. The new file inherits the directory's
// protected permissions.
func SaveAtomic(path string, cfg Config) error {
	if err := cfg.Validate(); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".settings-*.yaml")
	if err != nil {
		return err
	}
	temp := f.Name()
	_ = f.Close()
	defer os.Remove(temp)
	if err := Save(temp, cfg); err != nil {
		return err
	}
	return os.Rename(temp, path)
}

// SetProfileInFile changes the monitoring profile in the configuration file
// and keeps every other setting as written.
func SetProfileInFile(path, profile string) error {
	cfg, err := LoadForEdit(path)
	if err != nil {
		return err
	}
	if err := ApplyProfile(&cfg, profile); err != nil {
		return err
	}
	return SaveAtomic(path, cfg)
}
