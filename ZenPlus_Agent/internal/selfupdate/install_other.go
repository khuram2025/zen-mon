//go:build !windows

package selfupdate

import "fmt"

func launchInstaller(pkgPath string) error {
	return fmt.Errorf("self-update install is not supported on this platform yet (package staged at %s)", pkgPath)
}
