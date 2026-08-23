package netpath

// Raw-packet construction and reply parsing for the Paris-style traceroute
// engine. Every probe carries a 16-bit token in the IPv4 Identification field
// so the ICMP Time-Exceeded / Destination-Unreachable errors that quote the
// original datagram can be mapped back to the exact (flow, ttl, probe) that
// triggered them — the correlation trick from the SolarWinds NetPath patent
// (US 9,419,889) and Paris/Dublin traceroute.
//
// Token layout (16 bits):  nonce(3) | flow(4) | ttl(6) | pidx(3)
//   nonce  distinguishes overlapping runs on the shared raw sockets
//   flow   the ECMP flow index (each flow holds its 5-tuple constant)
//   ttl    the hop distance being probed
//   pidx   the probe index within a (flow, ttl) for loss statistics

import (
	"encoding/binary"
	"net"
)

const (
	tcpSPortBase = 33000 // src port = base + flow (per-flow ECMP key, TCP)
	udpSPortBase = 34000 // src port = base + flow (per-flow ECMP key, UDP)
	udpDPortBase = 33434 // classic traceroute UDP dest port (when no service port)
	tcpSeqMagic  = 0x5A00 // high 16 bits of the TCP sequence, marks our probes
	icmpMarker   = 0x8000 // high bit of the ICMP id, marks our echo requests
)

// encodeToken packs the probe coordinates into the 16-bit IP ID.
func encodeToken(nonce, flow, ttl, pidx int) uint16 {
	return uint16((nonce&7)<<13 | (flow&15)<<9 | (ttl&63)<<3 | (pidx & 7))
}

// decodeToken reverses encodeToken.
func decodeToken(tok uint16) (nonce, flow, ttl, pidx int) {
	pidx = int(tok & 7)
	ttl = int(tok>>3) & 63
	flow = int(tok>>9) & 15
	nonce = int(tok>>13) & 7
	return
}

// ---------------------------------------------------------------- checksums

// onesSum computes the 16-bit one's-complement sum of b (RFC 1071).
func onesSum(b []byte) uint32 {
	var sum uint32
	for i := 0; i+1 < len(b); i += 2 {
		sum += uint32(binary.BigEndian.Uint16(b[i : i+2]))
	}
	if len(b)%2 == 1 {
		sum += uint32(b[len(b)-1]) << 8
	}
	return sum
}

func fold(sum uint32) uint16 {
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return uint16(sum)
}

// checksum returns the internet checksum of b (with the checksum field zeroed).
func checksum(b []byte) uint16 {
	return ^fold(onesSum(b))
}

// l4Checksum computes a TCP/UDP checksum over the IPv4 pseudo-header + segment.
func l4Checksum(src, dst net.IP, proto byte, seg []byte) uint16 {
	pseudo := make([]byte, 12)
	copy(pseudo[0:4], src.To4())
	copy(pseudo[4:8], dst.To4())
	pseudo[9] = proto
	binary.BigEndian.PutUint16(pseudo[10:12], uint16(len(seg)))
	sum := onesSum(pseudo) + onesSum(seg)
	return ^fold(sum)
}

// ---------------------------------------------------------------- builders

// buildTCPSYN builds a bare TCP SYN whose sequence number encodes the token,
// so a destination SYN-ACK/RST (ack = seq+1) identifies the probe.
func buildTCPSYN(src, dst net.IP, flow int, dport int, tok uint16) []byte {
	seg := make([]byte, 20)
	sport := tcpSPortBase + flow
	binary.BigEndian.PutUint16(seg[0:2], uint16(sport))
	binary.BigEndian.PutUint16(seg[2:4], uint16(dport))
	binary.BigEndian.PutUint32(seg[4:8], uint32(tcpSeqMagic)<<16|uint32(tok)) // seq
	// ack = 0
	seg[12] = 5 << 4 // data offset = 5 words, no options
	seg[13] = 0x02   // SYN
	binary.BigEndian.PutUint16(seg[14:16], 29200) // window
	// checksum
	ck := l4Checksum(src, dst, 6, seg)
	binary.BigEndian.PutUint16(seg[16:18], ck)
	return seg
}

// buildUDP builds a small UDP datagram. The token rides in the IP ID; the
// segment payload is fixed filler.
func buildUDP(src, dst net.IP, flow int, dport int) []byte {
	seg := make([]byte, 12)
	sport := udpSPortBase + flow
	binary.BigEndian.PutUint16(seg[0:2], uint16(sport))
	binary.BigEndian.PutUint16(seg[2:4], uint16(dport))
	binary.BigEndian.PutUint16(seg[4:6], uint16(len(seg)))
	copy(seg[8:12], []byte("ZNPQ"))
	ck := l4Checksum(src, dst, 17, seg)
	if ck == 0 {
		ck = 0xffff
	}
	binary.BigEndian.PutUint16(seg[6:8], ck)
	return seg
}

// buildICMPEcho builds an ICMP echo request. The id encodes nonce+flow (held
// constant per flow so ECMP hashing keeps the flow's path stable); the seq
// encodes ttl+pidx. A 2-byte compensation word in the payload holds the ICMP
// checksum constant across a flow's probes (Paris-ICMP), so routers that hash
// on the ICMP checksum still keep the flow on one path.
func buildICMPEcho(nonce, flow, ttl, pidx int) []byte {
	msg := make([]byte, 12)
	msg[0] = 8 // echo request
	msg[1] = 0
	id := uint16(icmpMarker | (nonce&7)<<11 | (flow & 15))
	seq := uint16((ttl&63)<<3 | (pidx & 7))
	binary.BigEndian.PutUint16(msg[4:6], id)
	binary.BigEndian.PutUint16(msg[6:8], seq)
	// choose a per-flow constant checksum target so all probes in a flow share it
	ckConst := uint16(0xC000 | ((flow * 0x0111) & 0x0fff))
	target := ^ckConst // desired fold(sum)
	// sum with comp=0, checksum field=0
	s := fold(onesSum(msg))
	// comp = target - s  (one's-complement subtraction)
	comp := fold(uint32(target) + uint32(^s))
	binary.BigEndian.PutUint16(msg[8:10], comp)
	ck := checksum(msg)
	binary.BigEndian.PutUint16(msg[2:4], ck)
	return msg
}

// ---------------------------------------------------------------- parsers

type replyKind int

const (
	replyNone replyKind = iota
	replyTimeExceeded
	replyUnreachable // destination/port unreachable
	replyEcho        // echo reply from destination
	replyTCP         // SYN-ACK / RST from destination
)

type reply struct {
	kind replyKind
	from net.IP  // the responder (router or destination)
	tok  uint16  // recovered probe token (for TE / unreachable / echo)
	dest bool    // true when this proves the destination answered
}

// parseICMP decodes an ICMP message (payload of an ip4:icmp raw read). from is
// the outer source IP. It recovers our probe token from the quoted datagram
// (Time-Exceeded / Unreachable) or from the echoed id+seq (Echo Reply).
func parseICMP(from net.IP, p []byte) reply {
	r := reply{kind: replyNone, from: from}
	if len(p) < 8 {
		return r
	}
	typ := p[0]
	code := p[1]
	switch typ {
	case 11: // Time Exceeded
		tok, ok := tokenFromQuote(p[8:])
		if !ok {
			return r
		}
		r.kind = replyTimeExceeded
		r.tok = tok
		return r
	case 3: // Destination Unreachable
		tok, ok := tokenFromQuote(p[8:])
		if !ok {
			return r
		}
		r.kind = replyUnreachable
		r.tok = tok
		r.dest = true // reached the destination host (port/host unreachable)
		_ = code
		return r
	case 0: // Echo Reply
		id := binary.BigEndian.Uint16(p[4:6])
		seq := binary.BigEndian.Uint16(p[6:8])
		if id&icmpMarker == 0 {
			return r
		}
		nonce := int(id>>11) & 7
		flow := int(id & 15)
		ttl := int(seq>>3) & 63
		pidx := int(seq & 7)
		r.kind = replyEcho
		r.tok = encodeToken(nonce, flow, ttl, pidx)
		r.dest = true
		return r
	}
	return r
}

// tokenFromQuote extracts our IP-ID token from a quoted original datagram
// (the bytes an ICMP error carries after its 8-byte header: the original IPv4
// header + at least 8 bytes of transport).
func tokenFromQuote(q []byte) (uint16, bool) {
	if len(q) < 20 {
		return 0, false
	}
	if q[0]>>4 != 4 {
		return 0, false
	}
	id := binary.BigEndian.Uint16(q[4:6])
	return id, true
}

// parseTCPReply decodes a raw TCP segment (from the destination). It returns a
// reply when the segment is a SYN-ACK or RST answering one of our SYN probes,
// recovering the token from ack-1.
func parseTCPReply(from net.IP, seg []byte) reply {
	r := reply{kind: replyNone, from: from}
	if len(seg) < 20 {
		return r
	}
	dport := binary.BigEndian.Uint16(seg[2:4]) // our source port
	if int(dport) < tcpSPortBase || int(dport) >= tcpSPortBase+16 {
		return r
	}
	flags := seg[13]
	isSynAck := flags&0x12 == 0x12 // SYN+ACK
	isRst := flags&0x04 != 0       // RST
	if !isSynAck && !isRst {
		return r
	}
	ack := binary.BigEndian.Uint32(seg[8:12])
	if ack == 0 {
		return r
	}
	seq := ack - 1
	if seq>>16 != uint32(tcpSeqMagic) {
		return r
	}
	r.kind = replyTCP
	r.tok = uint16(seq & 0xffff)
	r.dest = true
	return r
}
