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

	"zenplus-agent/internal/netiface"
)

// Flow is one observed connection, listener, or local datagram endpoint over
// the capture window. Process/service ownership always refers to the local PID.
type Flow struct {
	Protocol      string `json:"protocol"`
	Kind          string `json:"kind"`
	Direction     string `json:"direction"`
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
	if o.SampleInterval < time.Second {
		o.SampleInterval = time.Second
	}
	if o.FlushInterval <= 0 {
		o.FlushInterval = 10 * time.Second
	}
	if o.MaxFlows <= 0 {
		o.MaxFlows = 5000
	}
	if o.MaxFlows > 10000 {
		o.MaxFlows = 10000
	}
	o.Interface = strings.TrimSpace(o.Interface)
}

// NormalizeOptions applies the resource and duration bounds used by Run.
func NormalizeOptions(opts Options) Options {
	opts.applyDefaults()
	return opts
}

// ValidateInterface checks an operator-supplied interface name before a
// background capture is accepted. Empty means all interfaces.
func ValidateInterface(name string) error {
	_, err := localAddrsForInterface(strings.TrimSpace(name))
	return err
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
	StartedAt      time.Time          `json:"started_at"`
	EndsAt         time.Time          `json:"ends_at"`
	Samples        int                `json:"samples"`
	FlowCount      int                `json:"flow_count"`
	Truncated      bool               `json:"truncated"`
	BytesAvailable bool               `json:"bytes_available"`
	Note           string             `json:"note,omitempty"`
	Interfaces     []InterfaceTraffic `json:"interfaces,omitempty"`
}

// InterfaceTraffic uses native interface counters, so it includes TCP, UDP,
// ICMP, IPv4, IPv6, and traffic too short-lived to appear in a socket sample.
// RXBytes/TXBytes are cumulative since this capture began; rates are bits/s.
type InterfaceTraffic struct {
	Interface            string
	InterfaceIndex       uint32
	Timestamp            time.Time
	RXBytes              uint64
	TXBytes              uint64
	RXBPS                float64
	TXBPS                float64
	PeakRXBPS            float64
	PeakTXBPS            float64
	LinkSpeedBPS         uint64
	ReceiveLinkSpeedBPS  uint64
	TransmitLinkSpeedBPS uint64
	RXUtilizationPct     float64
	TXUtilizationPct     float64
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
	opts = NormalizeOptions(opts)

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

	src, note, _ := newSource()
	stats.Note = note
	if note != "" {
		logf("network capture: %s", note)
	}
	defer src.Close()
	interfaces := newInterfaceTracker(opts.Interface, netiface.Snapshot)

	deadline := time.NewTimer(opts.Duration)
	defer deadline.Stop()
	sampleTick := time.NewTicker(opts.SampleInterval)
	defer sampleTick.Stop()
	flushTick := time.NewTicker(opts.FlushInterval)
	defer flushTick.Stop()

	sample := func() {
		now := time.Now().UTC()
		conns, err := src.Sample(ctx)
		if err != nil {
			if ctx.Err() == nil {
				logf("network capture sample failed: %v", err)
			}
		} else {
			c.merge(conns, local, opts.MaxFlows)
			stats.Samples++
		}
		if samples, err := interfaces.sample(ctx, now); err != nil {
			if ctx.Err() == nil {
				logf("network interface sample failed: %v", err)
			}
		} else {
			stats.Interfaces = samples
		}
	}

	emit := func(final bool) {
		snapshot, count, truncated, bytesAvailable := c.snapshot()
		stats.FlowCount = count
		stats.Truncated = truncated
		stats.BytesAvailable = bytesAvailable
		flush(snapshot, final, stats)
	}

	sample()
	for {
		select {
		case <-ctx.Done():
			stats.EndsAt = time.Now().UTC()
			emit(true)
			return stats, ctx.Err()
		case <-deadline.C:
			sample()
			stats.EndsAt = time.Now().UTC()
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
		if len(local) > 0 && rc.LocalIP != "" && !isUnspecifiedLocalIP(rc.LocalIP) && !local[rc.LocalIP] {
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
				Kind:        rc.Kind,
				Direction:   rc.Direction,
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
		if rc.Kind != "" {
			f.Kind = rc.Kind
		}
		if rc.Direction != "" {
			f.Direction = rc.Direction
		}
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

func isUnspecifiedLocalIP(value string) bool {
	ip := net.ParseIP(strings.TrimSpace(value))
	return ip != nil && ip.IsUnspecified()
}

func (c *collector) snapshot() ([]Flow, int, bool, bool) {
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
	bytesAvailable := false
	for i := range out {
		if out[i].BytesKnown {
			bytesAvailable = true
			break
		}
	}
	return out, len(out), c.truncated, bytesAvailable
}

type interfaceSnapshotFunc func(context.Context) ([]netiface.Counter, error)

type interfacePoint struct {
	baseline netiface.Counter
	previous netiface.Counter
	lastAt   time.Time
	peakRX   float64
	peakTX   float64
}

type interfaceTracker struct {
	selected string
	snapshot interfaceSnapshotFunc
	points   map[string]*interfacePoint
}

func newInterfaceTracker(selected string, snapshot interfaceSnapshotFunc) *interfaceTracker {
	return &interfaceTracker{
		selected: strings.TrimSpace(selected),
		snapshot: snapshot,
		points:   map[string]*interfacePoint{},
	}
}

func (t *interfaceTracker) sample(ctx context.Context, now time.Time) ([]InterfaceTraffic, error) {
	counters, err := t.snapshot(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]InterfaceTraffic, 0, len(counters))
	for _, counter := range counters {
		if t.selected != "" && !strings.EqualFold(counter.Name, t.selected) &&
			!strings.EqualFold(counter.Description, t.selected) {
			continue
		}
		key := fmt.Sprintf("%d", counter.InterfaceIndex)
		if counter.InterfaceIndex == 0 {
			key = strings.ToLower(counter.Name)
		}
		point, ok := t.points[key]
		if !ok {
			point = &interfacePoint{baseline: counter, previous: counter, lastAt: now}
			t.points[key] = point
		}
		dt := now.Sub(point.lastAt).Seconds()
		var rxBPS, txBPS float64
		if counter.BytesReceived < point.previous.BytesReceived {
			point.baseline.BytesReceived = counter.BytesReceived
		}
		if counter.BytesSent < point.previous.BytesSent {
			point.baseline.BytesSent = counter.BytesSent
		}
		if dt > 0 {
			rxBPS = float64(counterDelta(counter.BytesReceived, point.previous.BytesReceived)) * 8 / dt
			txBPS = float64(counterDelta(counter.BytesSent, point.previous.BytesSent)) * 8 / dt
		}
		if rxBPS > point.peakRX {
			point.peakRX = rxBPS
		}
		if txBPS > point.peakTX {
			point.peakTX = txBPS
		}
		linkSpeed := counter.ReceiveLinkSpeedBPS
		if counter.TransmitLinkSpeedBPS > linkSpeed {
			linkSpeed = counter.TransmitLinkSpeedBPS
		}
		out = append(out, InterfaceTraffic{
			Interface:            counter.Name,
			InterfaceIndex:       counter.InterfaceIndex,
			Timestamp:            now,
			RXBytes:              counterDelta(counter.BytesReceived, point.baseline.BytesReceived),
			TXBytes:              counterDelta(counter.BytesSent, point.baseline.BytesSent),
			RXBPS:                rxBPS,
			TXBPS:                txBPS,
			PeakRXBPS:            point.peakRX,
			PeakTXBPS:            point.peakTX,
			LinkSpeedBPS:         linkSpeed,
			ReceiveLinkSpeedBPS:  counter.ReceiveLinkSpeedBPS,
			TransmitLinkSpeedBPS: counter.TransmitLinkSpeedBPS,
			RXUtilizationPct:     utilizationPct(rxBPS, counter.ReceiveLinkSpeedBPS),
			TXUtilizationPct:     utilizationPct(txBPS, counter.TransmitLinkSpeedBPS),
		})
		point.previous = counter
		point.lastAt = now
	}
	sort.Slice(out, func(i, j int) bool {
		return strings.ToLower(out[i].Interface) < strings.ToLower(out[j].Interface)
	})
	return out, nil
}

func counterDelta(current, before uint64) uint64 {
	if current < before {
		return 0
	}
	return current - before
}

func utilizationPct(bitsPerSecond float64, linkSpeed uint64) float64 {
	if bitsPerSecond <= 0 || linkSpeed == 0 {
		return 0
	}
	pct := bitsPerSecond / float64(linkSpeed) * 100
	if pct > 100 {
		return 100
	}
	return pct
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
