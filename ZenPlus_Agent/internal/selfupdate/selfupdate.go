// Package selfupdate upgrades the agent from the controller's published
// package feed: fetch the manifest, download the package, verify its
// SHA-256, then hand off to the platform installer.
package selfupdate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"zenplus-agent/internal/client"
)

type Manifest struct {
	Platform             string    `json:"platform"`
	Channel              string    `json:"channel"`
	Arch                 string    `json:"arch"`
	LatestVersion        string    `json:"latest_version"`
	Version              string    `json:"version,omitempty"`
	FileName             string    `json:"file_name"`
	FileSize             int64     `json:"file_size"`
	SizeBytes            int64     `json:"size_bytes,omitempty"`
	SHA256               string    `json:"sha256"`
	ReleasedAt           time.Time `json:"released_at"`
	BuiltAtUTC           time.Time `json:"built_at_utc,omitempty"`
	DownloadURL          string    `json:"download_url"`
	ReleaseNotesURL      string    `json:"release_notes_url,omitempty"`
	SignatureStatus      string    `json:"signature_status,omitempty"`
	SigningSubject       string    `json:"signing_subject,omitempty"`
	RequiresAuthenticode bool      `json:"requires_authenticode,omitempty"`
}

var (
	mu        sync.Mutex
	inFlight  bool
	attempted = map[string]time.Time{}
)

const retryCooldown = time.Hour

// publicUpdateBaseURL is the vendor-controlled, TLS-only update channel. It
// may be replaced at build time for staging with -X, but enrollment secrets
// are never part of this URL or the public manifest.
var publicUpdateBaseURL = "https://zentryc.com/downloads/zenplus-agent"

func PublicUpdateBaseURL() string {
	return strings.TrimRight(publicUpdateBaseURL, "/")
}

func platformName() string {
	switch runtime.GOOS {
	case "windows":
		return "windows"
	case "darwin":
		return "macos"
	default:
		return "linux"
	}
}

func archName() string {
	if runtime.GOARCH == "arm64" {
		return "arm64"
	}
	return "amd64"
}

// FetchManifest asks the enrolled controller which agent version is published.
// It remains available as a compatibility fallback for private deployments.
func FetchManifest(ctx context.Context, c *client.Client, channel string) (Manifest, error) {
	if channel == "" {
		channel = "stable"
	}
	endpoint := fmt.Sprintf("/api/v1/agents/packages/manifest?platform=%s&channel=%s&arch=%s",
		platformName(), channel, archName())
	var m Manifest
	if _, _, err := c.GetJSON(ctx, endpoint, "", &m); err != nil {
		return Manifest{}, err
	}
	if err := normalizeManifest(&m); err != nil {
		return Manifest{}, fmt.Errorf("controller returned an incomplete package manifest")
	}
	return m, nil
}

// FetchPublicManifest reads the public, immutable Zentryc update channel.
// Public packages must use HTTPS and remain on zentryc.com.
func FetchPublicManifest(ctx context.Context, c *client.Client, channel string) (Manifest, error) {
	if channel == "" {
		channel = "stable"
	}
	manifestURL := PublicUpdateBaseURL() + "/" + url.PathEscape(channel) + "/manifest.json"
	var m Manifest
	if _, _, err := c.GetJSON(ctx, manifestURL, "", &m); err != nil {
		return Manifest{}, err
	}
	if err := normalizeManifest(&m); err != nil {
		return Manifest{}, fmt.Errorf("public update manifest is invalid: %w", err)
	}
	if err := validatePublicURL(m.DownloadURL); err != nil {
		return Manifest{}, fmt.Errorf("public update manifest is invalid: %w", err)
	}
	return m, nil
}

// FetchPublishedManifest prefers the enrolled appliance. This keeps update
// checks and package downloads fully functional on private networks with no
// internet route. The vendor channel is only a compatibility fallback when an
// older controller does not expose an agent package manifest.
func FetchPublishedManifest(ctx context.Context, c *client.Client, channel string) (Manifest, error) {
	controllerManifest, controllerErr := FetchManifest(ctx, c, channel)
	if controllerErr == nil {
		return controllerManifest, nil
	}
	publicManifest, publicErr := FetchPublicManifest(ctx, c, channel)
	if publicErr == nil {
		return publicManifest, nil
	}
	return Manifest{}, fmt.Errorf("controller channel: %v; public fallback: %v", controllerErr, publicErr)
}

func normalizeManifest(m *Manifest) error {
	if m.LatestVersion == "" {
		m.LatestVersion = m.Version
	}
	if m.Version == "" {
		m.Version = m.LatestVersion
	}
	if m.FileSize == 0 {
		m.FileSize = m.SizeBytes
	}
	if m.SizeBytes == 0 {
		m.SizeBytes = m.FileSize
	}
	if m.ReleasedAt.IsZero() {
		m.ReleasedAt = m.BuiltAtUTC
	}
	if m.LatestVersion == "" || m.FileName == "" || len(m.SHA256) != 64 || m.DownloadURL == "" {
		return fmt.Errorf("missing version, file, checksum, or download URL")
	}
	if _, err := hex.DecodeString(m.SHA256); err != nil {
		return fmt.Errorf("checksum is not hexadecimal")
	}
	return nil
}

func validatePublicURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("invalid download URL: %w", err)
	}
	host := strings.ToLower(u.Hostname())
	if u.Scheme != "https" || (host != "zentryc.com" && !strings.HasSuffix(host, ".zentryc.com")) {
		return fmt.Errorf("download URL must use HTTPS on zentryc.com")
	}
	return nil
}

// IsNewer compares numeric dotted versions. Release versions are normalized
// to three components by the build, while extra components remain supported.
func IsNewer(candidate, current string) bool {
	left := numericVersion(candidate)
	right := numericVersion(current)
	count := len(left)
	if len(right) > count {
		count = len(right)
	}
	for i := 0; i < count; i++ {
		var a, b int
		if i < len(left) {
			a = left[i]
		}
		if i < len(right) {
			b = right[i]
		}
		if a != b {
			return a > b
		}
	}
	return false
}

func numericVersion(value string) []int {
	value = strings.TrimPrefix(strings.TrimSpace(value), "v")
	parts := strings.Split(value, ".")
	out := make([]int, 0, len(parts))
	for _, part := range parts {
		digits := part
		if index := strings.IndexAny(digits, "-+"); index >= 0 {
			digits = digits[:index]
		}
		n, _ := strconv.Atoi(digits)
		out = append(out, n)
	}
	return out
}

// Apply downloads the manifest's package, verifies its checksum, and starts
// the platform installer detached (the installer stops/replaces/restarts
// this process). It refuses to re-attempt the same version more often than
// once per hour so a broken package cannot cause an install loop.
func Apply(ctx context.Context, c *client.Client, m Manifest, currentVersion string, logf func(string, ...any)) error {
	if m.LatestVersion == currentVersion {
		return nil
	}
	mu.Lock()
	if inFlight {
		mu.Unlock()
		return fmt.Errorf("an upgrade is already in progress")
	}
	if last, ok := attempted[m.LatestVersion]; ok && time.Since(last) < retryCooldown {
		mu.Unlock()
		return fmt.Errorf("upgrade to %s was already attempted at %s; retry after cooldown", m.LatestVersion, last.Format(time.RFC3339))
	}
	inFlight = true
	attempted[m.LatestVersion] = time.Now()
	mu.Unlock()
	defer func() {
		mu.Lock()
		inFlight = false
		mu.Unlock()
	}()

	download := m.DownloadURL
	if download == "" {
		download = fmt.Sprintf("/api/v1/agents/packages/%s/latest?arch=%s", platformName(), archName())
	}
	stagingDir := filepath.Join(os.TempDir(), "zenplus-agent-update")
	if err := os.MkdirAll(stagingDir, 0o755); err != nil {
		return err
	}
	pkgPath := filepath.Join(stagingDir, m.FileName)
	logf("self-update: downloading %s (%s) from controller", m.FileName, m.LatestVersion)
	if err := c.Download(ctx, download, pkgPath); err != nil {
		return fmt.Errorf("download package: %w", err)
	}
	sum, err := sha256File(pkgPath)
	if err != nil {
		return err
	}
	if !strings.EqualFold(sum, m.SHA256) {
		os.Remove(pkgPath)
		return fmt.Errorf("package checksum mismatch: manifest %s, downloaded %s", m.SHA256, sum)
	}
	if m.RequiresAuthenticode || strings.HasPrefix(strings.ToLower(m.DownloadURL), "https://zentryc.com/") {
		if err := verifyPackageSignature(pkgPath); err != nil {
			os.Remove(pkgPath)
			return fmt.Errorf("package signature verification failed: %w", err)
		}
	}
	logf("self-update: checksum verified; launching installer for %s", m.LatestVersion)
	if err := launchInstaller(pkgPath); err != nil {
		return fmt.Errorf("launch installer: %w", err)
	}
	return nil
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
