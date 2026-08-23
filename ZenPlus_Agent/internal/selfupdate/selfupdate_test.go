package selfupdate

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
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
