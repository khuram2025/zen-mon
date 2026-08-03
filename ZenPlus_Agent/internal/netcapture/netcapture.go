// Package netcapture runs on-demand network flow captures.
//
// This is connection-level flow accounting, not packet capture: it needs no
// Npcap/WinPcap driver and never touches payloads. During a capture window
// the agent samples the OS connection table on a short interval, keys each
// connection by its 5-tuple plus owning PID, and records how many bytes that
// connection has moved. Per-connection byte counters come from Windows TCP
// ESTATS (RFC 4898); see estats_windows.go.
//
// Flows are aggregated in memory and flushed to the controller periodically
// so the UI can follow a running capture instead of waiting for it to end.
package netcapture

import (
	"context"
	"fmt"
	"net"
	"sort"
	"strings"
	"sync"
	"time"
)

// Flow is one observed conversation over the capture window.
type Flow struct {
	Protocol      string `json:"protocol"`
	LocalIP       string `json:"local_ip"`
	LocalPort     uint32 `json:"local_port"`
	RemoteIP      string `json:"remote_ip"`
	RemotePort    uint32 `json:"remote_port"`
	PID           int32  `json:"pid"`
	ProcessName   string `json:"process_name"`
	ServiceName   string `json:"service_name,omitempty"`
	State         string `json:"state"`
	BytesSent     uint64 `json:"bytes_sent"`
	BytesReceived uint64 `json:"bytes_received"`
	// BytesKnown is false when the platform could not attribute byte counts
	// to this flow (UDP, or ESTATS unavailable). The UI must not render 0 as
	// "no traffic" in that case.
	BytesKnown bool      `json:"bytes_known"`
	FirstSeen  time.Time `json:"first_seen"`
	LastSeen   time.Time `json:"last_seen"`
	Samples    int       `json:"samples"`
}

type Options struct {
	Duration       time.Duration
	SampleInterval time.Duration
	// Interface limits the capture to flows whose local address belongs to
	// this interface. Empty means every interface.
	Interface string
	// FlushInterval controls how often partial results are emitted.
	FlushInterval time.Duration
	MaxFlows      int
}

func (o *Options) applyDefaults() {
	if o.Duration <= 0 {
		o.Duration = 5 * time.Minute
	}
	if o.Duration > time.Hour {
		o.Duration = time.Hour
	}
	if o.SampleInterval <= 0 {
		o.SampleInterval = 2 * time.Second
	}
	if o.FlushInterval <= 0 {
		o.FlushInterval = 10 * time.Second
	}
	if o.MaxFlows <= 0 {
		o.MaxFlows = 5000
	}
}

type flowKey struct {
	proto      string
	localIP    string
	localPort  uint32
	remoteIP   string
	remotePort uint32
	pid        int32
}

// FlushFunc receives a snapshot of flows observed so far. It is called on the
// flush interval and once more when the capture ends (final=true).
type FlushFunc func(flows []Flow, final bool, stats Stats)

type Stats struct {
	StartedAt      time.Time `json:"started_at"`
	EndsAt         time.Time `json:"ends_at"`
	Samples        int       `json:"samples"`
	FlowCount      int       `json:"flow_count"`
	Truncated      bool      `json:"truncated"`
	BytesAvailable bool      `json:"bytes_available"`
	Note           string    `json:"note,omitempty"`
}

type collector struct {
	mu    sync.Mutex
	flows map[flowKey]*Flow
	// baseline holds the first byte counters seen for a connection, so a
	// long-lived connection reports only what it moved during this window
	// rather than its lifetime total.
	baseline  map[flowKey][2]uint64
	truncated bool
}

// Run executes a capture until the duration elapses or ctx is cancelled.
func Run(ctx context.Context, opts Options, flush FlushFunc, logf func(string, ...any)) (Stats, error) {
	opts.applyDefaults()

	local, err := localAddrsForInterface(opts.Interface)
	if err != nil {
		return Stats{}, err
	}

	c := &collector{
		flows:    map[flowKey]*Flow{},
		baseline: map[flowKey][2]uint64{},
	}
	stats := Stats{StartedAt: time.Now().UTC()}
	stats.EndsAt = stats.StartedAt.Add(opts.Duration)

	src, note, bytesOK := newSource()
	stats.BytesAvailable = bytesOK
	stats.Note = note
	if note != "" {
		logf("network capture: %s", note)
	}
	defer src.Close()

	deadline := time.NewTimer(opts.Duration)
	defer deadline.Stop()
	sampleTick := time.NewTicker(opts.SampleInterval)
	defer sampleTick.Stop()
	flushTick := time.NewTicker(opts.FlushInterval)
	defer flushTick.Stop()

	sample := func() {
		conns, err := src.Sample()
		if err != nil {
			logf("network capture sample failed: %v", err)
			return
		}
		c.merge(conns, local, opts.MaxFlows)
		stats.Samples++
	}

	emit := func(final bool) {
		snapshot, count, truncated := c.snapshot()
		stats.FlowCount = count
		stats.Truncated = truncated
		flush(snapshot, final, stats)
	}

	sample()
	for {
		select {
		case <-ctx.Done():
			emit(true)
			return stats, ctx.Err()
		case <-deadline.C:
			sample()
			emit(true)
			return stats, nil
		case <-sampleTick.C:
			sample()
		case <-flushTick.C:
			emit(false)
		}
	}
}

func (c *collector) merge(conns []rawConn, local map[string]bool, maxFlows int) {
	now := time.Now().UTC()
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, rc := range conns {
		if len(local) > 0 && rc.LocalIP != "" && !local[rc.LocalIP] {
			continue
		}
		k := flowKey{
			proto:      rc.Protocol,
			localIP:    rc.LocalIP,
			localPort:  rc.LocalPort,
			remoteIP:   rc.RemoteIP,
			remotePort: rc.RemotePort,
			pid:        rc.PID,
		}
		f, ok := c.flows[k]
		if !ok {
			if len(c.flows) >= maxFlows {
				c.truncated = true
				continue
			}
			f = &Flow{
				Protocol:    rc.Protocol,
				LocalIP:     rc.LocalIP,
				LocalPort:   rc.LocalPort,
				RemoteIP:    rc.RemoteIP,
				RemotePort:  rc.RemotePort,
				PID:         rc.PID,
				ProcessName: rc.ProcessName,
				ServiceName: rc.ServiceName,
				FirstSeen:   now,
			}
			c.flows[k] = f
			if rc.BytesKnown {
				c.baseline[k] = [2]uint64{rc.BytesSent, rc.BytesReceived}
			}
		}
		f.LastSeen = now
		f.State = rc.State
		f.Samples++
		if rc.ProcessName != "" {
			f.ProcessName = rc.ProcessName
		}
		if rc.ServiceName != "" {
			f.ServiceName = rc.ServiceName
		}
		if rc.BytesKnown {
			base := c.baseline[k]
			// Counters are cumulative for the connection's life; subtract the
			// first reading so the flow reflects this window only. A counter
			// that moves backwards means the OS reused the tuple, so re-base.
			if rc.BytesSent < base[0] || rc.BytesReceived < base[1] {
				base = [2]uint64{rc.BytesSent, rc.BytesReceived}
				c.baseline[k] = base
			}
			f.BytesSent = rc.BytesSent - base[0]
			f.BytesReceived = rc.BytesReceived - base[1]
			f.BytesKnown = true
		}
	}
}

func (c *collector) snapshot() ([]Flow, int, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Flow, 0, len(c.flows))
	for _, f := range c.flows {
		out = append(out, *f)
	}
	// Busiest first, so a truncated upload still carries what matters.
	sort.Slice(out, func(i, j int) bool {
		ti := out[i].BytesSent + out[i].BytesReceived
		tj := out[j].BytesSent + out[j].BytesReceived
		if ti != tj {
			return ti > tj
		}
		return out[i].LastSeen.After(out[j].LastSeen)
	})
	return out, len(out), c.truncated
}

// localAddrsForInterface returns the set of local IPs bound to name. An empty
// name yields an empty set, meaning "do not filter".
func localAddrsForInterface(name string) (map[string]bool, error) {
	if strings.TrimSpace(name) == "" {
		return map[string]bool{}, nil
	}
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	for _, iface := range ifaces {
		if !strings.EqualFold(iface.Name, name) {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			return nil, err
		}
		out := map[string]bool{}
		for _, a := range addrs {
			if ipnet, ok := a.(*net.IPNet); ok {
				out[ipnet.IP.String()] = true
			}
		}
		return out, nil
	}
	return nil, fmt.Errorf("interface %q not found on this host", name)
}
