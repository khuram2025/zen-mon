package snmp

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"sync"
)

// Credential encryption format matches server/app/core/crypto.py:
//
//	1-byte version || 12-byte nonce || AES-256-GCM(plaintext, tag)
//
// Keyed from the SNMP_ENC_KEY env var (hex, base64, or raw 32 bytes).

const (
	cryptoVersion = 0x01
	nonceLen      = 12
	keyLen        = 32
)

var (
	aeadOnce sync.Once
	aead     cipher.AEAD
	aeadErr  error
)

func loadKey() ([]byte, error) {
	raw := os.Getenv("SNMP_ENC_KEY")
	if raw == "" {
		return nil, errors.New("SNMP_ENC_KEY not set")
	}
	// hex (64 chars)
	if len(raw) == 64 {
		if k, err := hex.DecodeString(raw); err == nil && len(k) == keyLen {
			return k, nil
		}
	}
	// base64 / base64url
	for _, dec := range []func(string) ([]byte, error){
		base64.StdEncoding.DecodeString,
		base64.URLEncoding.DecodeString,
		base64.RawStdEncoding.DecodeString,
		base64.RawURLEncoding.DecodeString,
	} {
		if k, err := dec(raw); err == nil && len(k) == keyLen {
			return k, nil
		}
	}
	// raw
	if len(raw) == keyLen {
		return []byte(raw), nil
	}
	return nil, fmt.Errorf("SNMP_ENC_KEY must decode to %d bytes", keyLen)
}

func getAEAD() (cipher.AEAD, error) {
	aeadOnce.Do(func() {
		key, err := loadKey()
		if err != nil {
			aeadErr = err
			return
		}
		block, err := aes.NewCipher(key)
		if err != nil {
			aeadErr = err
			return
		}
		aead, aeadErr = cipher.NewGCM(block)
	})
	return aead, aeadErr
}

// Decrypt unwraps a ciphertext produced by the FastAPI server.
// Returns empty string for nil/empty input. A decryption error is
// returned if the token is malformed or the key is wrong; callers
// should surface this as a device-level failure, not silently fall
// back to an empty credential.
func Decrypt(token []byte) (string, error) {
	if len(token) == 0 {
		return "", nil
	}
	if len(token) < 1+nonceLen+16 {
		return "", errors.New("ciphertext too short")
	}
	if token[0] != cryptoVersion {
		return "", fmt.Errorf("unsupported ciphertext version %d", token[0])
	}
	a, err := getAEAD()
	if err != nil {
		return "", fmt.Errorf("load snmp key: %w", err)
	}
	nonce := token[1 : 1+nonceLen]
	ct := token[1+nonceLen:]
	pt, err := a.Open(nil, nonce, ct, nil)
	if err != nil {
		return "", fmt.Errorf("snmp decrypt: %w", err)
	}
	return string(pt), nil
}

// CryptoConfigured reports whether SNMP_ENC_KEY is set and decodes.
// Used at startup to fail fast if credentials are stored but the key
// is missing.
func CryptoConfigured() bool {
	_, err := getAEAD()
	return err == nil
}
