//go:build windows

package secrets

import (
	"errors"
	"os"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	crypt32                = windows.NewLazySystemDLL("crypt32.dll")
	procCryptProtectData   = crypt32.NewProc("CryptProtectData")
	procCryptUnprotectData = crypt32.NewProc("CryptUnprotectData")
)

type dataBlob struct {
	cbData uint32
	pbData *byte
}

func ProtectToFile(path string, plaintext []byte) error {
	// Encrypt before applying the destination-specific file ACL.
	ciphertext, err := Protect(plaintext)
	if err != nil {
		return err
	}
	if isMachineDataPath(path) {
		return writeMachineSecretFile(path, ciphertext)
	}
	return os.WriteFile(path, ciphertext, 0o600)
}

func UnprotectFromFile(path string) ([]byte, error) {
	ciphertext, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Unprotect(ciphertext)
}

func Protect(plaintext []byte) ([]byte, error) {
	in := bytesToBlob(plaintext)
	var out dataBlob
	r, _, callErr := procCryptProtectData.Call(
		uintptr(unsafe.Pointer(&in)),
		0,
		0,
		0,
		0,
		windows.CRYPTPROTECT_LOCAL_MACHINE,
		uintptr(unsafe.Pointer(&out)),
	)
	if r == 0 {
		if callErr != windows.ERROR_SUCCESS {
			return nil, callErr
		}
		return nil, errors.New("CryptProtectData failed")
	}
	defer windows.LocalFree(windows.Handle(uintptr(unsafe.Pointer(out.pbData))))
	return blobBytes(out), nil
}

func Unprotect(ciphertext []byte) ([]byte, error) {
	in := bytesToBlob(ciphertext)
	var out dataBlob
	r, _, callErr := procCryptUnprotectData.Call(
		uintptr(unsafe.Pointer(&in)),
		0,
		0,
		0,
		0,
		0,
		uintptr(unsafe.Pointer(&out)),
	)
	if r == 0 {
		if callErr != windows.ERROR_SUCCESS {
			return nil, callErr
		}
		return nil, errors.New("CryptUnprotectData failed")
	}
	defer windows.LocalFree(windows.Handle(uintptr(unsafe.Pointer(out.pbData))))
	return blobBytes(out), nil
}

func bytesToBlob(b []byte) dataBlob {
	if len(b) == 0 {
		return dataBlob{}
	}
	return dataBlob{cbData: uint32(len(b)), pbData: &b[0]}
}

func blobBytes(blob dataBlob) []byte {
	if blob.cbData == 0 || blob.pbData == nil {
		return nil
	}
	src := unsafe.Slice(blob.pbData, int(blob.cbData))
	out := make([]byte, len(src))
	copy(out, src)
	return out
}
