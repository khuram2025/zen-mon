package enroll

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
)

func TestEnrollSendsSiteAndPolicy(t *testing.T) {
	var got model.EnrollmentRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/enroll" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		_ = json.NewEncoder(w).Encode(model.EnrollmentResponse{
			AgentID:  "agent-1",
			ServerID: "server-1",
			APIKey:   "zpa_key_AbC123dEf456GhI789jKl012MnO345pQr678",
			PolicyID: "policy-response",
		})
	}))
	defer server.Close()

	paths := runtime.NewPaths(filepath.Join(t.TempDir(), "data"))
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	cfg := config.Default()
	cfg.ControllerURL = server.URL
	cfg.EnrollmentToken = "zp_enroll_test"
	cfg.SiteID = "site-123"
	cfg.PolicyID = "policy-456"
	id := identity.Identity{
		AgentUID:     "win-test-agent",
		Hostname:     "WIN-TEST",
		Platform:     "windows",
		Architecture: "amd64",
	}

	result, err := Enroll(context.Background(), cfg, paths, id, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Enrolled || !result.Fresh {
		t.Fatalf("expected fresh enrollment, got %+v", result)
	}
	if got.SiteID != "site-123" {
		t.Fatalf("site_id not sent: %+v", got)
	}
	if got.PolicyID != "policy-456" {
		t.Fatalf("policy_id not sent: %+v", got)
	}
	if _, err := os.Stat(paths.CredentialMeta); err != nil {
		t.Fatalf("credential metadata was not written: %v", err)
	}
}
