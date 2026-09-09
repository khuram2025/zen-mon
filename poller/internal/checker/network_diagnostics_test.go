package checker

import (
	"context"
	"go.uber.org/zap"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHTTPIPv4DiagnosticsAndIPv6Isolation(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer s.Close()
	sc := &ServiceCheck{TargetURL: s.URL, Timeout: time.Second, HTTPExpectedStatus: 200, Config: map[string]any{"ip_version": "ipv4"}}
	c := NewHTTPChecker(zap.NewNop().Sugar())
	r := c.Check(context.Background(), sc, "test")
	if !r.IsUp || r.Diagnostics == nil || r.Diagnostics.RemoteIP != "127.0.0.1" || r.Diagnostics.ConnectMS == nil || r.Diagnostics.FailureStage != "" {
		t.Fatalf("IPv4 probe: %+v, diagnostics: %+v", r, r.Diagnostics)
	}
	sc.Config["ip_version"] = "ipv6"
	r = c.Check(context.Background(), sc, "test")
	if r.IsUp {
		t.Fatal("IPv6-only probe used IPv4")
	}
}

func TestHTTPSIPv4StillVerifiesCertificate(t *testing.T) {
	s := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer s.Close()
	c := NewHTTPChecker(zap.NewNop().Sugar())
	sc := &ServiceCheck{TargetURL: s.URL, Timeout: time.Second, HTTPExpectedStatus: 200, Config: map[string]any{"ip_version": "ipv4"}}
	r := c.Check(context.Background(), sc, "test")
	if r.IsUp || r.Diagnostics.FailureStage != "tls" || r.Diagnostics.TLSMS == nil {
		t.Fatalf("Untrusted TLS accepted or wrong diagnostics: %+v %+v", r, r.Diagnostics)
	}
	sc.HTTPIgnoreTLSErrors = true
	r = c.Check(context.Background(), sc, "test")
	if !r.IsUp || r.Diagnostics.TLSMS == nil {
		t.Fatalf("Explicit test-only bypass failed: %+v", r)
	}
}

func TestHTTPIPv6Loopback(t *testing.T) {
	l, err := net.Listen("tcp6", "[::1]:0")
	if err != nil {
		t.Skip("IPv6 loopback unavailable")
	}
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	s.Listener.Close()
	s.Listener = l
	s.Start()
	defer s.Close()
	sc := &ServiceCheck{TargetURL: s.URL, Timeout: time.Second, HTTPExpectedStatus: 200, Config: map[string]any{"ip_version": "ipv6"}}
	r := NewHTTPChecker(zap.NewNop().Sugar()).Check(context.Background(), sc, "test")
	if !r.IsUp || r.Diagnostics.RemoteIP != "::1" {
		t.Fatalf("IPv6 probe: %+v %+v", r, r.Diagnostics)
	}
}

func TestIPv4PolicyFollowsRedirects(t *testing.T) {
	var target string
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			http.Redirect(w, r, target+"/ok", 302)
			return
		}
		w.WriteHeader(200)
	}))
	defer s.Close()
	target = s.URL
	sc := &ServiceCheck{TargetURL: strings.Replace(s.URL, "127.0.0.1", "localhost", 1), Timeout: time.Second, HTTPExpectedStatus: 200, HTTPFollowRedirects: true, Config: map[string]any{"ip_version": "ipv4"}}
	r := NewHTTPChecker(zap.NewNop().Sugar()).Check(context.Background(), sc, "test")
	if !r.IsUp || r.Diagnostics.DNSMS == nil || r.Diagnostics.RemoteIP != "127.0.0.1" {
		t.Fatalf("Redirect/hostname probe: %+v %+v", r, r.Diagnostics)
	}
}
