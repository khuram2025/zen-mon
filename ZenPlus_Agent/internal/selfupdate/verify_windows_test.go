//go:build windows

package selfupdate

import (
	"debug/pe"
	"os"
	"path/filepath"
	"strings"
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

// Uses signed binaries already present on the host: the installed ZenPlus
// agent must pass the publisher pin and any other vendor's validly signed EXE
// must fail it, even though Windows trusts both.
func TestVerifyPackageSignaturePinsPublisher(t *testing.T) {
	zenplus := `C:\Program Files\ZenPlus\Agent\zenplus-agent.exe`
	if _, err := os.Stat(zenplus); err == nil {
		if err := verifyPackageSignature(zenplus); err != nil {
			t.Errorf("installed ZenPlus agent rejected: %v", err)
		}
	} else {
		t.Log("no installed ZenPlus agent; skipping the positive case")
	}
	others := []string{
		`C:\Program Files\PuTTY\plink.exe`,
		`C:\Program Files\Git\git-bash.exe`,
		`C:\Program Files\nodejs\node.exe`,
		`C:\Program Files\Mozilla Firefox\firefox.exe`,
		`C:\Program Files\Google\Chrome\Application\chrome.exe`,
	}
	checked := 0
	for _, path := range others {
		if _, err := os.Stat(path); err != nil {
			continue
		}
		if err := verifyPackageSignatureWithPolicy(path, signerPolicy{}); err == nil {
			t.Errorf("%s accepted with an empty publisher policy", path)
		}
		err := verifyPackageSignature(path)
		if err == nil {
			t.Errorf("%s (another publisher) passed the ZenPlus publisher pin", path)
			continue
		}
		if strings.Contains(err.Error(), "does not trust") {
			continue // not validly signed on this host; says nothing about the pin
		}
		if !strings.Contains(err.Error(), "publisher is not trusted") {
			t.Errorf("%s rejected for an unexpected reason: %v", path, err)
		}
		checked++
	}
	if checked == 0 {
		t.Skip("no validly signed third-party executable found on this host")
	}
}
