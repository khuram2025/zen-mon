package selfupdate

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"zenplus-agent/internal/client"
)

const testSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func TestInstalledFlavorFollowsTheAPMBundle(t *testing.T) {
	dir := t.TempDir()
	if got := flavorOfInstallDir(dir); got != FlavorCore {
		t.Fatalf("install without APM = %q, want core", got)
	}
	if err := os.MkdirAll(filepath.Join(dir, "apm"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "apm", "bundle-manifest.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := flavorOfInstallDir(dir); got != FlavorFull {
		t.Fatalf("install with APM bundle = %q, want full", got)
	}
}

func TestSelectFlavor(t *testing.T) {
	full := Manifest{LatestVersion: "1.13.0", FileName: "zenplus-agent-1.13.0.exe"}
	core := Manifest{LatestVersion: "1.13.0", FileName: "zenplus-agent-core-1.13.0.exe"}
	split := full
	split.Flavor = FlavorFull
	split.Flavors = map[string]Manifest{FlavorCore: core, FlavorFull: full}

	for _, tc := range []struct {
		name     string
		manifest Manifest
		flavor   string
		want     string
	}{
		{"legacy manifest serves full", full, FlavorFull, "zenplus-agent-1.13.0.exe"},
		{"empty flavour means full", full, "", "zenplus-agent-1.13.0.exe"},
		{"split manifest serves core", split, FlavorCore, "zenplus-agent-core-1.13.0.exe"},
		{"split manifest serves full", split, FlavorFull, "zenplus-agent-1.13.0.exe"},
		{"controller answered core directly", Manifest{Flavor: FlavorCore, FileName: "zenplus-agent-core-1.13.0.exe"}, FlavorCore, "zenplus-agent-core-1.13.0.exe"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := selectFlavor(tc.manifest, tc.flavor)
			if err != nil {
				t.Fatal(err)
			}
			if got.FileName != tc.want || got.Flavors != nil {
				t.Fatalf("selected %+v, want %s", got, tc.want)
			}
		})
	}

	// A core install must never be handed the full package (or vice versa).
	if _, err := selectFlavor(full, FlavorCore); !errors.Is(err, ErrFlavorUnavailable) {
		t.Fatalf("legacy full manifest offered to a core install: %v", err)
	}
	if _, err := selectFlavor(Manifest{Flavor: FlavorCore}, FlavorFull); !errors.Is(err, ErrFlavorUnavailable) {
		t.Fatalf("core manifest offered to a full install: %v", err)
	}
}

// A controller from before the split ignores the flavor query and returns its
// full package. A core agent must fall back to the public channel's core entry
// instead of installing ~200 MB of APM it does not use.
func TestCoreAgentFallsBackWhenControllerOnlyPublishesFull(t *testing.T) {
	var requested string
	controller := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requested = r.URL.Query().Get("flavor")
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"latest_version":"1.13.0","file_name":"zenplus-agent-1.13.0.exe","file_size":123,"sha256":"%s","download_url":"/api/v1/agents/packages/windows/latest"}`, testSHA)
	}))
	defer controller.Close()
	api, err := client.New(controller.URL, "", true, "agent", "key")
	if err != nil {
		t.Fatal(err)
	}
	previous := publicUpdateBaseURL
	publicUpdateBaseURL = "http://127.0.0.1:1/unreachable"
	defer func() { publicUpdateBaseURL = previous }()

	_, err = FetchPublishedManifest(context.Background(), api, "stable", FlavorCore)
	if requested != FlavorCore {
		t.Fatalf("controller was asked for flavor %q, want core", requested)
	}
	if err == nil || !strings.Contains(err.Error(), "installed flavour") {
		t.Fatalf("core agent accepted a full-only controller manifest: %v", err)
	}

	full, err := FetchManifest(context.Background(), api, "stable", FlavorFull)
	if err != nil || full.FileName != "zenplus-agent-1.13.0.exe" {
		t.Fatalf("full agent could not use the legacy controller manifest: %+v %v", full, err)
	}
}
