//go:build !windows

package selfupdate

import "fmt"

func verifyPackageSignature(_ string) error {
	return fmt.Errorf("Authenticode verification is only supported on Windows")
}
