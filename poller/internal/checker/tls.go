package checker

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"math"
	"net"
	"time"

	"go.uber.org/zap"
)

// TLSChecker performs TLS certificate health checks.
type TLSChecker struct {
	logger  *zap.SugaredLogger
	rootCAs *x509.CertPool // nil = the system roots; tests set their own
}

// NewTLSChecker creates a new TLS checker.
func NewTLSChecker(logger *zap.SugaredLogger) *TLSChecker {
	return &TLSChecker{logger: logger}
}

// Check performs a TLS certificate check.
func (c *TLSChecker) Check(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	result := &ServiceCheckResult{
		ServiceCheckID: sc.ID,
		DeviceID:       sc.DeviceID,
		CheckType:      "tls",
		Timestamp:      time.Now().UTC(),
		PollerID:       pollerID,
	}

	port := sc.TargetPort
	if port == 0 {
		port = 443
	}

	// A manual host address changes only where the connection goes: the TLS
	// server name and the certificate name check below keep sc.TargetHost.
	addr, host, manual, _ := sc.HostOverrides.connectAddress(net.JoinHostPort(sc.TargetHost, fmt.Sprint(port)), "")

	dialer := &net.Dialer{Timeout: sc.Timeout}
	tlsConfig := probeTLSConfig(ctx, sc.TargetHost, sc.ProbeTrust, c.rootCAs, false)

	start := time.Now()
	tlsDialer := &tls.Dialer{NetDialer: dialer, Config: tlsConfig}
	conn, err := tlsDialer.DialContext(ctx, "tcp", addr)
	result.ResponseTime = time.Since(start)
	if manual != "" {
		result.Diagnostics = overrideDiagnostics(sc, host, manual, err == nil)
	}

	if err != nil {
		result.Error = fmt.Sprintf("tls connect failed: %v", err)
		valid := false
		result.TLSValid = &valid
		return result
	}
	defer conn.Close()

	certs := conn.(*tls.Conn).ConnectionState().PeerCertificates
	if len(certs) == 0 {
		result.Error = "no certificates presented"
		valid := false
		result.TLSValid = &valid
		return result
	}

	leaf := certs[0]
	now := time.Now()
	daysRemaining := int(math.Floor(leaf.NotAfter.Sub(now).Hours() / 24))

	result.TLSDaysRemaining = &daysRemaining
	result.TLSExpiry = &leaf.NotAfter
	result.TLSIssuer = leaf.Issuer.String()
	result.TLSSubject = leaf.Subject.String()

	valid := true

	// Check if expired
	if now.After(leaf.NotAfter) {
		result.Error = "certificate expired"
		valid = false
		result.TLSValid = &valid
		result.IsUp = false
		return result
	}

	// Check if not yet valid
	if now.Before(leaf.NotBefore) {
		result.Error = "certificate not yet valid"
		valid = false
		result.TLSValid = &valid
		result.IsUp = false
		return result
	}

	result.TLSValid = &valid
	result.IsUp = true
	return result
}
