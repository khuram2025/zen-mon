package checker

import (
	"context"
	"fmt"
	"net"
	"strings"
	"time"

	"go.uber.org/zap"
)

// DNSChecker performs DNS resolution checks with optional expected-value match.
type DNSChecker struct {
	logger *zap.SugaredLogger
}

// NewDNSChecker creates a new DNS checker.
func NewDNSChecker(logger *zap.SugaredLogger) *DNSChecker {
	return &DNSChecker{logger: logger}
}

// Check resolves sc.TargetHost for the configured record type and (if
// sc.Config["expected"] is set) verifies the result contains that value.
// Supported record types: A, AAAA, CNAME, MX, TXT, NS.
func (c *DNSChecker) Check(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	result := &ServiceCheckResult{
		ServiceCheckID: sc.ID,
		DeviceID:       sc.DeviceID,
		CheckType:      "dns",
		Timestamp:      time.Now().UTC(),
		PollerID:       pollerID,
	}

	recordType := "A"
	expected := ""
	if sc.Config != nil {
		if v, ok := sc.Config["record_type"].(string); ok && v != "" {
			recordType = strings.ToUpper(v)
		}
		if v, ok := sc.Config["expected"].(string); ok {
			expected = v
		}
	}

	resolver := &net.Resolver{PreferGo: true}
	rctx, cancel := context.WithTimeout(ctx, sc.Timeout)
	defer cancel()

	start := time.Now()
	var answers []string
	var err error

	switch recordType {
	case "A":
		ips, e := resolver.LookupIP(rctx, "ip4", sc.TargetHost)
		err = e
		for _, ip := range ips {
			answers = append(answers, ip.String())
		}
	case "AAAA":
		ips, e := resolver.LookupIP(rctx, "ip6", sc.TargetHost)
		err = e
		for _, ip := range ips {
			answers = append(answers, ip.String())
		}
	case "CNAME":
		cname, e := resolver.LookupCNAME(rctx, sc.TargetHost)
		err = e
		if cname != "" {
			answers = []string{strings.TrimSuffix(cname, ".")}
		}
	case "MX":
		mxs, e := resolver.LookupMX(rctx, sc.TargetHost)
		err = e
		for _, m := range mxs {
			answers = append(answers, fmt.Sprintf("%d %s", m.Pref, strings.TrimSuffix(m.Host, ".")))
		}
	case "TXT":
		txts, e := resolver.LookupTXT(rctx, sc.TargetHost)
		err = e
		answers = txts
	case "NS":
		nss, e := resolver.LookupNS(rctx, sc.TargetHost)
		err = e
		for _, n := range nss {
			answers = append(answers, strings.TrimSuffix(n.Host, "."))
		}
	default:
		result.IsUp = false
		result.Error = "unsupported record type: " + recordType
		return result
	}

	result.ResponseTime = time.Since(start)

	if err != nil {
		result.IsUp = false
		result.Error = err.Error()
		return result
	}
	if len(answers) == 0 {
		result.IsUp = false
		result.Error = "no " + recordType + " records"
		return result
	}
	if expected != "" {
		matched := false
		for _, a := range answers {
			if strings.Contains(a, expected) {
				matched = true
				break
			}
		}
		if !matched {
			result.IsUp = false
			result.Error = fmt.Sprintf("expected %q not in %v", expected, answers)
			return result
		}
	}
	result.IsUp = true
	return result
}
