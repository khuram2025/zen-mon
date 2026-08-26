//go:build !windows

package secrets

import (
	"fmt"
	"os"
	"path/filepath"
)

// These helpers preserve the snapshot contract for non-Windows development
// builds. Machine-scope publication is normally disabled there because the
// Windows ProgramData location is unavailable.
func PrepareMachineDashboardDirectory(path string) error {
	info, err := os.Lstat(path)
	if err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("machine dashboard path is not a regular directory: %q", path)
		}
		return os.Chmod(path, 0o755)
	}
	if !os.IsNotExist(err) {
		return err
	}
	return os.MkdirAll(path, 0o755)
}

func ProtectMachineDashboardFile(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("machine dashboard path is not a regular file: %q", path)
	}
	return os.Chmod(path, 0o644)
}

func ValidateMachineDashboardSnapshot(path string) error {
	for _, item := range []struct {
		path      string
		directory bool
	}{
		{path: filepath.Dir(path), directory: true},
		{path: path, directory: false},
	} {
		info, err := os.Lstat(item.path)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || info.IsDir() != item.directory {
			return fmt.Errorf("machine dashboard path has an unexpected type: %q", item.path)
		}
	}
	return nil
}
