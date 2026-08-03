package secrets

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type Metadata struct {
	Masked    string    `json:"masked"`
	SHA256    string    `json:"sha256"`
	UpdatedAt time.Time `json:"updated_at"`
}

func NewMetadata(secret []byte) Metadata {
	trimmed := strings.TrimSpace(string(secret))
	sum := sha256.Sum256([]byte(trimmed))
	return Metadata{
		Masked:    Mask(trimmed),
		SHA256:    strings.ToLower(hex.EncodeToString(sum[:])),
		UpdatedAt: time.Now().UTC(),
	}
}

func WriteMetadata(path string, secret []byte) error {
	if path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(NewMetadata(secret), "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o600)
}

func ReadMetadata(path string) (Metadata, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Metadata{}, err
	}
	var meta Metadata
	if err := json.Unmarshal(b, &meta); err != nil {
		return Metadata{}, err
	}
	return meta, nil
}

func (m Metadata) Label() string {
	if m.Masked == "" {
		return ""
	}
	if len(m.SHA256) >= 12 {
		return m.Masked + " (sha256 " + m.SHA256[:12] + ")"
	}
	return m.Masked
}

func Mask(secret string) string {
	secret = strings.TrimSpace(secret)
	if secret == "" {
		return ""
	}
	if len(secret) <= 4 {
		return strings.Repeat("*", len(secret))
	}
	if len(secret) <= 12 {
		return secret[:2] + strings.Repeat("*", len(secret)-4) + secret[len(secret)-2:]
	}
	head := 8
	if len(secret) < head {
		head = len(secret) / 2
	}
	tail := 6
	if len(secret)-head < tail {
		tail = len(secret) - head
	}
	return secret[:head] + strings.Repeat("*", 6) + secret[len(secret)-tail:]
}
