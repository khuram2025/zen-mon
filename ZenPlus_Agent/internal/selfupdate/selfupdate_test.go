package selfupdate

import (
	"context"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"zenplus-agent/internal/client"
)

func TestPreparePackageRejectsFailuresAndCleansStaging(t *testing.T) {
	body := []byte("unsigned installer content")
	digest := fmt.Sprintf("%x", sha256.Sum256(body))
	for _, tc := range []struct {
		name       string
		status     int
		size       int64
		hash, want string
	}{
		{"HTTP error", 503, int64(len(body)), digest, "503"},
		{"truncated download", 200, int64(len(body) + 1), digest, "size mismatch"},
		{"body longer than declared", 200, int64(len(body) - 1), digest, "exceeds the maximum allowed size"},
		{"missing size", 200, 0, digest, "outside the accepted range"},
		{"oversized manifest", 200, MaxPackageBytes + 1, digest, "outside the accepted range"},
		{"wrong checksum", 200, int64(len(body)), strings.Repeat("a", 64), "checksum mismatch"},
		{"unsigned installer", 200, int64(len(body)), digest, "signature verification failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.name == "unsigned installer" && runtime.GOOS != "windows" {
				t.Skip("Windows signature policy")
			}
			root := t.TempDir()
			t.Setenv("TMP", root)
			t.Setenv("TEMP", root)
			t.Setenv("TMPDIR", root)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write(body)
			}))
			defer server.Close()
			api, err := client.New(server.URL, "", true, "", "")
			if err != nil {
				t.Fatal(err)
			}
			path, err := PreparePackage(context.Background(), api, Manifest{LatestVersion: "1.12.7", FileName: "update.exe", FileSize: tc.size, SHA256: tc.hash, DownloadURL: server.URL + "/update.exe"})
			if err == nil || !strings.Contains(err.Error(), tc.want) || path != "" {
				t.Fatalf("path=%q error=%v; want %q", path, err, tc.want)
			}
			entries, err := os.ReadDir(root)
			if err != nil {
				t.Fatal(err)
			}
			if len(entries) != 0 {
				t.Fatalf("failed download left staging files: %v", entries)
			}
		})
	}
}

func TestPreparePackageSignedDownloadIntegration(t *testing.T) {
	paths := os.Getenv("ZENPLUS_SIGNED_TEST_PACKAGES")
	if paths == "" {
		t.Skip("supply signed release fixtures")
	}
	source := filepath.SplitList(paths)[0]
	info, err := os.Stat(source)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := sha256File(source)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.ServeFile(w, r, source) }))
	defer server.Close()
	api, err := client.New(server.URL, "", true, "", "")
	if err != nil {
		t.Fatal(err)
	}
	path, err := PreparePackage(context.Background(), api, Manifest{LatestVersion: "1.12.7", FileName: "release.exe", FileSize: info.Size(), SHA256: digest, DownloadURL: server.URL + "/release.exe"})
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(filepath.Dir(path))
	got, err := sha256File(path)
	if err != nil || got != digest {
		t.Fatalf("prepared package mismatch: %s, %v", got, err)
	}
}

func TestPublicUpdateDownloadIntegration(t *testing.T) {
	if os.Getenv("ZENPLUS_TEST_PUBLIC_UPDATE") != "1" {
		t.Skip("enable for live Zentryc release validation")
	}
	api, err := client.New(PublicUpdateBaseURL(), "", true, "", "")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	manifest, err := FetchPublicManifest(ctx, api, "stable", FlavorFull)
	if err != nil {
		t.Fatal(err)
	}
	path, err := PreparePackage(ctx, api, manifest)
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(filepath.Dir(path))
	t.Logf("Public channel %s: downloaded %s, verified size %d, SHA-256 %s and Windows signature", manifest.LatestVersion, manifest.FileName, manifest.FileSize, manifest.SHA256)
}

func TestPreparePackageCancellationCleansStaging(t *testing.T) {
	root := t.TempDir()
	t.Setenv("TMP", root)
	t.Setenv("TEMP", root)
	t.Setenv("TMPDIR", root)
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("partial"))
		w.(http.Flusher).Flush()
		close(started)
		<-r.Context().Done()
	}))
	defer server.Close()
	api, err := client.New(server.URL, "", true, "", "")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go func() { <-started; cancel() }()
	path, err := PreparePackage(ctx, api, Manifest{LatestVersion: "1.12.7", FileName: "update.exe", SHA256: strings.Repeat("a", 64), DownloadURL: server.URL + "/update.exe"})
	if err == nil || path != "" {
		t.Fatalf("cancelled download succeeded: %s, %v", path, err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("cancelled download left staging files: %v", entries)
	}
}

func TestIsNewer(t *testing.T) {
	tests := []struct {
		candidate string
		current   string
		want      bool
	}{
		{"1.4.0", "1.3.2", true},
		{"1.4.0", "1.4.0", false},
		{"1.3.9", "1.4.0", false},
		{"v2.0.0", "1.99.99", true},
		{"1.4.1-beta.1", "1.4.0", true},
	}
	for _, tc := range tests {
		if got := IsNewer(tc.candidate, tc.current); got != tc.want {
			t.Fatalf("IsNewer(%q, %q) = %v, want %v", tc.candidate, tc.current, got, tc.want)
		}
	}
}

func TestApplyRefusesDowngradeBeforeDownloading(t *testing.T) {
	err := Apply(context.Background(), nil, Manifest{LatestVersion: "1.11.9"}, "1.12.1", func(string, ...any) {})
	if err == nil {
		t.Fatal("Apply accepted an older published agent")
	}
}

func TestValidatePublicURL(t *testing.T) {
	for _, valid := range []string{
		"https://zentryc.com/downloads/zenplus-agent/stable/a.msi",
		"https://cdn.zentryc.com/a.msi",
	} {
		if err := validatePublicURL(valid); err != nil {
			t.Fatalf("expected %q to be valid: %v", valid, err)
		}
	}
	for _, invalid := range []string{
		"http://zentryc.com/a.msi",
		"https://zentryc.com.evil.example/a.msi",
		"https://example.com/a.msi",
	} {
		if err := validatePublicURL(invalid); err == nil {
			t.Fatalf("expected %q to be rejected", invalid)
		}
	}
}

func TestPackageSignatureRequirementFailsClosedForWindowsAndVendorCDN(t *testing.T) {
	relativeControllerPackage := Manifest{DownloadURL: "/api/v1/agents/packages/windows/latest"}
	wantControllerSignature := runtime.GOOS == "windows"
	if got := requiresPackageSignature(relativeControllerPackage); got != wantControllerSignature {
		t.Fatalf("relative controller package signature requirement = %v, want %v on %s", got, wantControllerSignature, runtime.GOOS)
	}
	if !requiresPackageSignature(Manifest{DownloadURL: "https://cdn.zentryc.com/agent.msi"}) {
		t.Fatal("vendor CDN subdomain package did not require a signature")
	}
	if !requiresPackageSignature(Manifest{DownloadURL: "https://example.com/agent.msi", RequiresAuthenticode: true}) {
		t.Fatal("explicit Authenticode requirement was ignored")
	}
}

func TestNormalizeManifestRejectsPackageFilePathTraversal(t *testing.T) {
	for _, fileName := range []string{
		`..\outside.exe`, "../outside.exe", `C:\outside.exe`, "/outside.exe", "subdir/agent.exe", "agent.exe:stream",
	} {
		m := Manifest{
			LatestVersion: "1.12.4",
			FileName:      fileName,
			SHA256:        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			DownloadURL:   "/api/v1/agents/packages/windows/latest",
		}
		if err := normalizeManifest(&m); err == nil {
			t.Errorf("unsafe package file name %q was accepted", fileName)
		}
	}
}

func TestFetchPublishedManifestUsesControllerWithoutInternet(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/packages/manifest" {
			t.Fatalf("unexpected request path %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"latest_version":"1.8.0","file_name":"zenplus-agent-1.8.0.exe","file_size":123,"sha256":"%s","released_at":"%s","download_url":"/api/v1/agents/packages/windows/latest"}`,
			"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", time.Now().UTC().Format(time.RFC3339))
	}))
	defer server.Close()

	api, err := client.New(server.URL, "", true, "agent", "key")
	if err != nil {
		t.Fatal(err)
	}
	previous := publicUpdateBaseURL
	publicUpdateBaseURL = "http://127.0.0.1:1/unreachable"
	defer func() { publicUpdateBaseURL = previous }()

	manifest, err := FetchPublishedManifest(context.Background(), api, "stable", FlavorFull)
	if err != nil {
		t.Fatal(err)
	}
	if manifest.LatestVersion != "1.8.0" {
		t.Fatalf("unexpected controller manifest: %+v", manifest)
	}
}
