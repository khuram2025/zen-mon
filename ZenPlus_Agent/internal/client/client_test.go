package client

import (
	"context"
	"net/http"
	"net/http/httptest"
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
