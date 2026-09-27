// Package selfupdate upgrades the agent from the controller's published
// package feed: fetch the manifest, download the package, verify its
// SHA-256, then hand off to the platform installer.
package selfupdate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
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
	// Flavor names the package the fields above describe. Manifests written
	// before the core/full split omit it; those packages all include APM.
	Flavor string `json:"flavor,omitempty"`
	// Flavors lists every package of this release by flavour.
	Flavors map[string]Manifest `json:"flavors,omitempty"`
}

const (
	// FlavorCore is the server-monitoring-only package.
	FlavorCore = "core"
	// FlavorFull adds the offline APM bundle.
	FlavorFull = "full"
)

// ErrFlavorUnavailable reports a manifest that does not publish the flavour
// this installation needs.
var ErrFlavorUnavailable = errors.New("no package of the installed flavour is published")

// InstalledFlavor reports which package flavour the running binaries belong
// to. The full package installs the APM bundle next to the agent.
func InstalledFlavor() string {
	exe, err := os.Executable()
	if err != nil {
		return FlavorFull
	}
	return flavorOfInstallDir(filepath.Dir(exe))
}

func flavorOfInstallDir(dir string) string {
	if _, err := os.Stat(filepath.Join(dir, "apm", "bundle-manifest.json")); err == nil {
		return FlavorFull
	}
	return FlavorCore
}

// selectFlavor returns the package of the requested flavour. Updating across
// flavours would either silently remove APM from a server that uses it or add
// ~200 MB of APM components to one that does not, so a manifest that cannot
// name the right flavour is refused rather than guessed.
func selectFlavor(m Manifest, flavor string) (Manifest, error) {
	if flavor == "" {
		flavor = FlavorFull
	}
	if entry, ok := m.Flavors[flavor]; ok {
		entry.Flavor = flavor
		entry.Flavors = nil
		return entry, nil
	}
	top := m.Flavor
	if top == "" {
		top = FlavorFull // pre-split manifests only published full packages
	}
	if top != flavor {
		return Manifest{}, fmt.Errorf("%w (%s)", ErrFlavorUnavailable, flavor)
	}
	m.Flavor = flavor
	m.Flavors = nil
	return m, nil
}

var (
	mu        sync.Mutex
	inFlight  bool
	attempted = map[string]time.Time{}
)

const retryCooldown = time.Hour

// MaxPackageBytes caps any agent package. The combined APM release is about
// 230 MB; the ceiling leaves headroom without letting a manifest fill a disk.
const MaxPackageBytes int64 = 512 << 20

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
func FetchManifest(ctx context.Context, c *client.Client, channel, flavor string) (Manifest, error) {
	return fetchControllerManifest(ctx, c, channel, flavor, "")
}

// fetchControllerManifest asks for the latest package, or for version when set.
func fetchControllerManifest(ctx context.Context, c *client.Client, channel, flavor, version string) (Manifest, error) {
	if channel == "" {
		channel = "stable"
	}
	if flavor == "" {
		flavor = FlavorFull
	}
	endpoint := fmt.Sprintf("/api/v1/agents/packages/manifest?platform=%s&channel=%s&arch=%s&flavor=%s",
		platformName(), url.QueryEscape(channel), archName(), url.QueryEscape(flavor))
	if version != "" {
		endpoint += "&version=" + url.QueryEscape(version)
	}
	var m Manifest
	if _, _, err := c.GetJSON(ctx, endpoint, "", &m); err != nil {
		return Manifest{}, err
	}
	// Controllers that predate flavours ignore the query and answer with
	// their full package; selectFlavor refuses that answer for core installs.
	m, err := selectFlavor(m, flavor)
	if err != nil {
		return Manifest{}, err
	}
	if err := normalizeManifest(&m); err != nil {
		return Manifest{}, fmt.Errorf("controller returned an incomplete package manifest")
	}
	return m, nil
}

// FetchPublicManifest reads the public, immutable Zentryc update channel.
// Public packages must use HTTPS and remain on zentryc.com.
func FetchPublicManifest(ctx context.Context, c *client.Client, channel, flavor string) (Manifest, error) {
	if channel == "" {
		channel = "stable"
	}
	manifestURL := PublicUpdateBaseURL() + "/" + url.PathEscape(channel) + "/manifest.json"
	var m Manifest
	if _, _, err := c.GetJSON(ctx, manifestURL, "", &m); err != nil {
		return Manifest{}, err
	}
	m, err := selectFlavor(m, flavor)
	if err != nil {
		return Manifest{}, err
	}
	if err := normalizeManifest(&m); err != nil {
		return Manifest{}, fmt.Errorf("public update manifest is invalid: %w", err)
	}
	if err := validatePublicURL(m.DownloadURL); err != nil {
		return Manifest{}, fmt.Errorf("public update manifest is invalid: %w", err)
	}
	if runtime.GOOS == "windows" {
		m.RequiresAuthenticode = true
	}
	return m, nil
}

// FetchPublishedManifest prefers the enrolled appliance. This keeps update
// checks and package downloads fully functional on private networks with no
// internet route. The vendor channel is only a compatibility fallback when an
// older controller does not expose an agent package manifest.
func FetchPublishedManifest(ctx context.Context, c *client.Client, channel, flavor string) (Manifest, error) {
	controllerManifest, controllerErr := FetchManifest(ctx, c, channel, flavor)
	if controllerErr == nil {
		return controllerManifest, nil
	}
	publicManifest, publicErr := FetchPublicManifest(ctx, c, channel, flavor)
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
	fileName := strings.TrimSpace(m.FileName)
	if fileName != m.FileName || fileName == "." || fileName == ".." ||
		filepath.Base(fileName) != fileName || strings.ContainsAny(fileName, `/\:`) {
		return fmt.Errorf("package file name must be a single safe file name")
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
	return ApplyWithLog(ctx, c, m, currentVersion, "", logf)
}

// ApplyWithLog is Apply with the installer writing its log to logPath, so a
// failed upgrade can be diagnosed and reported after the agent restarts.
func ApplyWithLog(ctx context.Context, c *client.Client, m Manifest, currentVersion, logPath string, logf func(string, ...any)) error {
	if m.LatestVersion == currentVersion {
		return nil
	}
	if !IsNewer(m.LatestVersion, currentVersion) {
		return fmt.Errorf("refusing to downgrade agent from %s to %s", currentVersion, m.LatestVersion)
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

	logf("self-update: downloading %s (%s)", m.FileName, m.LatestVersion)
	pkgPath, err := PreparePackage(ctx, c, m)
	if err != nil {
		return err
	}
	logf("self-update: checksum and signature verified; launching installer for %s", m.LatestVersion)
	if err := launchInstaller(pkgPath, logPath); err != nil {
		_ = os.RemoveAll(filepath.Dir(pkgPath))
		return fmt.Errorf("launch installer: %w", err)
	}
	return nil
}

// PreparePackage stages and verifies a package without executing it. Both the
// service and the interactive dashboard use this same verification path.
// The caller owns the returned temporary directory until Setup takes over.
func PreparePackage(ctx context.Context, c *client.Client, m Manifest) (path string, err error) {
	if err := normalizeManifest(&m); err != nil {
		return "", err
	}
	// The declared size bounds the download, so it must be present and sane
	// before a single byte is written to disk.
	if m.FileSize <= 0 || m.FileSize > MaxPackageBytes {
		return "", fmt.Errorf("package size %d is outside the accepted range 1..%d bytes", m.FileSize, MaxPackageBytes)
	}
	stagingDir, err := os.MkdirTemp("", "zenplus-agent-update-")
	if err != nil {
		return "", err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(stagingDir)
		}
	}()
	pkgPath := filepath.Join(stagingDir, m.FileName)
	if err := c.Download(ctx, m.DownloadURL, pkgPath, m.FileSize); err != nil {
		return "", fmt.Errorf("download package: %w", err)
	}
	info, err := os.Stat(pkgPath)
	if err != nil {
		return "", err
	}
	if info.Size() != m.FileSize {
		return "", fmt.Errorf("package size mismatch: manifest %d, downloaded %d", m.FileSize, info.Size())
	}
	sum, err := sha256File(pkgPath)
	if err != nil {
		return "", err
	}
	if !strings.EqualFold(sum, m.SHA256) {
		return "", fmt.Errorf("package checksum mismatch: manifest %s, downloaded %s", m.SHA256, sum)
	}
	if requiresPackageSignature(m) {
		if err := verifyPackageSignature(pkgPath); err != nil {
			return "", fmt.Errorf("package signature verification failed: %w", err)
		}
	}
	return pkgPath, nil
}

func requiresPackageSignature(m Manifest) bool {
	// A controller-provided manifest is authenticated by the enrolled API, but
	// it is not itself a code-signing authority. Fail closed for every Windows
	// installer even when an older appliance omits the manifest flag or serves
	// the package from a relative URL.
	if runtime.GOOS == "windows" || m.RequiresAuthenticode {
		return true
	}
	// Retain signature enforcement for every vendor CDN subdomain, not just the
	// zentryc.com root URL previously recognized by a string prefix.
	return validatePublicURL(m.DownloadURL) == nil
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

// ErrVersionUnavailable reports that the requested version is not published
// in the installed flavour.
var ErrVersionUnavailable = errors.New("the requested agent version is not published")

// FetchPublishedVersion returns the package of exactly version. The appliance
// is asked for that version; the public channel is used only when its
// latest release is that version (older controllers ignore the version
// query and answer with their latest package, which is then rejected).
func FetchPublishedVersion(ctx context.Context, c *client.Client, channel, flavor, version string) (Manifest, error) {
	m, controllerErr := fetchControllerManifest(ctx, c, channel, flavor, version)
	if controllerErr == nil && m.LatestVersion == version {
		return m, nil
	}
	public, publicErr := FetchPublicManifest(ctx, c, channel, flavor)
	if publicErr == nil && public.LatestVersion == version {
		return public, nil
	}
	if controllerErr == nil {
		controllerErr = fmt.Errorf("appliance offered %s", m.LatestVersion)
	}
	return Manifest{}, fmt.Errorf("%w (%s %s): %v", ErrVersionUnavailable, flavor, version, controllerErr)
}
