package selfupdate

import (
	"crypto/sha1"
	"crypto/x509"
	"encoding/asn1"
	"encoding/hex"
	"fmt"
	"strings"
)

// A trusted Authenticode chain proves only that *some* publisher signed a
// package. Self-update runs the package as LocalSystem, so it must also prove
// that the publisher is Zentryc. Releases are signed through Azure Trusted
// Signing, whose leaf certificates rotate every few days, so the pin is on the
// stable parts of the chain rather than a leaf thumbprint:
//
//   - the verified subject (Microsoft ID-verified organisation details),
//   - the identity-validation EKU that Trusted Signing stamps on every leaf
//     issued for Zentryc's certificate profile, and
//   - the Microsoft Identity Verification root the chain terminates in.
//
// Rotating the Trusted Signing identity validation changes the EKU. Ship a
// release that adds the new OID to trustedSignerIdentityEKUs *before* signing
// with the new identity, or agents will refuse the update.
var (
	trustedSignerSubject = signerSubject{
		CommonName:   "Zentryc LLC",
		Organization: "Zentryc LLC",
		Locality:     "Sheridan",
		Province:     "Wyoming",
		Country:      "US",
	}
	// Comma-separated so a build can extend it with -X during a rotation.
	trustedSignerIdentityEKUs = "1.3.6.1.4.1.311.97.731568599.472678502.564581987.184423368"
	// Microsoft Identity Verification Root Certificate Authority 2020.
	trustedSignerRootThumbprints = "F40042E2E5F7E8EF8189FED15519AECE42C3BFA2"
)

var codeSigningEKU = asn1.ObjectIdentifier{1, 3, 6, 1, 5, 5, 7, 3, 3}

type signerSubject struct {
	CommonName, Organization, Locality, Province, Country string
}

type signerPolicy struct {
	Subject         signerSubject
	IdentityEKUs    []string
	RootThumbprints []string
}

func defaultSignerPolicy() signerPolicy {
	return signerPolicy{
		Subject:         trustedSignerSubject,
		IdentityEKUs:    splitList(trustedSignerIdentityEKUs),
		RootThumbprints: splitList(trustedSignerRootThumbprints),
	}
}

func splitList(value string) []string {
	var out []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

// checkSignerChain applies the publisher pin to the DER certificates of the
// chain Windows validated, ordered leaf first and root last. It never sees
// certificates that merely travel inside the signature, which an attacker can
// copy freely.
func checkSignerChain(policy signerPolicy, chain [][]byte) error {
	if len(chain) < 2 {
		return fmt.Errorf("signer chain has %d certificates; a publisher and a root are required", len(chain))
	}
	if len(policy.IdentityEKUs) == 0 || len(policy.RootThumbprints) == 0 || policy.Subject.Organization == "" {
		return fmt.Errorf("no trusted publisher is configured")
	}
	leaf, err := x509.ParseCertificate(chain[0])
	if err != nil {
		return fmt.Errorf("parse signer certificate: %w", err)
	}
	subject := leaf.Subject
	got := signerSubject{CommonName: subject.CommonName}
	got.Organization = first(subject.Organization)
	got.Locality = first(subject.Locality)
	got.Province = first(subject.Province)
	got.Country = first(subject.Country)
	if got != policy.Subject {
		return fmt.Errorf("package is signed by %q, not the ZenPlus publisher", subject.String())
	}

	var codeSigning, identity bool
	for _, oid := range leaf.UnknownExtKeyUsage {
		if oid.Equal(codeSigningEKU) {
			codeSigning = true
		}
		for _, allowed := range policy.IdentityEKUs {
			if oid.String() == allowed {
				identity = true
			}
		}
	}
	for _, usage := range leaf.ExtKeyUsage {
		if usage == x509.ExtKeyUsageCodeSigning {
			codeSigning = true
		}
	}
	if !codeSigning {
		return fmt.Errorf("signer certificate is not issued for code signing")
	}
	if !identity {
		return fmt.Errorf("signer certificate does not carry the ZenPlus signing identity")
	}

	sum := sha1.Sum(chain[len(chain)-1])
	root := strings.ToUpper(hex.EncodeToString(sum[:]))
	for _, allowed := range policy.RootThumbprints {
		if strings.EqualFold(root, allowed) {
			return nil
		}
	}
	return fmt.Errorf("signer chain ends in untrusted root %s", root)
}

func first(values []string) string {
	if len(values) == 0 {
		return ""
	}
	return values[0]
}
