package checker

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"net"
	"net/http/httptrace"
	"sync"
	"time"
)

// NetworkDiagnostics contains connection metadata only, never URLs or headers.
// Phase durations accumulate across redirects and workflow connections.
type NetworkDiagnostics struct {
	IPVersion    string   `json:"ip_version"`
	RemoteIP     string   `json:"remote_ip,omitempty"`
	DNSMS        *float64 `json:"dns_ms,omitempty"`
	ConnectMS    *float64 `json:"connect_ms,omitempty"`
	TLSMS        *float64 `json:"tls_ms,omitempty"`
	FailureStage string   `json:"failure_stage,omitempty"`
}

func (d *NetworkDiagnostics) JSON() string {
	if d == nil {
		return ""
	}
	b, _ := json.Marshal(d)
	return string(b)
}
func ProbeNetwork(version string) string {
	switch version {
	case "ipv4":
		return "tcp4"
	case "ipv6":
		return "tcp6"
	default:
		return "tcp"
	}
}
func probeIPVersion(sc *ServiceCheck) string {
	v, _ := sc.Config["ip_version"].(string)
	if v == "ipv4" || v == "ipv6" {
		return v
	}
	return "auto"
}

func traceProbe(ctx context.Context, sc *ServiceCheck, result *ServiceCheckResult) (context.Context, func()) {
	var mu sync.Mutex
	d := NetworkDiagnostics{IPVersion: probeIPVersion(sc)}
	var dnsStart, tlsStart time.Time
	connects := map[string]time.Time{}
	add := func(dst **float64, start time.Time) {
		if start.IsZero() {
			return
		}
		v := float64(time.Since(start).Microseconds()) / 1000
		if *dst != nil {
			v += **dst
		}
		*dst = &v
	}
	trace := &httptrace.ClientTrace{
		DNSStart: func(httptrace.DNSStartInfo) {
			mu.Lock()
			defer mu.Unlock()
			dnsStart = time.Now()
			d.FailureStage = "dns"
		},
		DNSDone: func(i httptrace.DNSDoneInfo) {
			mu.Lock()
			defer mu.Unlock()
			add(&d.DNSMS, dnsStart)
			if i.Err == nil {
				d.FailureStage = "connect"
			}
		},
		ConnectStart: func(network, addr string) {
			mu.Lock()
			defer mu.Unlock()
			connects[network+addr] = time.Now()
			d.FailureStage = "connect"
		},
		ConnectDone: func(network, addr string, err error) {
			mu.Lock()
			defer mu.Unlock()
			add(&d.ConnectMS, connects[network+addr])
			delete(connects, network+addr)
			if err == nil {
				host, _, _ := net.SplitHostPort(addr)
				d.RemoteIP = host
			}
		},
		TLSHandshakeStart: func() { mu.Lock(); defer mu.Unlock(); tlsStart = time.Now(); d.FailureStage = "tls" },
		TLSHandshakeDone: func(_ tls.ConnectionState, err error) {
			mu.Lock()
			defer mu.Unlock()
			add(&d.TLSMS, tlsStart)
			if err == nil {
				d.FailureStage = "response"
			}
		},
		GotConn: func(i httptrace.GotConnInfo) {
			mu.Lock()
			defer mu.Unlock()
			host, _, _ := net.SplitHostPort(i.Conn.RemoteAddr().String())
			d.RemoteIP = host
			d.FailureStage = "response"
		},
	}
	return httptrace.WithClientTrace(ctx, trace), func() {
		mu.Lock()
		defer mu.Unlock()
		if result.IsUp {
			d.FailureStage = ""
		} else if result.StatusCode > 0 {
			d.FailureStage = "http"
		}
		snapshot := d
		result.Diagnostics = &snapshot
	}
}
