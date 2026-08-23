package netpath

import (
	"encoding/binary"
	"net"
	"testing"
)

func TestTokenRoundTrip(t *testing.T) {
	cases := []struct{ nonce, flow, ttl, pidx int }{
		{0, 0, 1, 0}, {7, 15, 63, 7}, {3, 9, 30, 2}, {1, 4, 12, 5},
	}
	for _, c := range cases {
		tok := encodeToken(c.nonce, c.flow, c.ttl, c.pidx)
		n, f, tt, p := decodeToken(tok)
		if n != c.nonce || f != c.flow || tt != c.ttl || p != c.pidx {
			t.Errorf("round-trip %v -> tok %d -> (%d,%d,%d,%d)", c, tok, n, f, tt, p)
		}
	}
}

// A flow's ICMP probes must share one checksum across TTLs (Paris-ICMP), so a
// router hashing on the ICMP checksum keeps the flow on a single path.
func TestICMPChecksumConstantPerFlow(t *testing.T) {
	for flow := 0; flow < 8; flow++ {
		var ck uint16
		for ttl := 1; ttl <= 30; ttl++ {
			for pidx := 0; pidx < 3; pidx++ {
				msg := buildICMPEcho(2, flow, ttl, pidx)
				got := binary.BigEndian.Uint16(msg[2:4])
				// verify the checksum field is actually correct
				save := make([]byte, len(msg))
				copy(save, msg)
				save[2], save[3] = 0, 0
				if checksum(save) != got {
					t.Fatalf("flow %d ttl %d pidx %d: checksum field %x != recomputed %x", flow, ttl, pidx, got, checksum(save))
				}
				if ttl == 1 && pidx == 0 {
					ck = got
				} else if got != ck {
					t.Fatalf("flow %d: checksum drift ttl %d pidx %d: %x != %x", flow, ttl, pidx, got, ck)
				}
			}
		}
	}
}

// Different flows should get different ICMP checksums so they can be spread
// across ECMP branches.
func TestICMPChecksumVariesByFlow(t *testing.T) {
	seen := map[uint16]int{}
	for flow := 0; flow < 8; flow++ {
		msg := buildICMPEcho(0, flow, 5, 0)
		ck := binary.BigEndian.Uint16(msg[2:4])
		seen[ck]++
	}
	if len(seen) < 6 {
		t.Errorf("expected mostly distinct per-flow checksums, got %d distinct of 8", len(seen))
	}
}

func TestTCPSynChecksumAndSeq(t *testing.T) {
	src := net.IPv4(192, 168, 1, 10)
	dst := net.IPv4(8, 8, 8, 8)
	tok := encodeToken(1, 2, 7, 0)
	seg := buildTCPSYN(src, dst, 2, 443, tok)
	// verify L4 checksum
	got := binary.BigEndian.Uint16(seg[16:18])
	save := make([]byte, len(seg))
	copy(save, seg)
	save[16], save[17] = 0, 0
	if l4Checksum(src, dst, 6, save) != got {
		t.Fatalf("tcp checksum %x != recomputed", got)
	}
	// verify seq encodes the token and a SYN-ACK (ack=seq+1) recovers it
	seq := binary.BigEndian.Uint32(seg[4:8])
	ack := seq + 1
	rseg := make([]byte, 20)
	binary.BigEndian.PutUint16(rseg[0:2], 443)
	binary.BigEndian.PutUint16(rseg[2:4], uint16(tcpSPortBase+2)) // our src port
	binary.BigEndian.PutUint32(rseg[8:12], ack)
	rseg[13] = 0x12 // SYN+ACK
	r := parseTCPReply(dst, rseg)
	if r.kind != replyTCP || r.tok != tok || !r.dest {
		t.Fatalf("parseTCPReply failed: kind=%d tok=%d want %d", r.kind, r.tok, tok)
	}
}

func TestParseICMPTimeExceeded(t *testing.T) {
	// craft a Time-Exceeded quoting an IPv4 header whose ID is our token
	tok := encodeToken(4, 3, 9, 1)
	quote := make([]byte, 28)
	quote[0] = 0x45
	binary.BigEndian.PutUint16(quote[4:6], tok)
	quote[9] = 1 // proto icmp
	msg := make([]byte, 8+len(quote))
	msg[0] = 11 // time exceeded
	copy(msg[8:], quote)
	r := parseICMP(net.IPv4(10, 0, 0, 1), msg)
	if r.kind != replyTimeExceeded || r.tok != tok {
		t.Fatalf("parseICMP TE failed: kind=%d tok=%d want %d", r.kind, r.tok, tok)
	}
}
