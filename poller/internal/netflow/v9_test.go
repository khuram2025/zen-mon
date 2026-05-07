package netflow

import (
	"encoding/binary"
	"net"
	"testing"
	"time"
)

func TestParseV9TemplateAndData(t *testing.T) {
	exporter := net.ParseIP("192.0.2.10").To4()
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	template := make([]byte, V9HeaderLen+4+4+8*4)
	binary.BigEndian.PutUint16(template[0:2], 9)
	binary.BigEndian.PutUint16(template[2:4], 1)
	binary.BigEndian.PutUint32(template[4:8], 100_000)
	binary.BigEndian.PutUint32(template[8:12], 1_700_000_000)
	binary.BigEndian.PutUint32(template[12:16], 1)
	binary.BigEndian.PutUint32(template[16:20], 77)
	binary.BigEndian.PutUint16(template[20:22], 0)
	binary.BigEndian.PutUint16(template[22:24], uint16(4+4+8*4))
	binary.BigEndian.PutUint16(template[24:26], 256)
	binary.BigEndian.PutUint16(template[26:28], 8)
	fields := []V9TemplateField{
		{Type: 8, Length: 4},  // src
		{Type: 12, Length: 4}, // dst
		{Type: 1, Length: 4},  // bytes
		{Type: 2, Length: 4},  // packets
		{Type: 7, Length: 2},  // src port
		{Type: 11, Length: 2}, // dst port
		{Type: 4, Length: 1},  // protocol
		{Type: 21, Length: 4}, // last switched
	}
	off := 28
	for _, f := range fields {
		binary.BigEndian.PutUint16(template[off:off+2], f.Type)
		binary.BigEndian.PutUint16(template[off+2:off+4], f.Length)
		off += 4
	}

	records, stats, err := ParseV9(template, exporter, "test", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 0 || stats.TemplatesUpdated != 1 {
		t.Fatalf("template parse stats=%+v records=%d", stats, len(records))
	}

	recordLen := 25
	data := make([]byte, V9HeaderLen+4+recordLen)
	binary.BigEndian.PutUint16(data[0:2], 9)
	binary.BigEndian.PutUint16(data[2:4], 1)
	binary.BigEndian.PutUint32(data[4:8], 100_000)
	binary.BigEndian.PutUint32(data[8:12], 1_700_000_000)
	binary.BigEndian.PutUint32(data[12:16], 2)
	binary.BigEndian.PutUint32(data[16:20], 77)
	binary.BigEndian.PutUint16(data[20:22], 256)
	binary.BigEndian.PutUint16(data[22:24], uint16(4+recordLen))
	off = 24
	copy(data[off:off+4], net.ParseIP("10.1.1.10").To4()); off += 4
	copy(data[off:off+4], net.ParseIP("172.16.1.20").To4()); off += 4
	binary.BigEndian.PutUint32(data[off:off+4], 2048); off += 4
	binary.BigEndian.PutUint32(data[off:off+4], 8); off += 4
	binary.BigEndian.PutUint16(data[off:off+2], 51515); off += 2
	binary.BigEndian.PutUint16(data[off:off+2], 443); off += 2
	data[off] = 6; off++
	binary.BigEndian.PutUint32(data[off:off+4], 99_000)

	records, stats, err = ParseV9(data, exporter, "test", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.RecordsDecoded != 1 || len(records) != 1 {
		t.Fatalf("data parse stats=%+v records=%d", stats, len(records))
	}
	r := records[0]
	if r.SrcAddr.String() != "10.1.1.10" || r.DstAddr.String() != "172.16.1.20" {
		t.Fatalf("bad addresses: %+v", r)
	}
	if r.Bytes != 2048 || r.Packets != 8 || r.DstPort != 443 || r.Protocol != 6 {
		t.Fatalf("bad fields: %+v", r)
	}
}

func TestParseV9CiscoLike55Fallback(t *testing.T) {
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)
	packet := make([]byte, V9HeaderLen+4+55)
	binary.BigEndian.PutUint16(packet[0:2], 9)
	binary.BigEndian.PutUint16(packet[2:4], 1)
	binary.BigEndian.PutUint32(packet[4:8], 100_000)
	binary.BigEndian.PutUint32(packet[8:12], 1_700_000_000)
	binary.BigEndian.PutUint32(packet[12:16], 99)
	binary.BigEndian.PutUint32(packet[16:20], 1)
	binary.BigEndian.PutUint16(packet[20:22], 256)
	binary.BigEndian.PutUint16(packet[22:24], 59)
	row := packet[24:]
	binary.BigEndian.PutUint64(row[0:8], 66)
	binary.BigEndian.PutUint32(row[8:12], 1)
	row[12] = 6
	row[13] = 0
	row[14] = 0x1b
	binary.BigEndian.PutUint16(row[15:17], 50677)
	copy(row[17:21], net.ParseIP("10.21.2.11").To4())
	binary.BigEndian.PutUint32(row[21:25], 7)
	binary.BigEndian.PutUint16(row[25:27], 80)
	copy(row[27:31], net.ParseIP("104.18.20.226").To4())
	binary.BigEndian.PutUint32(row[31:35], 5)
	binary.BigEndian.PutUint32(row[35:39], 99_000)
	binary.BigEndian.PutUint32(row[39:43], 98_000)
	copy(row[51:55], net.ParseIP("0.8.164.143").To4())

	records, stats, err := ParseV9(packet, net.ParseIP("192.168.41.103"), "test", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.RecordsDecoded != 1 || len(records) != 1 {
		t.Fatalf("stats=%+v records=%d", stats, len(records))
	}
	r := records[0]
	if r.ExporterIP.String() != "192.168.41.103" || r.SrcAddr.String() != "10.21.2.11" || r.DstAddr.String() != "104.18.20.226" {
		t.Fatalf("bad record addresses: %+v", r)
	}
	if r.Bytes != 66 || r.Packets != 1 || r.DstPort != 80 || r.InputSNMP != 7 || r.OutputSNMP != 5 {
		t.Fatalf("bad record counters/ports/interfaces: %+v", r)
	}
}
