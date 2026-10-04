package sflow

import "encoding/binary"

// packetInfo is what a sampled packet header says about the flow it belongs
// to. ok is false when no IP header could be found (a non-IP frame, an
// unsupported header protocol, or a header cut before the IP addresses).
type packetInfo struct {
	ok         bool
	v6         bool
	src4, dst4 [4]byte
	src6, dst6 [16]byte
	proto      uint8
	tos        uint8 // IPv4 ToS byte / IPv6 traffic class

	srcPort, dstPort uint16
	hasPorts         bool
	tcpFlags         uint8
	hasFlags         bool
	icmp             uint16 // type<<8 | code
	hasICMP          bool
}

// Header protocols of the sampled_header flow record (sFlow v5
// header_protocol enumeration) that carry IP.
const (
	headerEthernet = 1  // ETHERNET-ISO88023
	headerIPv4     = 11 // the header starts at the IPv4 header
	headerIPv6     = 12
)

const (
	etherTypeIPv4      = 0x0800
	etherTypeIPv6      = 0x86DD
	etherTypeVLAN      = 0x8100 // 802.1Q
	etherTypeQinQ      = 0x88A8 // 802.1ad
	etherTypeQinQOld   = 0x9100 // pre-standard stacked VLAN
	etherTypeMPLS      = 0x8847
	etherTypeMPLSMulti = 0x8848
)

// maxEncapsulation bounds VLAN tags, MPLS labels and IPv6 extension headers
// walked in one header: real stacks are a few deep, a corrupt header must
// not loop.
const maxEncapsulation = 8

func parseHeader(proto uint32, h []byte) packetInfo {
	switch proto {
	case headerEthernet:
		return parseEthernet(h)
	case headerIPv4:
		return parseIPv4(h)
	case headerIPv6:
		return parseIPv6(h)
	}
	return packetInfo{}
}

// parseEthernet skips the MAC header, any 802.1Q / 802.1ad tags, an LLC/SNAP
// header and an MPLS label stack to reach the IP header.
func parseEthernet(h []byte) packetInfo {
	if len(h) < 14 {
		return packetInfo{}
	}
	et, off := binary.BigEndian.Uint16(h[12:14]), 14
	for i := 0; i < maxEncapsulation && (et == etherTypeVLAN || et == etherTypeQinQ || et == etherTypeQinQOld); i++ {
		if len(h) < off+4 {
			return packetInfo{}
		}
		et, off = binary.BigEndian.Uint16(h[off+2:off+4]), off+4
	}
	if et < 0x0600 { // an 802.3 length field: only SNAP-encapsulated IP is followed
		if len(h) < off+8 || h[off] != 0xAA || h[off+1] != 0xAA || h[off+2] != 0x03 {
			return packetInfo{}
		}
		et, off = binary.BigEndian.Uint16(h[off+6:off+8]), off+8
	}
	switch et {
	case etherTypeIPv4:
		return parseIPv4(h[off:])
	case etherTypeIPv6:
		return parseIPv6(h[off:])
	case etherTypeMPLS, etherTypeMPLSMulti:
		return parseMPLS(h[off:])
	}
	return packetInfo{}
}

// parseMPLS pops labels up to the bottom of the stack and recognises the IP
// payload by its version nibble (MPLS carries no payload type).
func parseMPLS(h []byte) packetInfo {
	for i := 0; i < maxEncapsulation; i++ {
		if len(h) < 4 {
			return packetInfo{}
		}
		bottom := h[2]&0x01 != 0
		h = h[4:]
		if !bottom {
			continue
		}
		if len(h) == 0 {
			return packetInfo{}
		}
		switch h[0] >> 4 {
		case 4:
			return parseIPv4(h)
		case 6:
			return parseIPv6(h)
		}
		return packetInfo{}
	}
	return packetInfo{}
}

func parseIPv4(h []byte) packetInfo {
	var p packetInfo
	if len(h) < 20 || h[0]>>4 != 4 {
		return p
	}
	ihl := int(h[0]&0x0F) * 4
	if ihl < 20 {
		return p
	}
	p.ok, p.tos, p.proto = true, h[1], h[9]
	copy(p.src4[:], h[12:16])
	copy(p.dst4[:], h[16:20])
	// Only the first fragment carries the transport header.
	if binary.BigEndian.Uint16(h[6:8])&0x1FFF != 0 || len(h) < ihl {
		return p
	}
	p.transport(h[ihl:])
	return p
}

// IPv6 extension headers walked to reach the transport header.
const (
	ip6HopByHop = 0
	ip6Routing  = 43
	ip6Fragment = 44
	ip6AH       = 51
	ip6DestOpts = 60
)

func parseIPv6(h []byte) packetInfo {
	var p packetInfo
	if len(h) < 40 || h[0]>>4 != 6 {
		return p
	}
	p.ok, p.v6 = true, true
	p.tos = h[0]<<4 | h[1]>>4
	copy(p.src6[:], h[8:24])
	copy(p.dst6[:], h[24:40])
	next, rest := h[6], h[40:]
	for i := 0; i < maxEncapsulation; i++ {
		var n int
		switch next {
		case ip6HopByHop, ip6Routing, ip6DestOpts:
			if len(rest) < 2 {
				break
			}
			n = (int(rest[1]) + 1) * 8
		case ip6AH:
			if len(rest) < 2 {
				break
			}
			n = (int(rest[1]) + 2) * 4
		case ip6Fragment:
			if len(rest) < 8 {
				break
			}
			if binary.BigEndian.Uint16(rest[2:4])>>3 != 0 {
				// A later fragment: the upper protocol is known, its header is not here.
				p.proto = rest[0]
				return p
			}
			n = 8
		default:
			p.proto = next
			p.transport(rest)
			return p
		}
		if n == 0 || len(rest) < n {
			// The sampled header ends inside the extension chain: the
			// addresses are real, the upper-layer protocol is the last one
			// the chain named.
			p.proto = next
			return p
		}
		next, rest = rest[0], rest[n:]
	}
	p.proto = next
	return p
}

// transport reads ports, TCP flags or the ICMP type/code from the start of
// the transport header (as far as the sampled header reaches).
func (p *packetInfo) transport(b []byte) {
	switch p.proto {
	case 6: // TCP
		p.ports(b)
		if len(b) >= 14 {
			p.tcpFlags, p.hasFlags = b[13], true
		}
	case 17, 33, 132, 136: // UDP, DCCP, SCTP, UDP-Lite: ports come first
		p.ports(b)
	case 1, 58: // ICMP, ICMPv6
		if len(b) >= 2 {
			p.icmp, p.hasICMP = uint16(b[0])<<8|uint16(b[1]), true
		}
	}
}

func (p *packetInfo) ports(b []byte) {
	if len(b) >= 4 {
		p.srcPort, p.dstPort, p.hasPorts = binary.BigEndian.Uint16(b[0:2]), binary.BigEndian.Uint16(b[2:4]), true
	}
}
