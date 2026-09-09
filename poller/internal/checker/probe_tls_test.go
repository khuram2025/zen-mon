package checker

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"go.uber.org/zap"
)

func testTrustCert(t *testing.T, name string, ca bool, parent *x509.Certificate, parentKey *ecdsa.PrivateKey) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 100))
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: name}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), BasicConstraintsValid: true, IsCA: ca, KeyUsage: x509.KeyUsageDigitalSignature, DNSNames: []string{"service.test"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	if ca {
		template.KeyUsage |= x509.KeyUsageCertSign
		template.ExtKeyUsage = nil
	}
	if parent == nil {
		parent = template
		parentKey = key
	} else {
		template.IssuingCertificateURL = []string{"http://issuer.example/" + name + ".crt"}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, parent, &key.PublicKey, parentKey)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert, key
}

func trustEntry(cert *x509.Certificate, hosts ...string) ProbeTrustCertificate {
	return ProbeTrustCertificate{PEM: string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw})), Hosts: hosts}
}

func TestProbeChainRecoveryAndStrictFailures(t *testing.T) {
	root, rootKey := testTrustCert(t, "root", true, nil, nil)
	intermediate, intKey := testTrustCert(t, "intermediate", true, root, rootKey)
	leaf, _ := testTrustCert(t, "leaf", false, intermediate, intKey)
	roots := x509.NewCertPool()
	roots.AddCert(root)
	falseValue := false
	for _, tc := range []struct {
		name, host string
		chain      []*x509.Certificate
		policy     *ProbeTrustPolicy
		roots      *x509.CertPool
		wantOK     bool
		wantFetch  bool
	}{
		{"missing intermediate", "service.test", []*x509.Certificate{leaf}, nil, roots, true, true},
		{"complete chain", "service.test", []*x509.Certificate{leaf, intermediate}, nil, roots, true, false},
		{"disabled", "service.test", []*x509.Certificate{leaf}, &ProbeTrustPolicy{AutoFetchIntermediates: &falseValue}, roots, false, false},
		{"wrong hostname", "wrong.test", []*x509.Certificate{leaf}, nil, roots, false, false},
		{"untrusted root never promoted", "service.test", []*x509.Certificate{leaf}, nil, x509.NewCertPool(), false, true},
		{"internal CA", "service.test", []*x509.Certificate{leaf, intermediate}, &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(root)}}, x509.NewCertPool(), true, false},
		{"CA scope mismatch", "service.test", []*x509.Certificate{leaf, intermediate}, &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(root, "other.test")}}, x509.NewCertPool(), false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			err := verifyProbeChain(context.Background(), tc.host, tc.chain, tc.policy, tc.roots, func(_ context.Context, address string) ([]*x509.Certificate, error) {
				calls++
				return []*x509.Certificate{intermediate, root}, nil
			})
			if (err == nil) != tc.wantOK {
				t.Fatalf("error=%v", err)
			}
			if (calls > 0) != tc.wantFetch {
				t.Fatalf("fetches=%d", calls)
			}
		})
	}
}

func TestProbeSelfSignedScopeAndRemoval(t *testing.T) {
	leaf, _ := testTrustCert(t, "self", false, nil, nil)
	for _, tc := range []struct {
		name    string
		entries []ProbeTrustCertificate
		ok      bool
	}{
		{"scoped", []ProbeTrustCertificate{trustEntry(leaf, "service.test")}, true},
		{"unscoped", []ProbeTrustCertificate{trustEntry(leaf)}, false},
		{"other host", []ProbeTrustCertificate{trustEntry(leaf, "other.test")}, false},
		{"removed", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := verifyProbeChain(context.Background(), "service.test", []*x509.Certificate{leaf}, &ProbeTrustPolicy{Certificates: tc.entries}, x509.NewCertPool(), func(context.Context, string) ([]*x509.Certificate, error) {
				t.Fatal("unexpected fetch")
				return nil, nil
			})
			if (err == nil) != tc.ok {
				t.Fatalf("error=%v", err)
			}
		})
	}
}

func TestProbeRecoveryRejectsExpiredWrongSignerAndCancellation(t *testing.T) {
	root, key := testTrustCert(t, "root", true, nil, nil)
	intermediate, ik := testTrustCert(t, "issuer", true, root, key)
	leaf, _ := testTrustCert(t, "leaf", false, intermediate, ik)
	roots := x509.NewCertPool()
	roots.AddCert(root)
	other, _ := testTrustCert(t, "wrong", true, nil, nil)
	if err := verifyProbeChain(context.Background(), "service.test", []*x509.Certificate{leaf}, nil, roots, func(context.Context, string) ([]*x509.Certificate, error) { return []*x509.Certificate{other}, nil }); err == nil {
		t.Fatal("accepted wrong signer")
	}
	expired := *leaf
	expired.NotAfter = time.Now().Add(-time.Second)
	if err := verifyProbeChain(context.Background(), "service.test", []*x509.Certificate{&expired}, nil, roots, func(context.Context, string) ([]*x509.Certificate, error) {
		t.Fatal("expired leaf triggered fetch")
		return nil, nil
	}); err == nil {
		t.Fatal("accepted expired leaf")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := verifyProbeChain(ctx, "service.test", []*x509.Certificate{leaf}, nil, roots, func(context.Context, string) ([]*x509.Certificate, error) {
		t.Fatal("canceled lookup")
		return nil, nil
	}); err != context.Canceled {
		t.Fatalf("error=%v", err)
	}
}

func TestIssuerNetworkRestrictions(t *testing.T) {
	for _, raw := range []string{"127.0.0.1", "192.168.8.221", "10.0.0.1", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "::ffff:127.0.0.1", "fd00::1", "fe80::1", "64:ff9b::7f00:1"} {
		if publicIssuerIP(net.ParseIP(raw)) {
			t.Errorf("allowed %s", raw)
		}
	}
	if !publicIssuerIP(net.ParseIP("8.8.8.8")) {
		t.Fatal("public address blocked")
	}
	for _, address := range []string{"file:///etc/passwd", "http://user:pass@example.com/", "http://example.com:22/", "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "https://example.com/#fragment"} {
		if _, err := fetchIntermediate(context.Background(), address); err == nil {
			t.Errorf("accepted %s", address)
		}
	}
}

func TestHTTPProbePrivateTrustIPAndWorkflow(t *testing.T) {
	cert, key := testTrustCert(t, "service", false, nil, nil)
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("healthy")) }))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{cert.Raw}, PrivateKey: key}}}
	srv.StartTLS()
	defer srv.Close()
	c := NewHTTPChecker(zap.NewNop().Sugar())
	for _, workflow := range []bool{false, true} {
		sc := &ServiceCheck{TargetURL: srv.URL, HTTPExpectedStatus: 200, Timeout: time.Second, ProbeTrust: &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(cert, "127.0.0.1")}}}
		if workflow {
			sc.WorkflowSteps = []HTTPWorkflowStep{{URL: srv.URL, ExpectedStatuses: "200", ContentMatch: "healthy"}}
		}
		if result := c.Check(context.Background(), sc, "test"); !result.IsUp {
			t.Fatalf("workflow=%v: %s", workflow, result.Error)
		}
		sc.ProbeTrust = nil
		if result := c.Check(context.Background(), sc, "test"); result.IsUp {
			t.Fatal("removed trust still accepted")
		}
	}
	// Trust scoped to the IP must not apply when the same server is addressed by name.
	sc := &ServiceCheck{TargetURL: strings.Replace(srv.URL, "127.0.0.1", "localhost", 1), HTTPExpectedStatus: 200, Timeout: time.Second, ProbeTrust: &ProbeTrustPolicy{Certificates: []ProbeTrustCertificate{trustEntry(cert, "127.0.0.1")}}}
	if result := c.Check(context.Background(), sc, "test"); result.IsUp {
		t.Fatal("accepted wrong hostname")
	}
}

func TestLiveIssuerRecovery(t *testing.T) {
	path := os.Getenv("ZENPLUS_TEST_CHAIN_PEM")
	if path == "" {
		t.Skip("optional live AIA test")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(data)
	if block == nil {
		t.Fatal("invalid PEM")
	}
	leaf, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	host := os.Getenv("ZENPLUS_TEST_CHAIN_HOST")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	disabled := false
	if err := verifyProbeChain(ctx, host, []*x509.Certificate{leaf}, &ProbeTrustPolicy{AutoFetchIntermediates: &disabled}, nil, fetchIntermediate); err == nil {
		t.Fatal("fixture should require issuer recovery")
	}
	if err := verifyProbeChain(ctx, host, []*x509.Certificate{leaf}, nil, nil, fetchIntermediate); err != nil {
		t.Fatal(err)
	}
	if err := verifyProbeChain(ctx, host, []*x509.Certificate{leaf}, nil, nil, fetchIntermediate); err != nil {
		t.Fatal("cached issuer:", err)
	}
}
