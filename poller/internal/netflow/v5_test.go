package netflow

import (
	"encoding/binary"
	"net"
	"testing"
	"time"
)

func v5Packet(samplingWord uint16, sysUptime, flowLast uint32) []byte {
	buf := make([]byte, V5HeaderLen+V5RecordLen)
	binary.BigEndian.PutUint16(buf[0:2], 5)
	binary.BigEndian.PutUint16(buf[2:4], 1)
	binary.BigEndian.PutUint32(buf[4:8], sysUptime)
	binary.BigEndian.PutUint32(buf[8:12], 1_700_000_000)
	binary.BigEndian.PutUint32(buf[16:20], 42)
	buf[20] = 1
	buf[21] = 2
	binary.BigEndian.PutUint16(buf[22:24], samplingWord)

	off := V5HeaderLen
	copy(buf[off:off+4], net.ParseIP("10.0.0.10").To4())
	copy(buf[off+4:off+8], net.ParseIP("172.16.0.20").To4())
	copy(buf[off+8:off+12], net.ParseIP("10.0.0.1").To4())
	binary.BigEndian.PutUint16(buf[off+12:off+14], 3)
	binary.BigEndian.PutUint16(buf[off+14:off+16], 4)
	binary.BigEndian.PutUint32(buf[off+16:off+20], 25)
	binary.BigEndian.PutUint32(buf[off+20:off+24], 15_000)
	binary.BigEndian.PutUint32(buf[off+24:off+28], flowLast-9_000)
	binary.BigEndian.PutUint32(buf[off+28:off+32], flowLast)
	binary.BigEndian.PutUint16(buf[off+32:off+34], 51515)
	binary.BigEndian.PutUint16(buf[off+34:off+36], 443)
	buf[off+37] = 0x1b
	buf[off+38] = 6
	buf[off+39] = 0
	binary.BigEndian.PutUint16(buf[off+40:off+42], 64512)
	binary.BigEndian.PutUint16(buf[off+42:off+44], 64513)
	buf[off+44] = 24
	buf[off+45] = 16
	return buf
}

func TestParseV5(t *testing.T) {
	pkt, err := ParseV5(v5Packet(100, 100_000, 99_000), addr("192.0.2.5"), "test-collector", time.Unix(1_700_000_001, 0))
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
	if r.FieldsPresent != FieldsV5 {
		t.Fatalf("fields_present = %#x, want %#x (bits 0-8 + tracked)", r.FieldsPresent, FieldsV5)
	}
	if r.ObservationDomain != 1<<8|2 || r.ExportProtocol != ProtocolNetFlow5 || r.IPVersion != 4 {
		t.Fatalf("identity: domain=%d proto=%q ipv=%d", r.ObservationDomain, r.ExportProtocol, r.IPVersion)
	}
	if r.FlowDirection != DirectionUnknown || r.InputSNMP != 3 || r.OutputSNMP != 4 {
		t.Fatalf("direction/ifindex: %+v", r)
	}
	if r.ExporterIP.String() != "192.0.2.5" || r.ExporterAddr != addr("192.0.2.5") {
		t.Fatalf("exporter = %v / %v", r.ExporterIP, r.ExporterAddr)
	}
	if r.SrcAddr6 != nil || r.DstAddr6 != nil {
		t.Fatal("an IPv4 flow must not carry IPv6 addresses")
	}
}

// The v5 header word is 2 mode bits + a 14-bit interval. Reading the whole
// word made deterministic 1-in-100 (0x4064) an interval of 16484 (X-02).
func TestParseV5SamplingModeBits(t *testing.T) {
	cases := []struct {
		word     uint16
		mode     uint8
		interval uint32
	}{
		{0x4064, 1, 100},
		{0x8064, 2, 100},
		{0x0064, 0, 100},
		{0x0000, 0, 1},
		{0x4000, 1, 1},
		{0x7FFF, 1, 0x3FFF},
	}
	for _, c := range cases {
		pkt, err := ParseV5(v5Packet(c.word, 100_000, 99_000), addr("192.0.2.5"), "t", testReceivedAt)
		if err != nil {
			t.Fatal(err)
		}
		if pkt.SamplingMode != c.mode || pkt.Records[0].SamplingInterval != c.interval {
			t.Errorf("word %#04x: mode=%d interval=%d, want mode=%d interval=%d",
				c.word, pkt.SamplingMode, pkt.Records[0].SamplingInterval, c.mode, c.interval)
		}
	}
}

func TestParseV5ExporterIdentity(t *testing.T) {
	cases := []struct {
		from     string
		wantAddr string
		wantIPv4 string // "" = exporter_ip column written as 0.0.0.0
	}{
		{"192.0.2.5", "192.0.2.5", "192.0.2.5"},
		{"::ffff:192.0.2.5", "192.0.2.5", "192.0.2.5"}, // dual-stack socket delivery
		{"2001:db8::5", "2001:db8::5", ""},
	}
	for _, c := range cases {
		pkt, err := ParseV5(v5Packet(0, 100_000, 99_000), addr(c.from), "t", testReceivedAt)
		if err != nil {
			t.Fatal(err)
		}
		r := pkt.Records[0]
		if r.ExporterAddr != addr(c.wantAddr) {
			t.Errorf("%s: exporter addr %v, want %s", c.from, r.ExporterAddr, c.wantAddr)
		}
		if (c.wantIPv4 == "" && r.ExporterIP != nil) || (c.wantIPv4 != "" && r.ExporterIP.String() != c.wantIPv4) {
			t.Errorf("%s: exporter_ip storage %v, want %q", c.from, r.ExporterIP, c.wantIPv4)
		}
	}
}

// A flow that ended just before the exporter's 32-bit uptime counter wrapped
// resolves to its real time instead of falling back to receipt time.
func TestUptimeTimestampWrap(t *testing.T) {
	export := time.Unix(1_700_000_000, 0).UTC()
	got := uptimeTimestamp(export, 500, 4_294_966_796, testReceivedAt) // 1000 ms before the wrap point
	if want := export.Add(-time.Second); !got.Equal(want) {
		t.Fatalf("wrapped uptime: got %s want %s", got, want)
	}
	// Flow time ahead of the header uptime: inconsistent, use receipt time.
	if got := uptimeTimestamp(export, 1_000, 5_000, testReceivedAt); !got.Equal(testReceivedAt) {
		t.Fatalf("future uptime: got %s want receipt time", got)
	}
}
