package netpath

// The traceroute engine. Trace() runs a Paris-style multi-flow traceroute to a
// target and returns a fully analyzed RunResult (per-hop RTT/loss, ECMP flow
// paths, end-to-end metrics, a topology hash). It crafts raw IPv4 packets so it
// can hold each flow's 5-tuple constant across the TTL sweep (stable ECMP) while
// varying only fields outside the hash — the correctness foundation Paris
// traceroute established for load-balanced networks.

import (
	"context"
	"fmt"
	"hash/fnv"
	"math"
	"net"
	"sort"
	"sync"
	"time"

	"golang.org/x/net/ipv4"
)

// RunConfig is one resolved traceroute request.
type RunConfig struct {
	Target       net.IP
	Port         int
	Protocol     string
	MaxHops      int
	ProbesPerHop int
	Flows        int
	Nonce        int
	ProbeTimeout time.Duration
	SendGap      time.Duration
}

// HopNode is one responder (router or destination) observed at a TTL.
type HopNode struct {
	IP        string  `json:"ip"`
	RttAvg    float64 `json:"rtt_avg"`
	RttMin    float64 `json:"rtt_min"`
	RttMax    float64 `json:"rtt_max"`
	LossPct   float64 `json:"loss_pct"`
	Sent      int     `json:"sent"`
	Recv      int     `json:"recv"`
	IsDest    bool    `json:"is_dest"`
	FlowCount int     `json:"flow_count"` // how many flows traversed this node (transit likelihood)
}

// Hop is the set of responders seen at one TTL.
type Hop struct {
	TTL       int       `json:"ttl"`
	Nodes     []HopNode `json:"nodes"`
	Anonymous bool      `json:"anonymous"` // no responder at this TTL but the path continues past it
}

// FlowPath is one ECMP flow's ordered path (IP per TTL, "" for a silent hop).
type FlowPath struct {
	Flow    int      `json:"flow"`
	Path    []string `json:"path"`
	Reached bool     `json:"reached"`
}

// RunResult is the analyzed outcome of a single trace.
type RunResult struct {
	Reached      bool
	HopCount     int
	NumPaths     int
	RttMs        float64
	LossPct      float64
	WorstHopLoss float64
	JitterMs     float64
	DurationMs   int
	PathHash     int64
	Hops         []Hop
	Flows        []FlowPath
	Error        string
}

type stamped struct {
	r reply
	t time.Time
}

type hopAgg struct {
	ip     string
	sent   int
	recv   int
	rtts   []float64
	isDest bool
}

func protoNum(p string) int {
	switch p {
	case ProtoTCP:
		return 6
	case ProtoUDP:
		return 17
	default:
		return 1 // icmp
	}
}

// Trace runs one traceroute and returns the analyzed result. It requires
// CAP_NET_RAW (the poller binary carries it via setcap).
func Trace(ctx context.Context, cfg RunConfig) (*RunResult, error) {
	start := time.Now()
	dst := cfg.Target.To4()
	if dst == nil {
		return nil, fmt.Errorf("netpath: only IPv4 targets are supported")
	}
	src, err := egressIP(cfg.Target, cfg.Port)
	if err != nil {
		return nil, fmt.Errorf("egress ip: %w", err)
	}
	if cfg.MaxHops <= 0 || cfg.MaxHops > 63 {
		cfg.MaxHops = 30
	}
	if cfg.ProbesPerHop <= 0 || cfg.ProbesPerHop > 7 {
		cfg.ProbesPerHop = 3
	}
	if cfg.Flows <= 0 || cfg.Flows > 16 {
		cfg.Flows = 4
	}
	if cfg.ProbeTimeout <= 0 {
		cfg.ProbeTimeout = 2 * time.Second
	}

	// Send socket for the probe protocol; ICMP socket always open (it carries
	// the Time-Exceeded / Unreachable errors for every mode). For TCP mode the
	// destination's SYN-ACK/RST arrives on the TCP raw socket.
	proto := protoNum(cfg.Protocol)
	icmpConn, err := newRawConn("ip4:icmp")
	if err != nil {
		return nil, fmt.Errorf("open icmp socket: %w", err)
	}
	defer icmpConn.Close()

	var sendConn *ipv4.RawConn // where we write probes
	var tcpConn *ipv4.RawConn  // TCP replies (tcp mode only)
	switch cfg.Protocol {
	case ProtoTCP:
		tcpConn, err = newRawConn("ip4:tcp")
		if err != nil {
			return nil, fmt.Errorf("open tcp socket: %w", err)
		}
		defer tcpConn.Close()
		sendConn = tcpConn
	case ProtoUDP:
		sendConn, err = newRawConn("ip4:udp")
		if err != nil {
			return nil, fmt.Errorf("open udp socket: %w", err)
		}
		defer sendConn.Close()
	default:
		sendConn = icmpConn
	}

	// Collect replies concurrently.
	var mu sync.Mutex
	var collected []stamped
	deadline := time.Now().Add(time.Duration(cfg.MaxHops*cfg.Flows)*cfg.SendGap + cfg.ProbeTimeout + 2*time.Second)
	recv := func(conn *ipv4.RawConn, tcp bool) {
		buf := make([]byte, 1500)
		for {
			_ = conn.SetReadDeadline(deadline)
			h, p, _, rerr := conn.ReadFrom(buf)
			if rerr != nil {
				return
			}
			now := time.Now()
			if h == nil {
				continue
			}
			var rp reply
			if tcp {
				rp = parseTCPReply(h.Src, p)
			} else {
				rp = parseICMP(h.Src, p)
			}
			if rp.kind == replyNone {
				continue
			}
			mu.Lock()
			collected = append(collected, stamped{rp, now})
			mu.Unlock()
		}
	}
	go recv(icmpConn, false)
	if tcpConn != nil {
		go recv(tcpConn, true)
	}

	// Send every (flow, ttl, probe) packet, recording send time by token.
	sendTime := make(map[uint16]time.Time, cfg.Flows*cfg.MaxHops*cfg.ProbesPerHop)
	if cfg.SendGap <= 0 {
		cfg.SendGap = time.Millisecond
	}
	for flow := 0; flow < cfg.Flows; flow++ {
		for ttl := 1; ttl <= cfg.MaxHops; ttl++ {
			for pidx := 0; pidx < cfg.ProbesPerHop; pidx++ {
				tok := encodeToken(cfg.Nonce, flow, ttl, pidx)
				payload := buildPayload(cfg, src, dst, flow, ttl, pidx, tok)
				h := &ipv4.Header{
					Version:  4,
					Len:      ipv4.HeaderLen,
					TotalLen: ipv4.HeaderLen + len(payload),
					ID:       int(tok),
					Flags:    ipv4.DontFragment,
					TTL:      ttl,
					Protocol: proto,
					Src:      src.To4(),
					Dst:      dst,
				}
				sendTime[tok] = time.Now()
				if werr := sendConn.WriteTo(h, payload, nil); werr != nil {
					// transient send error — skip this probe
					delete(sendTime, tok)
				}
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-time.After(cfg.SendGap):
				}
			}
		}
	}

	// Wait for stragglers, then stop the receivers.
	select {
	case <-ctx.Done():
	case <-time.After(cfg.ProbeTimeout):
	}
	icmpConn.Close()
	if tcpConn != nil {
		tcpConn.Close()
	}
	if sendConn != icmpConn && sendConn != tcpConn {
		sendConn.Close()
	}
	time.Sleep(20 * time.Millisecond) // let receivers drain
	mu.Lock()
	all := collected
	mu.Unlock()

	res := analyze(cfg, dst, sendTime, all)
	res.DurationMs = int(time.Since(start).Milliseconds())
	return res, nil
}

// buildPayload builds the L4 segment for one probe.
func buildPayload(cfg RunConfig, src, dst net.IP, flow, ttl, pidx int, tok uint16) []byte {
	switch cfg.Protocol {
	case ProtoTCP:
		dport := cfg.Port
		if dport == 0 {
			dport = 80
		}
		return buildTCPSYN(src, dst, flow, dport, tok)
	case ProtoUDP:
		dport := cfg.Port
		if dport == 0 {
			dport = udpDPortBase
		}
		return buildUDP(src, dst, flow, dport)
	default:
		return buildICMPEcho(cfg.Nonce, flow, ttl, pidx)
	}
}

// analyze correlates replies to probes and derives the full RunResult.
func analyze(cfg RunConfig, dst net.IP, sendTime map[uint16]time.Time, replies []stamped) *RunResult {
	dstStr := dst.String()
	// results[flow][ttl] -> aggregate
	results := make(map[int]map[int]*hopAgg)
	ensure := func(flow, ttl int) *hopAgg {
		if results[flow] == nil {
			results[flow] = make(map[int]*hopAgg)
		}
		if results[flow][ttl] == nil {
			results[flow][ttl] = &hopAgg{}
		}
		return results[flow][ttl]
	}
	// seed the "sent" counts for every probe we actually launched
	for tok := range sendTime {
		_, flow, ttl, _ := decodeToken(tok)
		ensure(flow, ttl).sent++
	}
	// fold in replies
	for _, s := range replies {
		st, ok := sendTime[s.r.tok]
		if !ok {
			continue // not ours (or a stale run)
		}
		_, flow, ttl, _ := decodeToken(s.r.tok)
		agg := ensure(flow, ttl)
		agg.recv++
		rtt := s.t.Sub(st).Seconds() * 1000.0
		if rtt < 0 {
			rtt = 0
		}
		agg.rtts = append(agg.rtts, rtt)
		if s.r.dest {
			agg.isDest = true
			agg.ip = dstStr
		} else if agg.ip == "" && s.r.from != nil {
			agg.ip = s.r.from.String()
		}
	}

	res := &RunResult{}

	// Per-flow destination TTL (first TTL the destination answered) and paths.
	flowDestTTL := make(map[int]int)
	maxObservedTTL := 0
	for flow := 0; flow < cfg.Flows; flow++ {
		destTTL := 0
		for ttl := 1; ttl <= cfg.MaxHops; ttl++ {
			a := results[flow][ttl]
			if a == nil {
				continue
			}
			if a.isDest && destTTL == 0 {
				destTTL = ttl
			}
			if a.ip != "" && ttl > maxObservedTTL {
				maxObservedTTL = ttl
			}
		}
		flowDestTTL[flow] = destTTL
	}

	// Effective path length: to the destination if reached, else to the last
	// responding hop across all flows.
	reached := false
	shortestDest := 0
	for flow := 0; flow < cfg.Flows; flow++ {
		if d := flowDestTTL[flow]; d > 0 {
			reached = true
			if shortestDest == 0 || d < shortestDest {
				shortestDest = d
			}
		}
	}
	res.Reached = reached
	limitTTL := shortestDest
	if !reached {
		limitTTL = maxObservedTTL
	}
	if limitTTL == 0 {
		limitTTL = 1
	}

	// Build flow paths (each flow up to its own destination, capped at limit for unreached).
	distinctPaths := map[string]struct{}{}
	for flow := 0; flow < cfg.Flows; flow++ {
		end := flowDestTTL[flow]
		if end == 0 {
			end = limitTTL
		}
		fp := FlowPath{Flow: flow, Reached: flowDestTTL[flow] > 0}
		var sig []string
		for ttl := 1; ttl <= end; ttl++ {
			ip := ""
			if a := results[flow][ttl]; a != nil {
				ip = a.ip
			}
			fp.Path = append(fp.Path, ip)
			sig = append(sig, fmt.Sprintf("%d:%s", ttl, ip))
		}
		res.Flows = append(res.Flows, fp)
		distinctPaths[joinSig(sig)] = struct{}{}
	}
	res.NumPaths = len(distinctPaths)

	// Aggregate per-TTL nodes across flows and compute the topology hash.
	var hashParts []string
	var destRtts []float64
	worstLoss := 0.0
	for ttl := 1; ttl <= limitTTL; ttl++ {
		nodes := map[string]*HopNode{}
		anySeen := false
		for flow := 0; flow < cfg.Flows; flow++ {
			// only count this flow at this ttl if it's at/above the hop within the flow's own path
			if d := flowDestTTL[flow]; d > 0 && ttl > d {
				continue
			}
			a := results[flow][ttl]
			if a == nil {
				continue
			}
			if a.ip == "" {
				continue
			}
			anySeen = true
			n := nodes[a.ip]
			if n == nil {
				n = &HopNode{IP: a.ip, IsDest: a.isDest, RttMin: math.MaxFloat64}
				nodes[a.ip] = n
			}
			n.Sent += a.sent
			n.Recv += a.recv
			n.FlowCount++
			for _, rt := range a.rtts {
				n.RttAvg += rt
				if rt < n.RttMin {
					n.RttMin = rt
				}
				if rt > n.RttMax {
					n.RttMax = rt
				}
				if a.isDest {
					destRtts = append(destRtts, rt)
				}
			}
		}
		hop := Hop{TTL: ttl}
		if !anySeen {
			hop.Anonymous = true // ICMP-rate-limited / silent hop — render grey, not as loss
			res.Hops = append(res.Hops, hop)
			continue
		}
		var ips []string
		for ip := range nodes {
			ips = append(ips, ip)
		}
		sort.Strings(ips)
		for _, ip := range ips {
			n := nodes[ip]
			if n.Recv > 0 {
				n.RttAvg = round2(n.RttAvg / float64(n.Recv))
			}
			if n.RttMin == math.MaxFloat64 {
				n.RttMin = 0
			}
			n.RttMin = round2(n.RttMin)
			n.RttMax = round2(n.RttMax)
			if n.Sent > 0 {
				n.LossPct = round2(100.0 * float64(n.Sent-n.Recv) / float64(n.Sent))
			}
			if !n.IsDest && n.LossPct > worstLoss {
				worstLoss = n.LossPct
			}
			hop.Nodes = append(hop.Nodes, *n)
			hashParts = append(hashParts, fmt.Sprintf("%d:%s", ttl, ip))
		}
		res.Hops = append(res.Hops, hop)
	}

	res.HopCount = limitTTL
	res.WorstHopLoss = round2(worstLoss)
	res.PathHash = topoHash(hashParts)

	// End-to-end metrics.
	if reached {
		if len(destRtts) > 0 {
			res.RttMs = round2(mean(destRtts))
			res.JitterMs = round2(stddev(destRtts))
		}
		// e2e loss = loss of destination probes at the shortest-path flows
		var dsent, drecv int
		for flow := 0; flow < cfg.Flows; flow++ {
			if d := flowDestTTL[flow]; d > 0 {
				if a := results[flow][d]; a != nil {
					dsent += a.sent
					drecv += a.recv
				}
			}
		}
		if dsent > 0 {
			res.LossPct = round2(100.0 * float64(dsent-drecv) / float64(dsent))
		}
	} else {
		res.LossPct = 100.0
	}
	return res
}

func joinSig(parts []string) string {
	out := ""
	for _, p := range parts {
		out += p + "|"
	}
	return out
}

func topoHash(parts []string) int64 {
	sort.Strings(parts)
	h := fnv.New64a()
	for _, p := range parts {
		_, _ = h.Write([]byte(p))
		_, _ = h.Write([]byte{'\n'})
	}
	return int64(h.Sum64())
}

func mean(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	var s float64
	for _, x := range v {
		s += x
	}
	return s / float64(len(v))
}

func stddev(v []float64) float64 {
	if len(v) < 2 {
		return 0
	}
	m := mean(v)
	var s float64
	for _, x := range v {
		s += (x - m) * (x - m)
	}
	return math.Sqrt(s / float64(len(v)-1))
}

func round2(f float64) float64 {
	return math.Round(f*100) / 100
}

// newRawConn opens a raw IPv4 socket (network e.g. "ip4:icmp") with IP_HDRINCL,
// so we control the outgoing IP header (TTL and the ID that carries the token).
func newRawConn(network string) (*ipv4.RawConn, error) {
	c, err := net.ListenPacket(network, "0.0.0.0")
	if err != nil {
		return nil, err
	}
	r, err := ipv4.NewRawConn(c)
	if err != nil {
		c.Close()
		return nil, err
	}
	return r, nil
}

// egressIP returns the local source address the kernel would use to reach the
// target — needed to build correct IP headers and L4 pseudo-header checksums.
func egressIP(target net.IP, port int) (net.IP, error) {
	p := port
	if p == 0 {
		p = 33434
	}
	c, err := net.Dial("udp", net.JoinHostPort(target.String(), fmt.Sprintf("%d", p)))
	if err != nil {
		return nil, err
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).IP, nil
}
