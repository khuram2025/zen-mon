package sflow

import (
	"encoding/binary"
	"testing"
)

// Sampled-header edge cases: what a switch really puts in the first 128 bytes
// of a frame. Each case states the ports/flags that may be read and those
// that must not be guessed.

func TestParseHeaderEdgeCases(t *testing.T) {
	ipv4Options := func() []byte {
		h := ipv4(0, 6, "10.0.0.1", "10.0.0.2", 0, nil)
		h[0] = 0x46 // IHL 6: one 4-byte option word
		binary.BigEndian.PutUint16(h[2:4], 24+20)
		return append(append(h, 1, 1, 1, 0), tcp(1234, 80, 0x02)...) // NOP NOP NOP EOL
	}()
	fragment := func(offset uint16, more bool, next byte, payload []byte) []byte {
		f := []byte{next, 0, 0, 0, 0, 0, 0, 42}
		v := offset << 3
		if more {
			v |= 1
		}
		binary.BigEndian.PutUint16(f[2:4], v)
		return append(f, payload...)
	}
	ah := append([]byte{6, 4}, make([]byte, 22)...) // next TCP, payload len 4 -> (4+2)*4 = 24 bytes
	snap := func(payload []byte) []byte {
		h := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
		h = binary.BigEndian.AppendUint16(h, uint16(8+len(payload))) // 802.3 length field
		h = append(h, 0xAA, 0xAA, 0x03, 0, 0, 0)                     // LLC/SNAP, OUI 0
		h = binary.BigEndian.AppendUint16(h, etherTypeIPv4)
		return append(h, payload...)
	}

	cases := []struct {
		name             string
		proto            uint32
		hdr              []byte
		ok, v6           bool
		ipProto          uint8
		ports            bool
		srcPort, dstPort uint16
		flags            bool
	}{
		{"IPv4 with options", headerIPv4, ipv4Options, true, false, 6, true, 1234, 80, true},
		{"IPv4 header cut inside its options", headerIPv4, ipv4Options[:22], true, false, 6, false, 0, 0, false},
		{"802.3 LLC/SNAP", headerEthernet, snap(ipv4(0, 17, "10.0.0.1", "10.0.0.2", 0, udp(53, 5353))), true, false, 17, true, 53, 5353, false},
		{"802.3 non-SNAP LLC (not IP)", headerEthernet, append(ethernet(64, nil, nil), 0x42, 0x42, 0x03, 0, 0, 0, 0, 0), false, false, 0, false, 0, 0, false},
		{"three VLAN tags", headerEthernet, ethernet(etherTypeIPv4, []uint16{etherTypeQinQ, etherTypeQinQOld, etherTypeVLAN}, ipv4(0, 6, "10.0.0.1", "10.0.0.2", 0, tcp(1, 2, 0x10))), true, false, 6, true, 1, 2, true},
		{"VLAN tag cut off", headerEthernet, ethernet(etherTypeVLAN, nil, []byte{0, 100}), false, false, 0, false, 0, 0, false},
		{"IPv6 first fragment", headerIPv6, ipv6(0, ip6Fragment, "2001:db8::1", "2001:db8::2", fragment(0, true, 17, udp(500, 4500))), true, true, 17, true, 500, 4500, false},
		{"IPv6 later fragment", headerIPv6, ipv6(0, ip6Fragment, "2001:db8::1", "2001:db8::2", fragment(185, false, 17, udp(500, 4500))), true, true, 17, false, 0, 0, false},
		{"IPv6 AH then TCP", headerIPv6, ipv6(0, ip6AH, "2001:db8::1", "2001:db8::2", append(ah, tcp(22, 50000, 0x18)...)), true, true, 6, true, 22, 50000, true},
		{"IPv6 ESP (encrypted, no ports)", headerIPv6, ipv6(0, 50, "2001:db8::1", "2001:db8::2", make([]byte, 16)), true, true, 50, false, 0, 0, false},
		{"IPv6 chain cut inside a header", headerIPv6, ipv6(0, ip6HopByHop, "2001:db8::1", "2001:db8::2", []byte{6, 2, 0, 0}), true, true, ip6HopByHop, false, 0, 0, false},
		{"IPv6 header too short", headerIPv6, ipv6(0, 6, "2001:db8::1", "2001:db8::2", nil)[:39], false, false, 0, false, 0, 0, false},
		{"TCP header cut before the flags", headerIPv4, ipv4(0, 6, "10.0.0.1", "10.0.0.2", 0, tcp(1, 2, 0x02)[:8]), true, false, 6, true, 1, 2, false},
		{"MPLS stack deeper than the walk", headerEthernet, ethernet(etherTypeMPLS, nil, make([]byte, 4*(maxEncapsulation+1))), false, false, 0, false, 0, 0, false},
		{"MPLS pseudowire control word", headerEthernet, ethernet(etherTypeMPLS, nil, []byte{0, 0x10, 0x01, 64, 0, 0, 0, 0}), false, false, 0, false, 0, 0, false},
		{"unknown header protocol", 7, ipv4(0, 6, "10.0.0.1", "10.0.0.2", 0, tcp(1, 2, 0x02)), false, false, 0, false, 0, 0, false},
	}
	for _, c := range cases {
		p := parseHeader(c.proto, c.hdr)
		if p.ok != c.ok || (c.ok && (p.v6 != c.v6 || p.proto != c.ipProto)) {
			t.Errorf("%s: ok=%v v6=%v proto=%d, want ok=%v v6=%v proto=%d", c.name, p.ok, p.v6, p.proto, c.ok, c.v6, c.ipProto)
			continue
		}
		if p.hasPorts != c.ports || p.srcPort != c.srcPort || p.dstPort != c.dstPort || p.hasFlags != c.flags {
			t.Errorf("%s: ports=%v %d/%d flags=%v, want ports=%v %d/%d flags=%v", c.name, p.hasPorts, p.srcPort, p.dstPort, p.hasFlags, c.ports, c.srcPort, c.dstPort, c.flags)
		}
	}
}
