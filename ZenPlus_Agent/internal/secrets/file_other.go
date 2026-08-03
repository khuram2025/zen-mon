//go:build !windows

package secrets

import "os"

func ProtectToFile(path string, plaintext []byte) error {
	return os.WriteFile(path, plaintext, 0o600)
}

func UnprotectFromFile(path string) ([]byte, error) {
	return os.ReadFile(path)
}
