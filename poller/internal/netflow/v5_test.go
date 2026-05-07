package netflow

import (
	"encoding/binary"
	"net"
	"testing"
	"time"
)

func TestParseV5(t *testing.T) {
	buf := make([]byte, V5HeaderLen+V5RecordLen)
	binary.BigEndian.PutUint16(buf[0:2], 5)
	binary.BigEndian.PutUint16(buf[2:4], 1)
	binary.BigEndian.PutUint32(buf[4:8], 100_000)
	binary.BigEndian.PutUint32(buf[8:12], 1_700_000_000)
	binary.BigEndian.PutUint32(buf[16:20], 42)
	buf[20] = 1
	buf[21] = 2
	binary.BigEndian.PutUint16(buf[22:24], 100)

	off := V5HeaderLen
	copy(buf[off:off+4], net.ParseIP("10.0.0.10").To4())
	copy(buf[off+4:off+8], net.ParseIP("172.16.0.20").To4())
	copy(buf[off+8:off+12], net.ParseIP("10.0.0.1").To4())
	binary.BigEndian.PutUint16(buf[off+12:off+14], 3)
	binary.BigEndian.PutUint16(buf[off+14:off+16], 4)
	binary.BigEndian.PutUint32(buf[off+16:off+20], 25)
	binary.BigEndian.PutUint32(buf[off+20:off+24], 15_000)
	binary.BigEndian.PutUint32(buf[off+24:off+28], 90_000)
	binary.BigEndian.PutUint32(buf[off+28:off+32], 99_000)
	binary.BigEndian.PutUint16(buf[off+32:off+34], 51515)
	binary.BigEndian.PutUint16(buf[off+34:off+36], 443)
	buf[off+37] = 0x1b
	buf[off+38] = 6
	buf[off+39] = 0
	binary.BigEndian.PutUint16(buf[off+40:off+42], 64512)
	binary.BigEndian.PutUint16(buf[off+42:off+44], 64513)
	buf[off+44] = 24
	buf[off+45] = 16

	pkt, err := ParseV5(buf, net.ParseIP("192.0.2.5"), "test-collector", time.Unix(1_700_000_001, 0))
	if err != nil {
		t.Fatal(err)
	}
	if len(pkt.Records) != 1 {
		t.Fatalf("expected 1 record, got %d", len(pkt.Records))
	}
	r := pkt.Records[0]
	if got := r.SrcAddr.String(); got != "10.0.0.10" {
		t.Fatalf("src addr = %s", got)
	}
	if got := r.DstAddr.String(); got != "172.16.0.20" {
		t.Fatalf("dst addr = %s", got)
	}
	if r.Bytes != 15_000 || r.Packets != 25 || r.DstPort != 443 || r.Protocol != 6 {
		t.Fatalf("unexpected decoded counters/ports/protocol: %+v", r)
	}
	want := time.Unix(1_700_000_000, 0).Add(-time.Second)
	if !r.Timestamp.Equal(want) {
		t.Fatalf("timestamp = %s, want %s", r.Timestamp, want)
	}
}
