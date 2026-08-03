//go:build !windows

package netcapture

import (
	"context"
	"strings"
	"time"

	"github.com/shirou/gopsutil/v4/net"
	"github.com/shirou/gopsutil/v4/process"
)

type rawConn struct {
	Protocol      string
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
	Sample() ([]rawConn, error)
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

func (s *posixSource) Sample() ([]rawConn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conns, err := net.ConnectionsWithContext(ctx, "inet")
	if err != nil {
		return nil, err
	}
	if time.Since(s.procAt) >= 5*time.Second {
		if procs, err := process.ProcessesWithContext(ctx); err == nil {
			next := make(map[int32]string, len(procs))
			for _, p := range procs {
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
		if c.Raddr.IP == "" || c.Raddr.Port == 0 {
			continue
		}
		proto := "tcp"
		if c.Type == 2 {
			proto = "udp"
		}
		out = append(out, rawConn{
			Protocol:    proto,
			LocalIP:     c.Laddr.IP,
			LocalPort:   c.Laddr.Port,
			RemoteIP:    c.Raddr.IP,
			RemotePort:  c.Raddr.Port,
			PID:         c.Pid,
			State:       strings.ToLower(c.Status),
			ProcessName: s.procNames[c.Pid],
		})
	}
	return out, nil
}
