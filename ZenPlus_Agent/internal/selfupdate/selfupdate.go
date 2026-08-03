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
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"zenplus-agent/internal/client"
)

type Manifest struct {
	Platform      string    `json:"platform"`
	Channel       string    `json:"channel"`
	Arch          string    `json:"arch"`
	LatestVersion string    `json:"latest_version"`
	FileName      string    `json:"file_name"`
	FileSize      int64     `json:"file_size"`
	SHA256        string    `json:"sha256"`
	ReleasedAt    time.Time `json:"released_at"`
	DownloadURL   string    `json:"download_url"`
}

var (
	mu        sync.Mutex
	inFlight  bool
	attempted = map[string]time.Time{}
)

const retryCooldown = time.Hour

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

// FetchManifest asks the controller which agent version is published.
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
	if m.LatestVersion == "" || m.SHA256 == "" {
		return Manifest{}, fmt.Errorf("controller returned an incomplete package manifest")
	}
	return m, nil
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
