// Package sflow decodes sFlow version 5 datagrams (sflow.org
// sflow_version_5.txt) into the collector's flow records and interface
// counter samples.
//
// sFlow is packet sampling, not flow export: every flow sample describes ONE
// packet chosen 1-in-N, carrying its first bytes. Each sample becomes one
// netflow.Record with Packets = 1 and Bytes = the frame length (the collector
// multiplies both by the sampling rate like any sampled record, so the stored
// estimate is N packets / N x frame bytes), SamplingInterval = N and
// FieldSampledSFlow set so readers can tell a sampled packet from a flow.
package sflow

import (
	"fmt"
	"net"
	"net/netip"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// Version is the only sFlow version decoded (v2/v4 datagrams use a different
// layout and are rejected as parse errors).
const Version = 5

// Sample types (data_format with enterprise 0).
const (
	sampleFlow             = 1
	sampleCounters         = 2
	sampleFlowExpanded     = 3
	sampleCountersExpanded = 4
)

// Flow record formats.
const (
	flowSampledHeader = 1
	flowSampledIPv4   = 3
	flowSampledIPv6   = 4
	flowExtRouter     = 1002
	flowExtGateway    = 1003
)

// counterGenericIf is the generic interface counters record (RFC 2863 ifTable).
const counterGenericIf = 1

// Interface sentinels: the "packet originated/terminated in the device
// itself" value of the compact (30-bit) and expanded encodings.
const (
	ifInternalCompact  = 0x3FFFFFFF
	ifInternalExpanded = 0xFFFFFFFF
)

// Datagram is one decoded sFlow datagram.
type Datagram struct {
	// Agent is the agent_address the datagram declares; invalid when the
	// agent sent the "unknown" address type.
	Agent netip.Addr
	// Exporter is the identity every record and statistic uses: the agent
	// address (sFlow's own notion of the device, typically its management
	// address, stable whichever interface the datagram leaves from), or the
	// UDP source when the agent declared none.
	Exporter   netip.Addr
	SubAgentID uint32 // observation domain of the records
	Sequence   uint32 // datagram sequence number, per agent and sub-agent
	UptimeMS   uint32 // agent uptime when the datagram was sent

	FlowSamples    int              // flow samples in the datagram (compact and expanded)
	CounterSamples int              // counter samples in the datagram
	Records        []netflow.Record // one per flow sample that carried an IP packet
	Unsupported    int              // flow samples without a decodable IP packet (non-IP frames)
	Malformed      int              // samples whose content was corrupt (skipped)
	Counters       []IfCounters     // generic interface counters from the counter samples
}

// IfCounters is one generic interface counters record. Counters are the
// agent's cumulative values as sent (32-bit ones wrap); readers compute rates
// from consecutive samples.
type IfCounters struct {
	IfIndex         uint32
	IfType          uint32
	Speed           uint64 // bits per second
	Direction       uint32 // 0 unknown, 1 full-duplex, 2 half-duplex, 3 in, 4 out
	AdminUp         bool
	OperUp          bool
	InOctets        uint64
	InUcast         uint32
	InMulticast     uint32
	InBroadcast     uint32
	InDiscards      uint32
	InErrors        uint32
	InUnknownProtos uint32
	OutOctets       uint64
	OutUcast        uint32
	OutMulticast    uint32
	OutBroadcast    uint32
	OutDiscards     uint32
	OutErrors       uint32
	Promiscuous     uint32
}

// Decode parses one datagram received from the UDP source from. A nil
// Datagram means the header itself was unusable. A non-nil Datagram with an
// error means some samples were decoded and a later one was malformed or cut
// off: the decoded samples are good (each sample is length-framed) and are
// kept, the error is counted as a parse error.
func Decode(data []byte, from netip.Addr, collectorID string, receivedAt time.Time) (*Datagram, error) {
	r := &reader{b: data}
	if v := r.u32(); r.err != nil {
		return nil, fmt.Errorf("sflow datagram too short: %d bytes", len(data))
	} else if v != Version {
		return nil, fmt.Errorf("unsupported sflow version %d", v)
	}
	dg := &Datagram{Agent: r.addr()}
	dg.SubAgentID = r.u32()
	dg.Sequence = r.u32()
	dg.UptimeMS = r.u32()
	n := r.count(8)
	if r.err != nil {
		return nil, fmt.Errorf("sflow datagram header: %w", r.err)
	}
	dg.Agent = netflow.CanonicalAddr(dg.Agent)
	dg.Exporter = dg.Agent
	if !dg.Exporter.IsValid() || dg.Exporter.IsUnspecified() {
		dg.Exporter = netflow.CanonicalAddr(from)
	}

	d := decoder{dg: dg, collectorID: collectorID, receivedAt: receivedAt.UTC(), exporterIP: netflow.ExporterStorageIP(dg.Exporter)}
	for i := 0; i < n; i++ {
		format := r.u32()
		body := r.opaque()
		if r.err != nil {
			return dg, fmt.Errorf("sflow sample %d of %d: %w", i+1, n, r.err)
		}
		if format>>12 != 0 { // enterprise-specific sample type
			continue
		}
		var err error
		switch format & 0xFFF {
		case sampleFlow, sampleFlowExpanded:
			dg.FlowSamples++
			err = d.flowSample(body, format&0xFFF == sampleFlowExpanded)
		case sampleCounters, sampleCountersExpanded:
			dg.CounterSamples++
			err = d.counterSample(body, format&0xFFF == sampleCountersExpanded)
		}
		if err != nil {
			dg.Malformed++
		}
	}
	if dg.Malformed > 0 {
		return dg, fmt.Errorf("%d malformed sflow samples", dg.Malformed)
	}
	return dg, nil
}

type decoder struct {
	dg          *Datagram
	collectorID string
	receivedAt  time.Time
	exporterIP  net.IP
}

// compactIf decodes the 32-bit interface encoding: the top two bits are the
// format (0 = a single ifIndex, 1 = packet discarded, 2 = several output
// interfaces), the rest the value. Only a real ifIndex is reported.
func compactIf(v uint32) (uint32, bool) {
	value := v & 0x3FFFFFFF
	return value, v>>30 == 0 && value != 0 && value != ifInternalCompact
}

func expandedIf(format, value uint32) (uint32, bool) {
	return value, format == 0 && value != 0 && value != ifInternalExpanded
}

// flowState collects the records of one flow sample.
type flowState struct {
	header      packetInfo
	frameLength uint32
	hasHeader   bool

	ip       packetInfo // sampled_ipv4 / sampled_ipv6 (agents that send these instead of headers)
	ipLength uint32

	nextHop        netip.Addr // extended_router (IGP next hop)
	gatewayNextHop netip.Addr // extended_gateway (BGP next hop)
	srcMask        uint8
	dstMask        uint8
	hasMasks       bool
	srcAS, dstAS   uint32
	hasAS          bool
}

func (d *decoder) flowSample(b []byte, expanded bool) error {
	r := &reader{b: b}
	r.u32() // sample sequence number
	r.u32() // source id (type<<24 | index, or type then index when expanded)
	if expanded {
		r.u32()
	}
	rate := r.u32()
	r.u32() // sample_pool
	r.u32() // drops
	var (
		in, out     uint32
		inOK, outOK bool
	)
	if expanded {
		in, inOK = expandedIf(r.u32(), r.u32())
		out, outOK = expandedIf(r.u32(), r.u32())
	} else {
		in, inOK = compactIf(r.u32())
		out, outOK = compactIf(r.u32())
	}
	n := r.count(8)
	if r.err != nil {
		return r.err
	}
	var fs flowState
	for i := 0; i < n; i++ {
		format := r.u32()
		data := r.opaque()
		if r.err != nil {
			return r.err
		}
		if format>>12 != 0 {
			continue
		}
		var err error
		switch format & 0xFFF {
		case flowSampledHeader:
			err = fs.sampledHeader(data)
		case flowSampledIPv4:
			err = fs.sampledIP(data, false)
		case flowSampledIPv6:
			err = fs.sampledIP(data, true)
		case flowExtRouter:
			err = fs.extRouter(data)
		case flowExtGateway:
			err = fs.extGateway(data)
		}
		if err != nil {
			return err
		}
	}

	pkt, bytes := fs.header, uint64(fs.frameLength)
	if !pkt.ok {
		pkt = fs.ip
		if !fs.hasHeader {
			bytes = uint64(fs.ipLength)
		}
	}
	if !pkt.ok {
		// No IP packet: nothing to attribute the sample to. It is counted,
		// never stored with made-up endpoints.
		d.dg.Unsupported++
		return nil
	}
	if rate == 0 {
		rate = 1 // not a valid rate; the sample stands for itself
	}
	rec := netflow.Record{
		Timestamp:         d.receivedAt, // a sample is taken when forwarded: sFlow carries no flow times
		ReceivedAt:        d.receivedAt,
		CollectorID:       d.collectorID,
		ExporterIP:        d.exporterIP,
		ExporterAddr:      d.dg.Exporter,
		FlowVersion:       0,
		FlowSequence:      d.dg.Sequence,
		SamplingInterval:  rate,
		Packets:           1,
		Bytes:             bytes,
		Protocol:          pkt.proto,
		TOS:               pkt.tos,
		ObservationDomain: d.dg.SubAgentID,
		FlowDirection:     netflow.DirectionUnknown,
		ExportProtocol:    netflow.ProtocolSFlow5,
		FieldsPresent:     netflow.FieldTracked | netflow.FieldSampledSFlow | netflow.FieldTOS,
	}
	if pkt.v6 {
		rec.SrcAddr6, rec.DstAddr6, rec.IPVersion = net.IP(clone(pkt.src6[:])), net.IP(clone(pkt.dst6[:])), 6
	} else {
		rec.SrcAddr, rec.DstAddr, rec.IPVersion = net.IP(clone(pkt.src4[:])), net.IP(clone(pkt.dst4[:])), 4
	}
	if pkt.hasPorts {
		rec.SrcPort, rec.DstPort = pkt.srcPort, pkt.dstPort
		rec.FieldsPresent |= netflow.FieldL4Ports
	}
	if pkt.hasFlags {
		rec.TCPFlags = pkt.tcpFlags
		rec.FieldsPresent |= netflow.FieldTCPFlags
	}
	if pkt.hasICMP {
		rec.SrcPort, rec.DstPort = 0, pkt.icmp
		rec.FieldsPresent |= netflow.FieldICMPType
	}
	if inOK {
		rec.InputSNMP = in
		rec.FieldsPresent |= netflow.FieldInputIf
	}
	if outOK {
		rec.OutputSNMP = out
		rec.FieldsPresent |= netflow.FieldOutputIf
	}
	if fs.hasMasks {
		rec.SrcMask, rec.DstMask = fs.srcMask, fs.dstMask
		rec.FieldsPresent |= netflow.FieldMasks
	}
	if fs.hasAS {
		rec.SrcAS, rec.DstAS = fs.srcAS, fs.dstAS
		rec.FieldsPresent |= netflow.FieldAS
	}
	// The IGP next hop (extended_router) wins over the BGP one, as for NetFlow.
	nh := fs.nextHop
	if !nh.IsValid() || nh.IsUnspecified() {
		nh = fs.gatewayNextHop
	}
	if nh.IsValid() && !nh.IsUnspecified() {
		if nh = nh.Unmap(); nh.Is4() {
			b := nh.As4()
			rec.NextHop = net.IP(b[:])
		} else {
			b := nh.As16()
			rec.NextHop6 = net.IP(b[:])
		}
		rec.FieldsPresent |= netflow.FieldNextHop
	}
	d.dg.Records = append(d.dg.Records, rec)
	return nil
}

func clone(b []byte) []byte { return append([]byte(nil), b...) }

// sampledHeader: header_protocol, frame_length, stripped, header bytes.
func (fs *flowState) sampledHeader(b []byte) error {
	r := &reader{b: b}
	proto := r.u32()
	frameLength := r.u32()
	r.u32() // stripped (e.g. the FCS)
	hdr := r.opaque()
	if r.err != nil {
		return r.err
	}
	fs.header, fs.frameLength, fs.hasHeader = parseHeader(proto, hdr), frameLength, true
	return nil
}

// sampledIP: sampled_ipv4 / sampled_ipv6 — the decoded IP fields some agents
// send instead of (or besides) the raw header.
func (fs *flowState) sampledIP(b []byte, v6 bool) error {
	r := &reader{b: b}
	length := r.u32()
	proto := r.u32()
	var p packetInfo
	if v6 {
		copy(p.src6[:], r.fixed(16))
		copy(p.dst6[:], r.fixed(16))
	} else {
		copy(p.src4[:], r.fixed(4))
		copy(p.dst4[:], r.fixed(4))
	}
	srcPort, dstPort, flags, tos := r.u32(), r.u32(), r.u32(), r.u32()
	if r.err != nil {
		return r.err
	}
	p.ok, p.v6, p.proto, p.tos = true, v6, uint8(proto), uint8(tos)
	switch p.proto {
	case 6:
		p.srcPort, p.dstPort, p.hasPorts = uint16(srcPort), uint16(dstPort), true
		p.tcpFlags, p.hasFlags = uint8(flags), true
	case 17, 33, 132, 136:
		p.srcPort, p.dstPort, p.hasPorts = uint16(srcPort), uint16(dstPort), true
	}
	fs.ip, fs.ipLength = p, length
	return nil
}

// extRouter: next hop, source and destination prefix lengths.
func (fs *flowState) extRouter(b []byte) error {
	r := &reader{b: b}
	nh := r.addr()
	src, dst := r.u32(), r.u32()
	if r.err != nil {
		return r.err
	}
	fs.nextHop = nh
	fs.srcMask, fs.dstMask, fs.hasMasks = uint8(min(src, 128)), uint8(min(dst, 128)), true
	return nil
}

// extGateway: BGP next hop, the router's AS, the source AS, the source peer
// AS, the AS path to the destination, communities and local preference.
// The destination AS is the last one of the path (its origin); a source AS of
// 0 or an empty path means the traffic starts or ends in the router's own AS,
// which then stands in (the convention goflow2 uses).
func (fs *flowState) extGateway(b []byte) error {
	r := &reader{b: b}
	nh := r.addr()
	routerAS, srcAS := r.u32(), r.u32()
	r.u32() // source peer AS
	dstAS, hasPath := routerAS, false
	for segs := r.count(8); segs > 0 && r.err == nil; segs-- {
		r.u32() // segment type: AS_SET or AS_SEQUENCE
		for n := r.count(4); n > 0 && r.err == nil; n-- {
			dstAS, hasPath = r.u32(), true
		}
	}
	for n := r.count(4); n > 0 && r.err == nil; n-- {
		r.u32() // community
	}
	r.u32() // local preference
	if r.err != nil {
		return r.err
	}
	if srcAS == 0 {
		srcAS = routerAS
	}
	if !hasPath {
		dstAS = routerAS
	}
	fs.gatewayNextHop = nh
	fs.srcAS, fs.dstAS, fs.hasAS = srcAS, dstAS, true
	return nil
}

func (d *decoder) counterSample(b []byte, expanded bool) error {
	r := &reader{b: b}
	r.u32() // sample sequence number
	r.u32() // source id
	if expanded {
		r.u32()
	}
	n := r.count(8)
	for i := 0; i < n && r.err == nil; i++ {
		format := r.u32()
		data := r.opaque()
		if r.err != nil || format != counterGenericIf {
			continue
		}
		c, err := parseIfCounters(data)
		if err != nil {
			return err
		}
		d.dg.Counters = append(d.dg.Counters, c)
	}
	return r.err
}

// parseIfCounters decodes the 88-byte generic interface counters record.
func parseIfCounters(b []byte) (IfCounters, error) {
	r := &reader{b: b}
	c := IfCounters{IfIndex: r.u32(), IfType: r.u32(), Speed: r.u64(), Direction: r.u32()}
	status := r.u32()
	c.AdminUp, c.OperUp = status&0x1 != 0, status&0x2 != 0
	c.InOctets = r.u64()
	c.InUcast, c.InMulticast, c.InBroadcast = r.u32(), r.u32(), r.u32()
	c.InDiscards, c.InErrors, c.InUnknownProtos = r.u32(), r.u32(), r.u32()
	c.OutOctets = r.u64()
	c.OutUcast, c.OutMulticast, c.OutBroadcast = r.u32(), r.u32(), r.u32()
	c.OutDiscards, c.OutErrors, c.Promiscuous = r.u32(), r.u32(), r.u32()
	return c, r.err
}
