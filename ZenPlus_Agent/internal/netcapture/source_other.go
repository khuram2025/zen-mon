//go:build !windows

package netcapture

import (
	"context"
	stdnet "net"
	"strings"
	"time"

	"github.com/shirou/gopsutil/v4/net"
	"github.com/shirou/gopsutil/v4/process"
)

type rawConn struct {
	Protocol      string
	Kind          string
	Direction     string
	LocalIP       string
	LocalPort     uint32
	RemoteIP      string
	RemotePort    uint32
	PID           int32
	ProcessName   string
	ServiceName   string
	State         string
	BytesSent     uint64
	BytesReceived uint64
	BytesKnown    bool
}

type source interface {
	Sample(context.Context) ([]rawConn, error)
	Close()
}

type posixSource struct {
	procNames map[int32]string
	procAt    time.Time
}

// Linux exposes no per-connection byte counters without eBPF or conntrack
// accounting, so flows are reported without traffic volume.
func newSource() (source, string, bool) {
	return &posixSource{procNames: map[int32]string{}},
		"per-connection byte counters are not available on this platform; " +
			"capturing connections without traffic volume", false
}

func (s *posixSource) Close() {}

func (s *posixSource) Sample(parent context.Context) ([]rawConn, error) {
	ctx, cancel := context.WithTimeout(parent, 15*time.Second)
	defer cancel()

	conns, err := net.ConnectionsWithContext(ctx, "inet")
	if err != nil {
		return nil, err
	}
	if time.Since(s.procAt) >= 5*time.Second {
		if procs, err := process.ProcessesWithContext(ctx); err == nil {
			next := make(map[int32]string, len(procs))
			for _, p := range procs {
				if p.Pid <= 0 {
					continue
				}
				if name, err := p.NameWithContext(ctx); err == nil && name != "" {
					next[p.Pid] = name
				}
			}
			s.procNames = next
			s.procAt = time.Now()
		}
	}

	out := make([]rawConn, 0, len(conns))
	for _, c := range conns {
		proto := "tcp"
		if c.Type == 2 {
			proto = "udp"
		}
		hasPeer := c.Raddr.Port > 0 && !posixUnspecifiedIP(c.Raddr.IP)
		kind := "connection"
		direction := "unknown"
		state := strings.ToLower(strings.TrimSpace(c.Status))
		if proto == "udp" {
			kind = "endpoint"
			direction = "local"
			if state == "" {
				state = "bound"
			}
		} else if state == "listen" {
			kind = "listener"
			direction = "inbound"
		} else if !hasPeer {
			kind = "endpoint"
			direction = "local"
		} else if ip := stdnet.ParseIP(c.Raddr.IP); ip != nil && ip.IsLoopback() {
			direction = "local"
		}
		remoteIP := ""
		remotePort := uint32(0)
		if hasPeer {
			remoteIP = c.Raddr.IP
			remotePort = c.Raddr.Port
		}
		out = append(out, rawConn{
			Protocol:    proto,
			Kind:        kind,
			Direction:   direction,
			LocalIP:     c.Laddr.IP,
			LocalPort:   c.Laddr.Port,
			RemoteIP:    remoteIP,
			RemotePort:  remotePort,
			PID:         c.Pid,
			State:       state,
			ProcessName: s.procNames[c.Pid],
		})
	}
	return out, nil
}

func posixUnspecifiedIP(value string) bool {
	value = strings.TrimSpace(value)
	if value == "" {
		return true
	}
	ip := stdnet.ParseIP(value)
	return ip != nil && ip.IsUnspecified()
}
