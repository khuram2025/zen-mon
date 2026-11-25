package checker

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.uber.org/zap"
)

const hostOverrideVectors = "../../../scripts/dev/host-overrides-contract/vectors.json"

func TestHostOverrideContractVectors(t *testing.T) {
	raw, err := os.ReadFile(hostOverrideVectors)
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Entries []HostOverrideEntry `json:"entries"`
		Places  []struct {
			Place  string            `json:"place"`
			Expect map[string]string `json:"expect"`
		} `json:"places"`
		Lookups []struct {
			Place   string  `json:"place"`
			Host    string  `json:"host"`
			Address *string `json:"address"`
		} `json:"lookups"`
	}
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors.Places) == 0 || len(vectors.Lookups) == 0 {
		t.Fatal("empty contract")
	}
	for _, place := range vectors.Places {
		got := ResolveHostOverrides(vectors.Entries, place.Place)
		if len(got) != len(place.Expect) {
			t.Errorf("%s: got %v, want %v", place.Place, got, place.Expect)
		}
		for host, address := range place.Expect {
			if got[host] != address {
				t.Errorf("%s: %s = %q, want %q", place.Place, host, got[host], address)
			}
		}
	}
	for _, lookup := range vectors.Lookups {
		address, ok := ResolveHostOverrides(vectors.Entries, lookup.Place).Lookup(lookup.Host)
		if lookup.Address == nil {
			if ok {
				t.Errorf("%s at %s: got %s, want no manual address", lookup.Host, lookup.Place, address)
			}
		} else if !ok || address != *lookup.Address {
			t.Errorf("%s at %s: got %q, want %q", lookup.Host, lookup.Place, address, *lookup.Address)
		}
	}
}

func TestResolveHostOverridesSkipsDamagedRows(t *testing.T) {
	got := ResolveHostOverrides([]HostOverrideEntry{
		{Host: "name.test", Address: "other.test"},   // a name is never an address
		{Host: "10.0.0.1", Address: "10.0.0.2"},      // an address is never a name
		{Host: "", Address: "10.0.0.3"},              // no name
		{Host: "zone.test", Address: "fe80::1%eth0"}, // no interface zones
		{Host: "scope.test", Address: "10.0.0.4", Scope: "elsewhere"},
		{Host: "Mapped.Test.", Address: "::ffff:10.0.0.5"},
	}, HostOverrideScopeCentral)
	if len(got) != 1 || got["mapped.test"] != "10.0.0.5" {
		t.Fatalf("unexpected table %v", got)
	}
	if ResolveHostOverrides(nil, HostOverrideScopeCentral) != nil {
		t.Fatal("an empty list must give no table")
	}
}

func TestParseHostOverrideEntriesNeverFailsOnADamagedDocument(t *testing.T) {
	for _, raw := range []string{"", "{}", "[]", `"text"`, `{"entries": "x"}`, `{"entries": null}`, "not json"} {
		if got := ParseHostOverrideEntries([]byte(raw)); len(got) != 0 {
			t.Errorf("%q: expected no entries, got %v", raw, got)
		}
	}
	// Rows of the wrong shape are left out; the good row beside them still applies.
	got := ParseHostOverrideEntries([]byte(`{"entries": ["x", 7, {"host": 123, "address": "10.0.0.1"}, null,
		{"host": "good.test", "address": "10.0.0.2", "scope": "central", "note": "kept", "sensor_id": null}]}`))
	table := ResolveHostOverrides(got, HostOverrideScopeCentral)
	if len(table) != 1 || table["good.test"] != "10.0.0.2" {
		t.Fatalf("unexpected table %v from %v", table, got)
	}
}

// overrideTLSServer serves a certificate for service.test and 127.0.0.1 and
// records the Host header and TLS server name of every request.
type overrideTLSServer struct {
	*httptest.Server
	mu    sync.Mutex
	hosts []string
	names []string
	roots *x509.CertPool
	port  string
}

func newOverrideTLSServer(t *testing.T) *overrideTLSServer {
	t.Helper()
	cert, key := testTrustCert(t, "service", false, nil, nil)
	s := &overrideTLSServer{roots: x509.NewCertPool()}
	s.roots.AddCert(cert)
	s.Server = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		s.hosts = append(s.hosts, r.Host)
		s.names = append(s.names, r.TLS.ServerName)
		s.mu.Unlock()
		w.Write([]byte("healthy"))
	}))
	s.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{cert.Raw}, PrivateKey: key}}}
	s.StartTLS()
	t.Cleanup(s.Close)
	parsed, _ := url.Parse(s.URL)
	s.port = parsed.Port()
	return s
}

func (s *overrideTLSServer) seen() (hosts, names []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.hosts...), append([]string(nil), s.names...)
}

func TestHTTPCheckConnectsToManualAddressAndKeepsTheName(t *testing.T) {
	srv := newOverrideTLSServer(t)
	c := NewHTTPChecker(zap.NewNop().Sugar())
	c.rootCAs = srv.roots
	target := "https://service.test:" + srv.port + "/health"
	for _, workflow := range []bool{false, true} {
		sc := &ServiceCheck{TargetURL: target, HTTPExpectedStatus: 200, Timeout: 2 * time.Second,
			HostOverrides: HostOverrides{"service.test": "127.0.0.1"}}
		if workflow {
			sc.WorkflowSteps = []HTTPWorkflowStep{{URL: target, ExpectedStatuses: "200", ContentMatch: "healthy"}}
		}
		result := c.Check(context.Background(), sc, "test")
		if !result.IsUp {
			t.Fatalf("workflow=%v: %s", workflow, result.Error)
		}
		d := result.Diagnostics
		if d == nil || d.OverrideHost != "service.test" || d.OverrideAddress != "127.0.0.1" || d.RemoteIP != "127.0.0.1" {
			t.Fatalf("workflow=%v: the result does not say a manual address was used: %+v", workflow, d)
		}
		if d.DNSMS != nil {
			t.Fatalf("workflow=%v: a manual address must not be reported as a DNS lookup", workflow)
		}
	}
	hosts, names := srv.seen()
	if len(hosts) != 2 {
		t.Fatalf("expected 2 requests, saw %d", len(hosts))
	}
	for i := range hosts {
		if hosts[i] != "service.test:"+srv.port {
			t.Errorf("Host header %q: the name must be kept", hosts[i])
		}
		if names[i] != "service.test" {
			t.Errorf("TLS server name %q: the name must be kept", names[i])
		}
	}

	// Without the entry the name does not resolve, and nothing is marked.
	plain := &ServiceCheck{TargetURL: target, HTTPExpectedStatus: 200, Timeout: 2 * time.Second}
	result := c.Check(context.Background(), plain, "test")
	if result.IsUp {
		t.Fatal("service.test resolved without a manual address")
	}
	if result.Diagnostics != nil && result.Diagnostics.OverrideAddress != "" {
		t.Fatal("a check without a manual address was marked as using one")
	}
}

func TestHTTPCheckStillVerifiesTheCertificateForTheName(t *testing.T) {
	// The certificate is valid for service.test and for 127.0.0.1. A check of
	// another name sent to 127.0.0.1 must fail: verifying the dialled address
	// instead of the name would wrongly accept it.
	srv := newOverrideTLSServer(t)
	c := NewHTTPChecker(zap.NewNop().Sugar())
	c.rootCAs = srv.roots
	sc := &ServiceCheck{TargetURL: "https://wrong.test:" + srv.port + "/", HTTPExpectedStatus: 200, Timeout: 2 * time.Second,
		HostOverrides: HostOverrides{"wrong.test": "127.0.0.1", "service.test": "127.0.0.1"}}
	result := c.Check(context.Background(), sc, "test")
	if result.IsUp {
		t.Fatal("a certificate for another name was accepted through a manual address")
	}
	if !strings.Contains(result.Error, "wrong.test") {
		t.Fatalf("expected a certificate name error for wrong.test, got %q", result.Error)
	}
	if d := result.Diagnostics; d == nil || d.OverrideAddress != "127.0.0.1" || d.FailureStage != "tls" {
		t.Fatalf("a failed check must still say which address it used: %+v", d)
	}
	// Private trust scoped to the name keeps working through a manual address.
	c.rootCAs = x509.NewCertPool()
	trusted := &ServiceCheck{TargetURL: "https://service.test:" + srv.port + "/", HTTPExpectedStatus: 200, Timeout: 2 * time.Second,
		HostOverrides: HostOverrides{"service.test": "127.0.0.1"},
		ProbeTrust:    &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(srv.Certificate(), "service.test")}}}
	if result := c.Check(context.Background(), trusted, "test"); !result.IsUp {
		t.Fatalf("trust scoped to the name was not applied: %s", result.Error)
	}
	trusted.ProbeTrust = &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(srv.Certificate(), "127.0.0.1")}}
	if result := c.Check(context.Background(), trusted, "test"); result.IsUp {
		t.Fatal("trust scoped to the address was applied to a check of the name")
	}
}

func TestHTTPRedirectToAnotherHostIsLookedUpAgain(t *testing.T) {
	var secondHits atomic.Int32
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		secondHits.Add(1)
		if r.Host == "" || !strings.HasPrefix(r.Host, "second.test:") {
			http.Error(w, "wrong host", http.StatusBadRequest)
			return
		}
		w.Write([]byte("ok"))
	}))
	defer second.Close()
	secondURL, _ := url.Parse(second.URL)
	first := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://second.test:"+secondURL.Port()+"/landing", http.StatusFound)
	}))
	defer first.Close()
	firstURL, _ := url.Parse(first.URL)

	c := NewHTTPChecker(zap.NewNop().Sugar())
	sc := &ServiceCheck{TargetURL: "http://first.test:" + firstURL.Port() + "/", HTTPExpectedStatus: 200, HTTPFollowRedirects: true,
		Timeout: 2 * time.Second, HostOverrides: HostOverrides{"first.test": "127.0.0.1"}}
	result := c.Check(context.Background(), sc, "test")
	if result.IsUp || secondHits.Load() != 0 {
		t.Fatalf("a redirect to a host without a manual address reused the first host's address (up=%v, hits=%d)", result.IsUp, secondHits.Load())
	}
	if !strings.Contains(result.Error, "second.test") {
		t.Fatalf("expected a lookup failure for second.test, got %q", result.Error)
	}

	sc.HostOverrides = HostOverrides{"first.test": "127.0.0.1", "second.test": "127.0.0.1"}
	result = c.Check(context.Background(), sc, "test")
	if !result.IsUp || secondHits.Load() != 1 {
		t.Fatalf("redirect with both hosts mapped: up=%v hits=%d error=%q", result.IsUp, secondHits.Load(), result.Error)
	}
	if d := result.Diagnostics; d == nil || d.OverrideHost != "first.test" {
		t.Fatalf("the result should name the check's own host: %+v", d)
	}
}

func TestHTTPCheckRefusesManualAddressOfTheOtherFamily(t *testing.T) {
	c := NewHTTPChecker(zap.NewNop().Sugar())
	sc := &ServiceCheck{TargetURL: "http://service.test:8080/", HTTPExpectedStatus: 200, Timeout: time.Second,
		Config: map[string]any{"ip_version": "ipv6"}, HostOverrides: HostOverrides{"service.test": "127.0.0.1"}}
	result := c.Check(context.Background(), sc, "test")
	if result.IsUp || !strings.Contains(result.Error, "IPv6 only") {
		t.Fatalf("expected a plain address family error, got up=%v %q", result.IsUp, result.Error)
	}
}

func TestTLSCheckConnectsToManualAddressAndVerifiesTheName(t *testing.T) {
	srv := newOverrideTLSServer(t)
	port, _ := strconv.Atoi(srv.port)
	c := NewTLSChecker(zap.NewNop().Sugar())
	c.rootCAs = srv.roots
	overrides := HostOverrides{"service.test": "127.0.0.1", "wrong.test": "127.0.0.1"}

	result := c.Check(context.Background(), &ServiceCheck{TargetHost: "service.test", TargetPort: port, Timeout: 2 * time.Second, HostOverrides: overrides}, "test")
	if !result.IsUp || result.TLSValid == nil || !*result.TLSValid || result.TLSSubject == "" {
		t.Fatalf("TLS check through a manual address failed: %s", result.Error)
	}
	if d := result.Diagnostics; d == nil || d.OverrideHost != "service.test" || d.OverrideAddress != "127.0.0.1" || d.RemoteIP != "127.0.0.1" {
		t.Fatalf("the result does not say a manual address was used: %+v", d)
	}

	// Same server, another name: the certificate (valid for 127.0.0.1 too)
	// must be rejected for the name the check asked for.
	result = c.Check(context.Background(), &ServiceCheck{TargetHost: "wrong.test", TargetPort: port, Timeout: 2 * time.Second, HostOverrides: overrides}, "test")
	if result.IsUp || result.TLSValid == nil || *result.TLSValid || !strings.Contains(result.Error, "wrong.test") {
		t.Fatalf("a certificate for another name was accepted: up=%v %q", result.IsUp, result.Error)
	}
	if d := result.Diagnostics; d == nil || d.OverrideAddress != "127.0.0.1" || d.RemoteIP != "" {
		t.Fatalf("a failed check must say which address it tried, without a connected address: %+v", d)
	}

	// No entry: the name is resolved as before and nothing is marked.
	result = c.Check(context.Background(), &ServiceCheck{TargetHost: "127.0.0.1", TargetPort: port, Timeout: 2 * time.Second, HostOverrides: overrides}, "test")
	if !result.IsUp || result.Diagnostics != nil {
		t.Fatalf("a check of an address must not use a manual address: up=%v %+v %s", result.IsUp, result.Diagnostics, result.Error)
	}
}

func TestVerifyServiceTLSAtKeepsTheName(t *testing.T) {
	srv := newOverrideTLSServer(t)
	port, _ := strconv.Atoi(srv.port)
	policy := &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(srv.Certificate(), "service.test")}}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	leaf, err := VerifyServiceTLSAt(ctx, "service.test", "127.0.0.1", port, policy)
	if err != nil || leaf == nil {
		t.Fatalf("verification at a manual address failed: %v", err)
	}
	if _, err := VerifyServiceTLSAt(ctx, "wrong.test", "127.0.0.1", port, policy); err == nil {
		t.Fatal("verified another name at a manual address")
	}
}

func TestTCPCheckConnectsToManualAddress(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			conn.Close()
		}
	}()
	port := listener.Addr().(*net.TCPAddr).Port
	c := NewTCPChecker(zap.NewNop().Sugar())

	result := c.Check(context.Background(), &ServiceCheck{TargetHost: "TCP.test.", TargetPort: port, Timeout: 2 * time.Second,
		HostOverrides: HostOverrides{"tcp.test": "127.0.0.1"}}, "test")
	if !result.IsUp {
		t.Fatalf("TCP check through a manual address failed: %s", result.Error)
	}
	if d := result.Diagnostics; d == nil || d.OverrideHost != "tcp.test" || d.OverrideAddress != "127.0.0.1" || d.RemoteIP != "127.0.0.1" {
		t.Fatalf("the result does not say a manual address was used: %+v", d)
	}

	result = c.Check(context.Background(), &ServiceCheck{TargetHost: "tcp.test", TargetPort: port, Timeout: 2 * time.Second}, "test")
	if result.IsUp || result.Diagnostics != nil {
		t.Fatalf("tcp.test connected without a manual address: %+v", result.Diagnostics)
	}
}

func TestICMPCheckPingsManualAddress(t *testing.T) {
	c := NewICMPChecker(zap.NewNop().Sugar())
	// Raw sockets may be unavailable where tests run, so the ping itself may
	// fail; what matters is that the name is not resolved and the result says
	// which address was pinged.
	result := c.Check(context.Background(), &ServiceCheck{TargetHost: "icmp.test", Timeout: time.Second,
		HostOverrides: HostOverrides{"icmp.test": "127.0.0.1"}}, "test")
	if strings.HasPrefix(result.Error, "resolve:") {
		t.Fatalf("the name was resolved despite a manual address: %s", result.Error)
	}
	if d := result.Diagnostics; d == nil || d.OverrideHost != "icmp.test" || d.OverrideAddress != "127.0.0.1" {
		t.Fatalf("the result does not say a manual address was used: %+v", d)
	}
	if result.IsUp && result.Diagnostics.RemoteIP != "127.0.0.1" {
		t.Fatalf("an answered ping should name the address: %+v", result.Diagnostics)
	}

	result = c.Check(context.Background(), &ServiceCheck{TargetHost: "icmp.test", Timeout: time.Second}, "test")
	if result.IsUp || !strings.HasPrefix(result.Error, "resolve:") || result.Diagnostics != nil {
		t.Fatalf("icmp.test without a manual address: up=%v %q %+v", result.IsUp, result.Error, result.Diagnostics)
	}
}

func TestDNSCheckIgnoresManualAddresses(t *testing.T) {
	c := NewDNSChecker(zap.NewNop().Sugar())
	// A name that exists only as a manual address is not in DNS: the DNS check
	// must stay Down, or it would report a working record that does not exist.
	result := c.Check(context.Background(), &ServiceCheck{TargetHost: "only-manual.test", Timeout: 2 * time.Second,
		HostOverrides: HostOverrides{"only-manual.test": "127.0.0.1"}}, "test")
	if result.IsUp {
		t.Fatal("a DNS check answered from a manual address")
	}
	if result.Diagnostics != nil {
		t.Fatalf("a DNS check must never be marked as using a manual address: %+v", result.Diagnostics)
	}

	// A name that does resolve keeps its real answer, not the manual address.
	real := c.Check(context.Background(), &ServiceCheck{TargetHost: "localhost", Timeout: 2 * time.Second,
		Config: map[string]any{"record_type": "A"}}, "test")
	if !real.IsUp {
		t.Skipf("localhost does not resolve here: %s", real.Error)
	}
	manual := c.Check(context.Background(), &ServiceCheck{TargetHost: "localhost", Timeout: 2 * time.Second,
		Config:        map[string]any{"record_type": "A", "expected": "203.0.113.9"},
		HostOverrides: HostOverrides{"localhost": "203.0.113.9"}}, "test")
	if manual.IsUp {
		t.Fatal("a DNS check returned the manual address as the record")
	}
}
