package selfupdate

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"runtime"
	"testing"
	"time"

	"zenplus-agent/internal/client"
)

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

	manifest, err := FetchPublishedManifest(context.Background(), api, "stable")
	if err != nil {
		t.Fatal(err)
	}
	if manifest.LatestVersion != "1.8.0" {
		t.Fatalf("unexpected controller manifest: %+v", manifest)
	}
}
