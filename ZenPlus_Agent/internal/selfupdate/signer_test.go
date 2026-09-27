package selfupdate

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha1"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/hex"
	"math/big"
	"strings"
	"testing"
	"time"
)

const testIdentityEKU = "1.3.6.1.4.1.311.97.1.2.3.4"

type testChain struct {
	root, leaf []byte
}

func newTestChain(t *testing.T, subject pkix.Name, ekus []asn1.ObjectIdentifier, codeSigning bool) testChain {
	t.Helper()
	rootKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	rootTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "Test Root"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	root, err := x509.CreateCertificate(rand.Reader, rootTemplate, rootTemplate, &rootKey.PublicKey, rootKey)
	if err != nil {
		t.Fatal(err)
	}
	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	leafTemplate := &x509.Certificate{
		SerialNumber:       big.NewInt(2),
		Subject:            subject,
		NotBefore:          time.Now().Add(-time.Hour),
		NotAfter:           time.Now().Add(time.Hour),
		KeyUsage:           x509.KeyUsageDigitalSignature,
		UnknownExtKeyUsage: ekus,
	}
	if codeSigning {
		leafTemplate.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageCodeSigning}
	}
	leaf, err := x509.CreateCertificate(rand.Reader, leafTemplate, rootTemplate, &leafKey.PublicKey, rootKey)
	if err != nil {
		t.Fatal(err)
	}
	return testChain{root: root, leaf: leaf}
}

func zentrycName() pkix.Name {
	return pkix.Name{
		CommonName:   "Zentryc LLC",
		Organization: []string{"Zentryc LLC"},
		Locality:     []string{"Sheridan"},
		Province:     []string{"Wyoming"},
		Country:      []string{"US"},
	}
}

func policyFor(chain testChain) signerPolicy {
	sum := sha1.Sum(chain.root)
	return signerPolicy{
		Subject:         trustedSignerSubject,
		IdentityEKUs:    []string{testIdentityEKU},
		RootThumbprints: []string{strings.ToUpper(hex.EncodeToString(sum[:]))},
	}
}

func mustOID(t *testing.T, value string) asn1.ObjectIdentifier {
	t.Helper()
	var oid asn1.ObjectIdentifier
	for _, part := range strings.Split(value, ".") {
		n := 0
		for _, c := range part {
			n = n*10 + int(c-'0')
		}
		oid = append(oid, n)
	}
	return oid
}

func TestCheckSignerChainAcceptsPinnedPublisher(t *testing.T) {
	chain := newTestChain(t, zentrycName(), []asn1.ObjectIdentifier{mustOID(t, testIdentityEKU)}, true)
	if err := checkSignerChain(policyFor(chain), [][]byte{chain.leaf, chain.root}); err != nil {
		t.Fatalf("pinned publisher rejected: %v", err)
	}
}

func TestCheckSignerChainRejectsOtherPublishers(t *testing.T) {
	identity := []asn1.ObjectIdentifier{mustOID(t, testIdentityEKU)}
	other := zentrycName()
	other.Organization = []string{"Some Other Vendor LLC"}
	other.CommonName = "Some Other Vendor LLC"
	lookalike := zentrycName()
	lookalike.Province = []string{"Delaware"}

	good := newTestChain(t, zentrycName(), identity, true)
	unrelatedRoot := newTestChain(t, zentrycName(), identity, true)

	cases := []struct {
		name   string
		chain  [][]byte
		policy signerPolicy
		want   string
	}{
		{"other organisation", [][]byte{newTestChain(t, other, identity, true).leaf, good.root}, policyFor(good), "not the ZenPlus publisher"},
		{"same name, other registration", [][]byte{newTestChain(t, lookalike, identity, true).leaf, good.root}, policyFor(good), "not the ZenPlus publisher"},
		{"missing identity EKU", func() [][]byte { c := newTestChain(t, zentrycName(), nil, true); return [][]byte{c.leaf, c.root} }(), policyFor(good), "signing identity"},
		{"not a code-signing certificate", func() [][]byte { c := newTestChain(t, zentrycName(), identity, false); return [][]byte{c.leaf, c.root} }(), policyFor(good), "code signing"},
		{"untrusted root", [][]byte{unrelatedRoot.leaf, unrelatedRoot.root}, policyFor(good), "untrusted root"},
		{"leaf only", [][]byte{good.leaf}, policyFor(good), "root are required"},
		{"empty policy", [][]byte{good.leaf, good.root}, signerPolicy{}, "no trusted publisher"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkSignerChain(tc.policy, tc.chain)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want %q", err, tc.want)
			}
		})
	}
}

func TestDefaultSignerPolicyIsPopulated(t *testing.T) {
	policy := defaultSignerPolicy()
	if len(policy.IdentityEKUs) == 0 || len(policy.RootThumbprints) == 0 || policy.Subject.Organization != "Zentryc LLC" {
		t.Fatalf("default signer policy is incomplete: %+v", policy)
	}
}
