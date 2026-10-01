package sflow

import (
	"encoding/binary"
	"math/rand/v2"
	"net"
	"net/netip"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// Golden datagrams are built here with a small XDR encoder following
// sflow_version_5.txt field by field, so each test states exactly what the
// agent sent.

var testReceived = time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC)

type xdr struct{ b []byte }

func (x *xdr) u32(v uint32) *xdr { x.b = binary.BigEndian.AppendUint32(x.b, v); return x }
func (x *xdr) u64(v uint64) *xdr { x.b = binary.BigEndian.AppendUint64(x.b, v); return x }

func (x *xdr) opaque(b []byte) *xdr {
	x.u32(uint32(len(b)))
	x.b = append(x.b, b...)
	for len(x.b)%4 != 0 {
		x.b = append(x.b, 0)
	}
	return x
}

// addr encodes an sFlow address; "" is the unknown address type.
func (x *xdr) addr(s string) *xdr {
	if s == "" {
		return x.u32(addrUnknown)
	}
	a := netip.MustParseAddr(s)
	if a.Is4() {
		b := a.As4()
		x.u32(addrIPv4)
		x.b = append(x.b, b[:]...)
		return x
	}
	b := a.As16()
	x.u32(addrIPv6)
	x.b = append(x.b, b[:]...)
	return x
}

// record is one (format, data) element of a sample or flow-record list.
type record struct {
	format uint32
	data   []byte
}

func datagram(agent string, subAgent, seq uint32, samples ...record) []byte {
	x := (&xdr{}).u32(5).addr(agent).u32(subAgent).u32(seq).u32(3_600_000).u32(uint32(len(samples)))
	for _, s := range samples {
		x.u32(s.format).opaque(s.data)
	}
	return x.b
}

func flowSample(rate, in, out uint32, recs ...record) record {
	x := (&xdr{}).u32(1).u32(5).u32(rate).u32(rate * 10).u32(0).u32(in).u32(out).u32(uint32(len(recs)))
	for _, r := range recs {
		x.u32(r.format).opaque(r.data)
	}
	return record{sampleFlow, x.b}
}

func expandedFlowSample(rate, inFmt, in, outFmt, out uint32, recs ...record) record {
	x := (&xdr{}).u32(1).u32(0).u32(5).u32(rate).u32(rate * 10).u32(0).u32(inFmt).u32(in).u32(outFmt).u32(out).u32(uint32(len(recs)))
	for _, r := range recs {
		x.u32(r.format).opaque(r.data)
	}
	return record{sampleFlowExpanded, x.b}
}

func headerRecord(proto, frameLength uint32, hdr []byte) record {
	return record{flowSampledHeader, (&xdr{}).u32(proto).u32(frameLength).u32(4).opaque(hdr).b}
}

func routerRecord(nextHop string, srcMask, dstMask uint32) record {
	return record{flowExtRouter, (&xdr{}).addr(nextHop).u32(srcMask).u32(dstMask).b}
}

func gatewayRecord(nextHop string, routerAS, srcAS uint32, path ...uint32) record {
	x := (&xdr{}).addr(nextHop).u32(routerAS).u32(srcAS).u32(0)
	if len(path) == 0 {
		x.u32(0)
	} else {
		x.u32(1).u32(2).u32(uint32(len(path))) // one AS_SEQUENCE segment
		for _, as := range path {
			x.u32(as)
		}
	}
	x.u32(1).u32(0xFFFF0001) // one community
	x.u32(100)               // local preference
	return record{flowExtGateway, x.b}
}

// ── packet builders ──────────────────────────────────────────────────────

func ethernet(etherType uint16, vlans []uint16, payload []byte) []byte {
	h := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
	for _, tpid := range vlans {
		h = binary.BigEndian.AppendUint16(h, tpid)
		h = binary.BigEndian.AppendUint16(h, 100) // VLAN 100
	}
	h = binary.BigEndian.AppendUint16(h, etherType)
	return append(h, payload...)
}

func ipv4(tos, proto byte, src, dst string, fragOffset uint16, payload []byte) []byte {
	h := make([]byte, 20)
	h[0], h[1], h[9] = 0x45, tos, proto
	binary.BigEndian.PutUint16(h[2:4], uint16(20+len(payload)))
	binary.BigEndian.PutUint16(h[6:8], fragOffset)
	h[8] = 64
	s, d := netip.MustParseAddr(src).As4(), netip.MustParseAddr(dst).As4()
	copy(h[12:16], s[:])
	copy(h[16:20], d[:])
	return append(h, payload...)
}

func ipv6(tc, next byte, src, dst string, payload []byte) []byte {
	h := make([]byte, 40)
	h[0], h[1] = 0x60|tc>>4, tc<<4
	binary.BigEndian.PutUint16(h[4:6], uint16(len(payload)))
	h[6], h[7] = next, 64
	s, d := netip.MustParseAddr(src).As16(), netip.MustParseAddr(dst).As16()
	copy(h[8:24], s[:])
	copy(h[24:40], d[:])
	return append(h, payload...)
}

func tcp(sport, dport uint16, flags byte) []byte {
	h := make([]byte, 20)
	binary.BigEndian.PutUint16(h[0:2], sport)
	binary.BigEndian.PutUint16(h[2:4], dport)
	h[12], h[13] = 5<<4, flags
	return h
}

func udp(sport, dport uint16) []byte {
	h := make([]byte, 8)
	binary.BigEndian.PutUint16(h[0:2], sport)
	binary.BigEndian.PutUint16(h[2:4], dport)
	return h
}

func decode(t *testing.T, b []byte) *Datagram {
	t.Helper()
	dg, err := Decode(b, netip.MustParseAddr("192.0.2.250"), "test", testReceived)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	return dg
}

// ── tests ────────────────────────────────────────────────────────────────

func TestDecodeFlowSampleEthernetVLANTCP(t *testing.T) {
	hdr := ethernet(etherTypeIPv4, []uint16{etherTypeVLAN}, ipv4(0xB8, 6, "10.20.1.10", "142.250.74.46", 0x4000, tcp(51000, 443, 0x12)))
	dg := decode(t, datagram("192.0.2.100", 3, 77, flowSample(512, 5, 7,
		headerRecord(headerEthernet, 1518, hdr),
		routerRecord("10.0.0.1", 24, 16),
		gatewayRecord("10.0.0.254", 65000, 0, 3356, 15169),
	)))
	if dg.Exporter != netip.MustParseAddr("192.0.2.100") || dg.SubAgentID != 3 || dg.Sequence != 77 || dg.FlowSamples != 1 || len(dg.Records) != 1 {
		t.Fatalf("datagram: %+v", dg)
	}
	r := dg.Records[0]
	if r.Packets != 1 || r.Bytes != 1518 || r.SamplingInterval != 512 {
		t.Fatalf("one sampled packet (the collector scales by the rate): packets=%d bytes=%d rate=%d", r.Packets, r.Bytes, r.SamplingInterval)
	}
	if r.SrcAddr.String() != "10.20.1.10" || r.DstAddr.String() != "142.250.74.46" || r.IPVersion != 4 || r.SrcAddr6 != nil {
		t.Fatalf("addresses: %+v", r)
	}
	if r.Protocol != 6 || r.SrcPort != 51000 || r.DstPort != 443 || r.TCPFlags != 0x12 || r.TOS != 0xB8 {
		t.Fatalf("5-tuple/flags/tos: %+v", r)
	}
	if r.InputSNMP != 5 || r.OutputSNMP != 7 || r.NextHop.String() != "10.0.0.1" || r.SrcMask != 24 || r.DstMask != 16 {
		t.Fatalf("interfaces/next hop/masks: %+v", r)
	}
	// Source AS 0 = the router's own AS; the destination AS is the path origin.
	if r.SrcAS != 65000 || r.DstAS != 15169 {
		t.Fatalf("AS: src=%d dst=%d", r.SrcAS, r.DstAS)
	}
	if r.FlowVersion != 0 || r.ExportProtocol != netflow.ProtocolSFlow5 || r.ObservationDomain != 3 || r.FlowSequence != 77 ||
		!r.Timestamp.Equal(testReceived) || r.ExporterIP.String() != "192.0.2.100" || r.FlowDirection != netflow.DirectionUnknown {
		t.Fatalf("identity/time: %+v", r)
	}
	want := netflow.FieldTracked | netflow.FieldSampledSFlow | netflow.FieldTOS | netflow.FieldL4Ports | netflow.FieldTCPFlags |
		netflow.FieldInputIf | netflow.FieldOutputIf | netflow.FieldMasks | netflow.FieldAS | netflow.FieldNextHop
	if r.FieldsPresent != want {
		t.Fatalf("fields_present %#x want %#x", r.FieldsPresent, want)
	}
}

func TestDecodeExpandedIPv6(t *testing.T) {
	hopByHop := append([]byte{17, 0}, make([]byte, 6)...) // next = UDP, 8 bytes
	pkt := ipv6(0x28, 0, "2001:db8:20::10", "2001:db8:ffff::443", append(hopByHop, udp(5353, 53)...))
	hdr := ethernet(etherTypeIPv6, []uint16{etherTypeQinQ, etherTypeVLAN}, pkt)
	dg := decode(t, datagram("2001:db8::100", 0, 1, expandedFlowSample(2048, 0, 70000, 0, ifInternalExpanded,
		headerRecord(headerEthernet, 120, hdr),
		gatewayRecord("2001:db8::1", 65000, 64512),
	)))
	r := dg.Records[0]
	if r.IPVersion != 6 || r.SrcAddr6.String() != "2001:db8:20::10" || r.DstAddr6.String() != "2001:db8:ffff::443" || r.SrcAddr != nil {
		t.Fatalf("IPv6 addresses: %+v", r)
	}
	if r.Protocol != 17 || r.SrcPort != 5353 || r.DstPort != 53 || r.TOS != 0x28 || r.FieldsPresent&netflow.FieldTCPFlags != 0 {
		t.Fatalf("UDP behind a hop-by-hop header: %+v", r)
	}
	// 32-bit ifIndex from the expanded encoding; "internal" is not an interface.
	if r.InputSNMP != 70000 || r.OutputSNMP != 0 || r.FieldsPresent&netflow.FieldOutputIf != 0 {
		t.Fatalf("interfaces: in=%d out=%d fields=%#x", r.InputSNMP, r.OutputSNMP, r.FieldsPresent)
	}
	// No path: the destination is in the router's own AS; the BGP next hop is used.
	if r.SrcAS != 64512 || r.DstAS != 65000 || r.NextHop6.String() != "2001:db8::1" || r.NextHop != nil {
		t.Fatalf("gateway: %+v", r)
	}
	if r.ExporterIP != nil || r.ExporterAddr != netip.MustParseAddr("2001:db8::100") || r.SamplingInterval != 2048 {
		t.Fatalf("IPv6 agent identity: %+v", r)
	}
}

func TestDecodeICMPFragmentsMPLSAndSampledIP(t *testing.T) {
	icmp := decode(t, datagram("192.0.2.1", 0, 1, flowSample(100, 1, 2,
		headerRecord(headerIPv4, 98, ipv4(0, 1, "10.0.0.1", "10.0.0.2", 0, []byte{8, 0, 0, 0})))))
	if r := icmp.Records[0]; r.DstPort != 0x0800 || r.SrcPort != 0 || r.FieldsPresent&netflow.FieldICMPType == 0 || r.FieldsPresent&netflow.FieldL4Ports != 0 {
		t.Fatalf("ICMP echo request: %+v", r)
	}

	// A later IPv4 fragment has no transport header: no ports, never guessed.
	frag := decode(t, datagram("192.0.2.1", 0, 2, flowSample(100, 1, 2,
		headerRecord(headerIPv4, 1500, ipv4(0, 17, "10.0.0.1", "10.0.0.2", 185, udp(1, 2))))))
	if r := frag.Records[0]; r.SrcPort != 0 || r.DstPort != 0 || r.FieldsPresent&netflow.FieldL4Ports != 0 || r.Protocol != 17 {
		t.Fatalf("non-first fragment: %+v", r)
	}

	// Two MPLS labels, then IPv4/UDP.
	labels := []byte{0, 0x10, 0x00, 64, 0, 0x20, 0x01, 64}
	mpls := decode(t, datagram("192.0.2.1", 0, 3, flowSample(100, 1, 2,
		headerRecord(headerEthernet, 200, ethernet(etherTypeMPLS, nil, append(labels, ipv4(0, 17, "10.9.0.1", "10.9.0.2", 0, udp(4000, 4789))...))))))
	if r := mpls.Records[0]; r.SrcAddr.String() != "10.9.0.1" || r.DstPort != 4789 {
		t.Fatalf("MPLS: %+v", r)
	}

	// sampled_ipv4 instead of a header: the IP length is the size.
	ip := (&xdr{}).u32(1400).u32(6).b
	ip = append(ip, 10, 1, 1, 1, 10, 2, 2, 2)
	ip = (&xdr{b: ip}).u32(40000).u32(22).u32(0x18).u32(0x10).b
	sampled := decode(t, datagram("192.0.2.1", 0, 4, flowSample(10, 1, 2, record{flowSampledIPv4, ip})))
	if r := sampled.Records[0]; r.Bytes != 1400 || r.SrcAddr.String() != "10.1.1.1" || r.DstPort != 22 || r.TCPFlags != 0x18 || r.TOS != 0x10 {
		t.Fatalf("sampled_ipv4: %+v", r)
	}
}

// A sampled frame that is not IP (ARP here) has no endpoints: counted as
// unsupported, never stored as a 0.0.0.0 flow.
func TestDecodeNonIPFrameUnsupported(t *testing.T) {
	arp := ethernet(0x0806, nil, make([]byte, 28))
	dg := decode(t, datagram("192.0.2.1", 0, 1,
		flowSample(100, 1, 2, headerRecord(headerEthernet, 64, arp)),
		flowSample(100, 1, 2, headerRecord(headerEthernet, 64, ethernet(etherTypeIPv4, nil, ipv4(0, 17, "10.0.0.1", "10.0.0.2", 0, udp(1, 2))))),
	))
	if dg.FlowSamples != 2 || dg.Unsupported != 1 || len(dg.Records) != 1 {
		t.Fatalf("flow samples=%d unsupported=%d records=%d", dg.FlowSamples, dg.Unsupported, len(dg.Records))
	}
}

func ifCountersRecord(ifIndex uint32, inOctets, outOctets uint64) []byte {
	x := (&xdr{}).u32(ifIndex).u32(6).u64(10_000_000_000).u32(1).u32(3)
	x.u64(inOctets).u32(1000).u32(20).u32(30).u32(4).u32(5).u32(6)
	x.u64(outOctets).u32(2000).u32(21).u32(31).u32(7).u32(8).u32(0)
	return x.b
}

func TestDecodeCounterSamples(t *testing.T) {
	compact := (&xdr{}).u32(9).u32(12).u32(2).
		u32(counterGenericIf).opaque(ifCountersRecord(12, 1<<40, 5_000)).
		u32(4413<<12 | 1).opaque([]byte{1, 2, 3, 4}).b // enterprise record: skipped
	expanded := (&xdr{}).u32(10).u32(0).u32(70001).u32(1).
		u32(counterGenericIf).opaque(ifCountersRecord(70001, 1, 2)).b
	dg := decode(t, datagram("192.0.2.1", 1, 5, record{sampleCounters, compact}, record{sampleCountersExpanded, expanded}))
	if dg.CounterSamples != 2 || len(dg.Counters) != 2 || len(dg.Records) != 0 {
		t.Fatalf("counter samples=%d counters=%d records=%d", dg.CounterSamples, len(dg.Counters), len(dg.Records))
	}
	c := dg.Counters[0]
	want := IfCounters{IfIndex: 12, IfType: 6, Speed: 10_000_000_000, Direction: 1, AdminUp: true, OperUp: true,
		InOctets: 1 << 40, InUcast: 1000, InMulticast: 20, InBroadcast: 30, InDiscards: 4, InErrors: 5, InUnknownProtos: 6,
		OutOctets: 5_000, OutUcast: 2000, OutMulticast: 21, OutBroadcast: 31, OutDiscards: 7, OutErrors: 8}
	if c != want {
		t.Fatalf("if_counters:\n got %+v\nwant %+v", c, want)
	}
	if dg.Counters[1].IfIndex != 70001 {
		t.Fatalf("expanded counter sample: %+v", dg.Counters[1])
	}
}

func TestDecodeIdentityAndErrors(t *testing.T) {
	// No agent address: the UDP source is the exporter.
	dg := decode(t, datagram("", 0, 1))
	if dg.Exporter != netip.MustParseAddr("192.0.2.250") || dg.Agent.IsValid() {
		t.Fatalf("unknown agent address: %+v", dg)
	}
	if _, err := Decode(append([]byte{0, 0, 0, 4}, datagram("192.0.2.1", 0, 1)[4:]...), netip.MustParseAddr("192.0.2.250"), "t", testReceived); err == nil {
		t.Fatal("sFlow v4 must be rejected")
	}
	if dg, err := Decode([]byte{0, 0, 0, 5, 0, 0, 0, 1}, netip.MustParseAddr("192.0.2.250"), "t", testReceived); err == nil || dg != nil {
		t.Fatalf("truncated header: %v %+v", err, dg)
	}

	// One corrupt sample (an impossible record count) between two good ones:
	// the good ones are kept, the datagram reports the error.
	bad := (&xdr{}).u32(1).u32(5).u32(100).u32(1000).u32(0).u32(1).u32(2).u32(0xFFFFFF).b
	good := flowSample(100, 1, 2, headerRecord(headerIPv4, 60, ipv4(0, 6, "10.0.0.1", "10.0.0.2", 0, tcp(1, 2, 0x02))))
	dg, err := Decode(datagram("192.0.2.1", 0, 9, good, record{sampleFlow, bad}, good, record{4413<<12 | 1, []byte{9, 9, 9, 9}}),
		netip.MustParseAddr("192.0.2.250"), "t", testReceived)
	if err == nil || dg == nil || len(dg.Records) != 2 || dg.Malformed != 1 || dg.FlowSamples != 3 {
		t.Fatalf("partial decode: err=%v dg=%+v", err, dg)
	}
}

func checkRecords(t *testing.T, dg *Datagram) {
	t.Helper()
	if dg == nil {
		return
	}
	for _, r := range dg.Records {
		v4 := len(r.SrcAddr) == 4 && len(r.DstAddr) == 4 && r.SrcAddr6 == nil && r.IPVersion == 4
		v6 := len(r.SrcAddr6) == 16 && len(r.DstAddr6) == 16 && r.SrcAddr == nil && r.IPVersion == 6
		if !v4 && !v6 {
			t.Fatalf("address invariant: %+v", r)
		}
		for _, ip := range []net.IP{r.NextHop, r.NextHop6} {
			if ip != nil && len(ip) != 4 && len(ip) != 16 {
				t.Fatalf("next hop: %+v", r)
			}
		}
		if r.SamplingInterval == 0 || r.Packets != 1 || r.FieldsPresent&netflow.FieldSampledSFlow == 0 {
			t.Fatalf("sample shape: %+v", r)
		}
	}
}

func corpus() []byte {
	hdr := ethernet(etherTypeIPv6, []uint16{etherTypeVLAN}, ipv6(0, 6, "2001:db8::1", "2001:db8::2", tcp(1, 2, 0x11)))
	counters := (&xdr{}).u32(1).u32(3).u32(1).u32(counterGenericIf).opaque(ifCountersRecord(3, 10, 20)).b
	return datagram("192.0.2.1", 2, 3,
		flowSample(64, 3, 4, headerRecord(headerEthernet, 90, hdr), routerRecord("2001:db8::fe", 64, 48), gatewayRecord("", 1, 2, 3, 4)),
		expandedFlowSample(64, 0, 5, 1, 9, headerRecord(headerIPv4, 60, ipv4(0, 1, "10.0.0.1", "10.0.0.2", 0, []byte{3, 1}))),
		record{sampleCounters, counters},
	)
}

func TestDecodeSurvivesTruncationAndCorruption(t *testing.T) {
	c := corpus()
	from := netip.MustParseAddr("192.0.2.250")
	dg, err := Decode(c, from, "t", testReceived)
	if err != nil || len(dg.Records) != 2 || len(dg.Counters) != 1 {
		t.Fatalf("corpus: err=%v %+v", err, dg)
	}
	for cut := 0; cut < len(c); cut++ {
		dg, _ := Decode(c[:cut], from, "t", testReceived)
		checkRecords(t, dg)
	}
	rng := rand.New(rand.NewPCG(3, 4))
	for i := 0; i < 5000; i++ {
		b := append([]byte(nil), c...)
		for n := 1 + rng.IntN(4); n > 0; n-- {
			b[rng.IntN(len(b))] = byte(rng.IntN(256))
		}
		dg, _ := Decode(b, from, "t", testReceived)
		checkRecords(t, dg)
	}
	for i := 0; i < 1000; i++ {
		b := make([]byte, rng.IntN(1500))
		for j := range b {
			b[j] = byte(rng.IntN(256))
		}
		if len(b) >= 4 {
			copy(b, []byte{0, 0, 0, 5})
		}
		dg, _ := Decode(b, from, "t", testReceived)
		checkRecords(t, dg)
	}
}

// go test -fuzz=FuzzDecode ./internal/sflow; plain go test runs the seed.
func FuzzDecode(f *testing.F) {
	f.Add(corpus())
	from := netip.MustParseAddr("192.0.2.250")
	f.Fuzz(func(t *testing.T, b []byte) {
		dg, _ := Decode(b, from, "t", testReceived)
		checkRecords(t, dg)
	})
}
