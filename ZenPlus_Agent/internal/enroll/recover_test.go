package enroll

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/secrets"
)

func newPaths(t *testing.T) runtime.Paths {
	t.Helper()
	paths := runtime.NewPaths(filepath.Join(t.TempDir(), "data"))
	if err := paths.Ensure(); err != nil {
		t.Fatal(err)
	}
	return paths
}

// An already-enrolled agent must reuse its stored key rather than spending an
// enrollment-token use on every restart.
func TestEnsurePrefersStoredCredentialOverToken(t *testing.T) {
	var enrollCalls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&enrollCalls, 1)
		_ = json.NewEncoder(w).Encode(model.EnrollmentResponse{
			AgentID: "a", ServerID: "s", APIKey: "zpa_key_new",
		})
	}))
	defer server.Close()

	paths := newPaths(t)
	if err := secrets.ProtectToFile(paths.CredentialFile, []byte("zpa_key_existing")); err != nil {
		t.Fatal(err)
	}
	cfg := config.Default()
	cfg.ControllerURL = server.URL
	cfg.EnrollmentToken = "zpa_enr_token"

	res, err := Ensure(context.Background(), cfg, paths, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Enrolled || res.APIKey != "zpa_key_existing" {
		t.Fatalf("expected the stored credential to be reused, got %+v", res)
	}
	if n := atomic.LoadInt32(&enrollCalls); n != 0 {
		t.Fatalf("enrollment endpoint was called %d time(s) despite valid stored credentials", n)
	}
}

// Recover discards the rejected key and enrols again with the token.
func TestRecoverReplacesRejectedCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(model.EnrollmentResponse{
			AgentID: "agent-2", ServerID: "server-2", APIKey: "zpa_key_fresh",
		})
	}))
	defer server.Close()

	paths := newPaths(t)
	if err := secrets.ProtectToFile(paths.CredentialFile, []byte("zpa_key_revoked")); err != nil {
		t.Fatal(err)
	}
	cfg := config.Default()
	cfg.ControllerURL = server.URL
	cfg.EnrollmentToken = "zpa_enr_token"

	res, err := Recover(context.Background(), cfg, paths, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if res.APIKey != "zpa_key_fresh" {
		t.Fatalf("expected a freshly issued key, got %q", res.APIKey)
	}
	stored, err := secrets.UnprotectFromFile(paths.CredentialFile)
	if err != nil || string(stored) != "zpa_key_fresh" {
		t.Fatalf("new credential was not persisted: %q (%v)", stored, err)
	}
}

// With no token there is nothing to re-enrol with; Recover must report that
// clearly instead of leaving the agent retrying a dead key.
func TestRecoverWithoutTokenReportsUnenrolled(t *testing.T) {
	paths := newPaths(t)
	cfg := config.Default()
	cfg.ControllerURL = "http://127.0.0.1:1"
	cfg.EnrollmentToken = ""

	res, err := Recover(context.Background(), cfg, paths, func(string, ...any) {})
	if err == nil {
		t.Fatal("expected an error explaining that re-enrollment is impossible")
	}
	if res.Enrolled {
		t.Fatal("agent must not report itself enrolled")
	}
}

// A cloned VM / restored golden image must not keep the source host's
// credentials, or both hosts fight over one identity.
func TestClonedMachineDropsInheritedCredentials(t *testing.T) {
	paths := newPaths(t)
	if err := secrets.ProtectToFile(paths.CredentialFile, []byte("zpa_key_from_image")); err != nil {
		t.Fatal(err)
	}
	// Identity captured on a different machine.
	stored := `{"agent_uid":"win-old","agent_id":"agt_old","server_id":"srv_old",
	            "machine_guid":"00000000-0000-0000-0000-00000000dead",
	            "hostname":"GOLDEN-IMAGE","platform":"windows","architecture":"amd64"}`
	if err := os.WriteFile(paths.IdentityFile, []byte(stored), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg := config.Default()
	cfg.ControllerURL = "http://127.0.0.1:1"
	cfg.EnrollmentToken = ""

	res, _ := Ensure(context.Background(), cfg, paths, func(string, ...any) {})
	if res.Identity.AgentUID == "win-old" {
		t.Fatal("cloned host kept the golden image's agent_uid")
	}
	if res.Enrolled {
		t.Fatal("cloned host kept the golden image's credentials")
	}
	if _, err := os.Stat(paths.CredentialFile); !os.IsNotExist(err) {
		t.Fatal("inherited credential file was not removed on clone detection")
	}
}
