package checker

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"time"
)

// ProbeTrustPolicy is supplied by the administrator-controlled settings store,
// never by an individual service check's type-specific config.
type ProbeTrustPolicy struct {
	AutoFetchIntermediates *bool                   `json:"auto_fetch_intermediates,omitempty"`
	Certificates           []ProbeTrustCertificate `json:"certificates"`
}

type ProbeTrustCertificate struct {
	PEM   string   `json:"pem"`
	Hosts []string `json:"hosts"`
}

func probeTransport(sc *ServiceCheck, roots *x509.CertPool) *http.Transport {
	dial := &net.Dialer{Timeout: sc.Timeout}
	return &http.Transport{DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
		return dial.DialContext(ctx, ProbeNetwork(probeIPVersion(sc)), addr)
	}, DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
		host, _, err := net.SplitHostPort(addr)
		if err != nil {
			return nil, err
		}
		cfg := probeTLSConfig(ctx, host, sc.ProbeTrust, roots, sc.HTTPIgnoreTLSErrors)
		raw, err := dial.DialContext(ctx, ProbeNetwork(probeIPVersion(sc)), addr)
		if err != nil {
			return nil, err
		}
		conn := tls.Client(raw, cfg)
		trace := httptrace.ContextClientTrace(ctx)
		if trace != nil && trace.TLSHandshakeStart != nil {
			trace.TLSHandshakeStart()
		}
		handshakeCtx, cancel := context.WithTimeout(ctx, sc.Timeout)
		defer cancel()
		err = conn.HandshakeContext(handshakeCtx)
		if trace != nil && trace.TLSHandshakeDone != nil {
			trace.TLSHandshakeDone(conn.ConnectionState(), err)
		}
		if err != nil {
			raw.Close()
			return nil, err
		}
		return conn, nil
	}}
}

func probeTLSConfig(ctx context.Context, host string, policy *ProbeTrustPolicy, roots *x509.CertPool, ignore bool) *tls.Config {
	cfg := &tls.Config{ServerName: host, InsecureSkipVerify: true} // Custom verifier below performs full x509 verification after chain recovery.
	if !ignore {
		cfg.VerifyConnection = func(cs tls.ConnectionState) error {
			return verifyProbeChain(ctx, host, cs.PeerCertificates, policy, roots, fetchIntermediate)
		}
	}
	return cfg
}

type issuerFetcher func(context.Context, string) ([]*x509.Certificate, error)

// VerifyServiceTLS returns a leaf only after strict verification. The caller
// may pin that leaf for its immediately following HTTP connection.
func VerifyServiceTLS(ctx context.Context, host string, port int, policy *ProbeTrustPolicy, ipVersion ...string) (*x509.Certificate, error) {
	d := tls.Dialer{Config: probeTLSConfig(ctx, host, policy, nil, false)}
	network := "tcp"
	if len(ipVersion) > 0 {
		network = ProbeNetwork(ipVersion[0])
	}
	conn, err := d.DialContext(ctx, network, net.JoinHostPort(host, fmt.Sprint(port)))
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	return conn.(*tls.Conn).ConnectionState().PeerCertificates[0], nil
}

func verifyProbeChain(ctx context.Context, host string, chain []*x509.Certificate, policy *ProbeTrustPolicy, roots *x509.CertPool, fetch issuerFetcher) error {
	if len(chain) == 0 {
		return errors.New("no certificates presented")
	}
	leaf := chain[0]
	if err := leaf.VerifyHostname(host); err != nil {
		return err
	}
	if roots == nil {
		var err error
		roots, err = x509.SystemCertPool()
		if err != nil {
			return fmt.Errorf("load system certificate authorities: %w", err)
		}
	} else {
		roots = roots.Clone()
	}
	if policy != nil {
		for _, entry := range policy.Certificates {
			if len(entry.Hosts) > 0 {
				match := false
				for _, scope := range entry.Hosts {
					if strings.EqualFold(strings.TrimSuffix(scope, "."), strings.TrimSuffix(host, ".")) {
						match = true
					}
				}
				if !match {
					continue
				}
			}
			block, _ := pem.Decode([]byte(entry.PEM))
			if block == nil || block.Type != "CERTIFICATE" {
				return errors.New("invalid configured probe trust certificate")
			}
			cert, err := x509.ParseCertificate(block.Bytes)
			if err != nil {
				return fmt.Errorf("invalid configured probe trust certificate: %w", err)
			}
			if !cert.IsCA {
				if len(entry.Hosts) == 0 || cert.CheckSignature(cert.SignatureAlgorithm, cert.RawTBSCertificate, cert.Signature) != nil || !bytes.Equal(cert.RawIssuer, cert.RawSubject) {
					return errors.New("self-signed service certificates require an explicit host scope and valid self signature")
				}
			}
			roots.AddCert(cert)
		}
	}
	intermediates := x509.NewCertPool()
	for _, cert := range chain[1:] {
		intermediates.AddCert(cert)
	}
	opts := x509.VerifyOptions{DNSName: host, Roots: roots, Intermediates: intermediates, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	_, err := leaf.Verify(opts)
	if err == nil {
		return nil
	}
	var unknown x509.UnknownAuthorityError
	if !errors.As(err, &unknown) || (policy != nil && policy.AutoFetchIntermediates != nil && !*policy.AutoFetchIntermediates) {
		return err
	}
	// Only issuer certificates are fetched. They are never added to Roots.
	queue := append([]*x509.Certificate(nil), chain...)
	seen := map[string]bool{}
	attempts := 0
	for len(queue) > 0 && attempts < 4 {
		child := queue[0]
		queue = queue[1:]
		for _, address := range child.IssuingCertificateURL {
			if seen[address] || attempts >= 4 {
				continue
			}
			seen[address] = true
			attempts++
			if ctx.Err() != nil {
				return ctx.Err()
			}
			certs, fetchErr := fetch(ctx, address)
			if fetchErr != nil {
				continue
			}
			for _, issuer := range certs {
				if child.CheckSignatureFrom(issuer) != nil {
					continue
				}
				intermediates.AddCert(issuer)
				if _, verifyErr := leaf.Verify(opts); verifyErr == nil {
					return nil
				}
				queue = append(queue, issuer)
			}
		}
	}
	return fmt.Errorf("%w (issuer recovery could not build a trusted chain; install the internal CA under Security > Service probe trust if applicable)", err)
}

// Public AIA only. Private PKI is installed explicitly by an administrator.
func publicIssuerIP(ip net.IP) bool {
	a, ok := netip.AddrFromSlice(ip)
	if !ok {
		return false
	}
	a = a.Unmap()
	if !a.IsGlobalUnicast() || a.IsPrivate() || a.IsLoopback() || a.IsLinkLocalUnicast() {
		return false
	}
	for _, raw := range []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "2001:db8::/32", "2001::/23", "2002::/16", "64:ff9b::/96", "64:ff9b:1::/48"} {
		if netip.MustParsePrefix(raw).Contains(a) {
			return false
		}
	}
	return true
}

type issuerCacheEntry struct {
	certs   []*x509.Certificate
	expires time.Time
}

var issuerCache = struct {
	sync.Mutex
	entries map[string]issuerCacheEntry
}{entries: make(map[string]issuerCacheEntry)}

func fetchIntermediate(ctx context.Context, address string) ([]*x509.Certificate, error) {
	u, err := url.Parse(address)
	if err != nil || len(address) > 2048 || u.User != nil || u.Hostname() == "" || u.Fragment != "" || (u.Scheme != "http" && u.Scheme != "https") || (u.Port() != "" && u.Port() != "80" && u.Port() != "443") {
		return nil, errors.New("unsupported issuer URL")
	}
	issuerCache.Lock()
	cached, ok := issuerCache.entries[address]
	issuerCache.Unlock()
	if ok && time.Now().Before(cached.expires) {
		return cached.certs, nil
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	transport := &http.Transport{
		Proxy: nil, DisableKeepAlives: true, MaxResponseHeaderBytes: 16384,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, err
			}
			ips, err := net.DefaultResolver.LookupIP(ctx, "ip", host)
			if err != nil {
				return nil, err
			}
			if len(ips) == 0 {
				return nil, errors.New("issuer host has no addresses")
			}
			for _, ip := range ips {
				if !publicIssuerIP(ip) {
					return nil, errors.New("issuer URL must resolve only to public addresses")
				}
			}
			// Dial the validated IP, never resolve the name a second time.
			d := &net.Dialer{}
			return d.DialContext(ctx, network, net.JoinHostPort(ips[0].String(), port))
		},
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("issuer redirects are disabled") }}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("issuer download HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	if err != nil {
		return nil, err
	}
	if len(data) > 65536 {
		return nil, errors.New("issuer certificate exceeds 64 KiB")
	}
	if block, _ := pem.Decode(data); block != nil && block.Type == "CERTIFICATE" {
		data = block.Bytes
	}
	cert, err := x509.ParseCertificate(data)
	if err != nil {
		return nil, err
	}
	if !cert.IsCA || time.Now().Before(cert.NotBefore) || time.Now().After(cert.NotAfter) {
		return nil, errors.New("issuer is not a currently valid CA")
	}
	certs := []*x509.Certificate{cert}
	issuerCache.Lock()
	if len(issuerCache.entries) >= 64 { // Bounded cache; final verification always rechecks trust and validity.
		for key := range issuerCache.entries {
			delete(issuerCache.entries, key)
			break
		}
	}
	issuerCache.entries[address] = issuerCacheEntry{certs: certs, expires: time.Now().Add(time.Hour)}
	issuerCache.Unlock()
	return certs, nil
}
