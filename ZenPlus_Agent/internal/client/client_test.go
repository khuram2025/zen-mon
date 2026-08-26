package client

import (
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestStatusErrorClassification(t *testing.T) {
	cases := []struct {
		code         int
		unauthorized bool
	}{
		{http.StatusUnauthorized, true},
		{http.StatusForbidden, true},
		{http.StatusInternalServerError, false},
		{http.StatusBadGateway, false},
		{http.StatusBadRequest, false},
	}
	for _, tc := range cases {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(tc.code)
			_, _ = w.Write([]byte(`{"detail":"nope"}`))
		}))
		c, err := New(server.URL, "", true, "agent-1", "key-1")
		if err != nil {
			server.Close()
			t.Fatal(err)
		}
		_, _, err = c.PostJSON(context.Background(), "/api/v1/agents/heartbeat", map[string]string{}, nil)
		server.Close()
		if err == nil {
			t.Fatalf("status %d: expected an error", tc.code)
		}
		if got := IsUnauthorized(err); got != tc.unauthorized {
			t.Fatalf("status %d: IsUnauthorized=%v, want %v", tc.code, got, tc.unauthorized)
		}
		if !IsStatus(err, tc.code) {
			t.Fatalf("status %d: IsStatus did not match", tc.code)
		}
	}
}

// A transport-level failure (controller unreachable) must not be mistaken for
// an auth rejection, or the agent would stop instead of backing off.
func TestTransportErrorIsNotUnauthorized(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := server.URL
	server.Close() // nothing is listening now

	c, err := New(url, "", true, "agent-1", "key-1")
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = c.PostJSON(context.Background(), "/api/v1/agents/heartbeat", map[string]string{}, nil)
	if err == nil {
		t.Fatal("expected a transport error")
	}
	if IsUnauthorized(err) {
		t.Fatal("a connection failure must not be classified as unauthorized")
	}
}

func TestResolvePreservesQueryParameters(t *testing.T) {
	c, err := New("https://appliance.example/base", "", true, "agent-1", "key-1")
	if err != nil {
		t.Fatal(err)
	}
	got := c.resolve("/api/v1/agents/packages/manifest?platform=windows&channel=stable")
	want := "https://appliance.example/api/v1/agents/packages/manifest?platform=windows&channel=stable"
	if got != want {
		t.Fatalf("resolve() = %q, want %q", got, want)
	}
}

func TestControllerCABundleAuthenticatesSelfSignedAppliance(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	caFile := filepath.Join(t.TempDir(), "controller-ca.pem")
	certificate := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})
	if err := os.WriteFile(caFile, certificate, 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := NewWithControllerCA(server.URL, "", true, caFile, "agent-1", "key-1")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.GetJSON(context.Background(), "/health", "", nil); err != nil {
		t.Fatalf("self-signed appliance was not authenticated by its explicit CA bundle: %v", err)
	}
}

func TestControllerCABundleRejectsInvalidPEM(t *testing.T) {
	caFile := filepath.Join(t.TempDir(), "controller-ca.pem")
	if err := os.WriteFile(caFile, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewWithControllerCA("https://controller.example", "", true, caFile, "", ""); err == nil {
		t.Fatal("invalid controller CA bundle was accepted")
	}
}

func TestAuthenticationHeadersNeverLeaveControllerAuthority(t *testing.T) {
	type received struct {
		path          string
		authorization string
		agentID       string
	}
	var (
		mu       sync.Mutex
		external []received
	)
	externalServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		external = append(external, received{
			path: r.URL.Path, authorization: r.Header.Get("Authorization"), agentID: r.Header.Get("X-Agent-Id"),
		})
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer externalServer.Close()

	var controller received
	controllerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirect" {
			http.Redirect(w, r, externalServer.URL+"/redirected", http.StatusFound)
			return
		}
		controller = received{
			path: r.URL.Path, authorization: r.Header.Get("Authorization"), agentID: r.Header.Get("X-Agent-Id"),
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer controllerServer.Close()

	c, err := New(controllerServer.URL, "", true, "agent-1", "controller-secret")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.GetJSON(context.Background(), "/relative", "", nil); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.GetJSON(context.Background(), externalServer.URL+"/direct", "", nil); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.GetJSON(context.Background(), "/redirect", "", nil); err != nil {
		t.Fatal(err)
	}
	if err := c.Download(context.Background(), externalServer.URL+"/download", filepath.Join(t.TempDir(), "package.bin")); err != nil {
		t.Fatal(err)
	}

	if controller.authorization != "Bearer controller-secret" || controller.agentID != "agent-1" {
		t.Fatalf("controller request did not carry enrollment auth: %+v", controller)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(external) != 3 {
		t.Fatalf("external request count = %d, want 3", len(external))
	}
	for _, got := range external {
		if got.authorization != "" || got.agentID != "" {
			t.Errorf("request %s leaked controller auth: Authorization=%q X-Agent-Id=%q", got.path, got.authorization, got.agentID)
		}
	}
}
