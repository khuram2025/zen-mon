//go:build windows

package selfupdate

import (
	"debug/pe"
	"os"
	"path/filepath"
	"testing"
)

func TestVerifyPackageSignatureRejectsUnsignedAndMissing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "unsigned.exe")
	if err := os.WriteFile(path, []byte("not a signed executable"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := verifyPackageSignature(path); err == nil {
		t.Fatal("unsigned package trusted")
	}
	if err := verifyPackageSignature(path + ".missing"); err == nil {
		t.Fatal("missing package trusted")
	}
}

// Release validation supplies the actual Microsoft-signed EXE and MSI. No
// private key or locally trusted test certificate is needed by this test.
func TestVerifyPackageSignatureReleaseIntegration(t *testing.T) {
	paths := os.Getenv("ZENPLUS_SIGNED_TEST_PACKAGES")
	if paths == "" {
		t.Skip("set ZENPLUS_SIGNED_TEST_PACKAGES to signed release EXE/MSI paths")
	}
	for _, source := range filepath.SplitList(paths) {
		t.Run(filepath.Ext(source), func(t *testing.T) {
			if err := verifyPackageSignature(source); err != nil {
				t.Fatalf("signed release rejected: %v", err)
			}
			data, err := os.ReadFile(source)
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(t.TempDir(), "publisher's release with spaces"+filepath.Ext(source))
			if err := os.WriteFile(path, data, 0600); err != nil {
				t.Fatal(err)
			}
			if err := verifyPackageSignature(path); err != nil {
				t.Fatalf("quoted path rejected: %v", err)
			}
			if filepath.Ext(source) == ".exe" {
				binary, err := pe.Open(source)
				if err != nil {
					t.Fatal(err)
				}
				section := binary.Section(".text")
				if section == nil {
					binary.Close()
					t.Fatal("no .text section")
				}
				data[section.Offset+16] ^= 1
				binary.Close()
				if err := os.WriteFile(path, data, 0600); err != nil {
					t.Fatal(err)
				}
				if err := verifyPackageSignature(path); err == nil {
					t.Fatal("tampered signed package trusted")
				}
			}
		})
	}
}
