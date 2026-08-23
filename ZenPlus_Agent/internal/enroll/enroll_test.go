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
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
)

func TestEnsureCreatesPendingRegistrationWithoutOperatorToken(t *testing.T) {
	var got model.EnrollmentRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/enroll" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		_ = json.NewEncoder(w).Encode(model.EnrollmentResponse{
			AgentID: "00000000-0000-4000-8000-000000000001", AuthorizationState: "pending",
		})
	}))
	defer server.Close()

	paths := runtime.NewPaths(filepath.Join(t.TempDir(), "data"))
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	cfg := config.Default()
	cfg.ControllerURL = server.URL

	result, err := Ensure(context.Background(), cfg, paths, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if result.Enrolled || result.AuthorizationState != "pending" {
		t.Fatalf("expected pending registration, got %+v", result)
	}
	if result.Identity.AgentID != "00000000-0000-4000-8000-000000000001" {
		t.Fatalf("controller agent id was not persisted: %+v", result.Identity)
	}
	if len(got.PendingSecret) < 32 {
		t.Fatalf("protected continuity secret was not sent: %+v", got)
	}
	if _, err := os.Stat(paths.PendingSecret); err != nil {
		t.Fatalf("pending secret was not persisted: %v", err)
	}
	if _, err := os.Stat(paths.CredentialFile); !os.IsNotExist(err) {
		t.Fatalf("pending registration must not receive an API credential: %v", err)
	}
}
