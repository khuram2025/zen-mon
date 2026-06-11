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

// buildV9FlowSet wraps a single FlowSet body in a NetFlow v9 packet header.
func buildV9FlowSet(sourceID, sequence uint32, flowSetID uint16, body []byte) []byte {
	pkt := make([]byte, V9HeaderLen+4+len(body))
	binary.BigEndian.PutUint16(pkt[0:2], 9)
	binary.BigEndian.PutUint16(pkt[2:4], 1)
	binary.BigEndian.PutUint32(pkt[4:8], 100_000)
	binary.BigEndian.PutUint32(pkt[8:12], 1_700_000_000)
	binary.BigEndian.PutUint32(pkt[12:16], sequence)
	binary.BigEndian.PutUint32(pkt[16:20], sourceID)
	binary.BigEndian.PutUint16(pkt[20:22], flowSetID)
	binary.BigEndian.PutUint16(pkt[22:24], uint16(4+len(body)))
	copy(pkt[24:], body)
	return pkt
}

// TestParseV9TemplatelessDataNotForceDecoded verifies that a data set arriving
// before its template is NOT force-decoded into a flow. The deleted 55-byte
// fallback used to do exactly this and produced exabyte-scale garbage byte
// counts that dominated every statistic (BUG-01).
func TestParseV9TemplatelessDataNotForceDecoded(t *testing.T) {
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	// 55-byte body whose first 8 bytes are a huge value: the exact shape the old
	// fallback decoded into a bytes>1e12 record.
	body := make([]byte, 55)
	binary.BigEndian.PutUint64(body[0:8], 303_465_212_429_312)
	packet := buildV9FlowSet(1, 99, 256, body)

	records, stats, err := ParseV9(packet, net.ParseIP("192.168.41.103"), "test", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 0 {
		t.Fatalf("template-less data must not be decoded, got %d records: %+v", len(records), records)
	}
	if stats.RecordsDecoded != 0 || stats.DataSetsWaiting != 1 {
		t.Fatalf("expected 0 decoded / 1 waiting, got %+v", stats)
	}
}

// TestParseV9OptionsDataSkipped verifies Options Templates are cached and their
// data records are skipped (never decoded as flows), even when the bytes would
// have looked like a huge flow under the old fallback (BUG-01).
func TestParseV9OptionsDataSkipped(t *testing.T) {
	exporter := net.ParseIP("192.0.2.10").To4()
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	// Options Template (FlowSet 1): templateID 256, 1 scope field, 2 option fields.
	tmplBody := make([]byte, 6+4+8)
	binary.BigEndian.PutUint16(tmplBody[0:2], 256) // template ID
	binary.BigEndian.PutUint16(tmplBody[2:4], 4)   // option scope length (1 field def)
	binary.BigEndian.PutUint16(tmplBody[4:6], 8)   // option length (2 field defs)
	binary.BigEndian.PutUint16(tmplBody[6:8], 1)   // scope field: System
	binary.BigEndian.PutUint16(tmplBody[8:10], 4)  // scope value len
	binary.BigEndian.PutUint16(tmplBody[10:12], 34) // SAMPLING_INTERVAL
	binary.BigEndian.PutUint16(tmplBody[12:14], 4)
	binary.BigEndian.PutUint16(tmplBody[14:16], 35) // SAMPLING_ALGORITHM
	binary.BigEndian.PutUint16(tmplBody[16:18], 1)
	tmplPkt := buildV9FlowSet(77, 1, 1, tmplBody)

	_, stats, err := ParseV9(tmplPkt, exporter, "test", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.OptionsTemplatesUpdated != 1 {
		t.Fatalf("expected 1 options template cached, got %+v", stats)
	}
	if _, ok := cache.GetOptions(exporter, 77, 256); !ok {
		t.Fatal("options template 256 was not cached")
	}

	// Options Data (FlowSet 256): would-be garbage under the old fallback.
	dataBody := make([]byte, 55)
	binary.BigEndian.PutUint64(dataBody[0:8], 8_286_623_314_361_712_640)
	dataPkt := buildV9FlowSet(77, 2, 256, dataBody)

	records, stats, err := ParseV9(dataPkt, exporter, "test", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 0 || stats.OptionsDataSkipped != 1 || stats.RecordsDecoded != 0 {
		t.Fatalf("options data must be skipped: records=%d stats=%+v", len(records), stats)
	}
}

// TestParseV9SamplingLearned verifies the sampler is read from options data and
// applied to the SamplingInterval of subsequent flow records (BUG-05).
func TestParseV9SamplingLearned(t *testing.T) {
	exporter := net.ParseIP("192.0.2.20").To4()
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	// Options template 300: scope System(1,4) + option SAMPLING_INTERVAL(34,4).
	otb := make([]byte, 6+4+4)
	binary.BigEndian.PutUint16(otb[0:2], 300)
	binary.BigEndian.PutUint16(otb[2:4], 4)  // scope length (1 field def)
	binary.BigEndian.PutUint16(otb[4:6], 4)  // option length (1 field def)
	binary.BigEndian.PutUint16(otb[6:8], 1)  // scope: System
	binary.BigEndian.PutUint16(otb[8:10], 4)
	binary.BigEndian.PutUint16(otb[10:12], 34) // SAMPLING_INTERVAL
	binary.BigEndian.PutUint16(otb[12:14], 4)
	if _, st, err := ParseV9(buildV9FlowSet(7, 1, 1, otb), exporter, "t", receivedAt, cache); err != nil || st.OptionsTemplatesUpdated != 1 {
		t.Fatalf("options template: err=%v stats=%+v", err, st)
	}

	// Options data: scope value (4 bytes) + sampling interval = 1000.
	odb := make([]byte, 8)
	binary.BigEndian.PutUint32(odb[4:8], 1000)
	if _, st, err := ParseV9(buildV9FlowSet(7, 2, 300, odb), exporter, "t", receivedAt, cache); err != nil || st.SamplersLearned != 1 {
		t.Fatalf("options data: err=%v stats=%+v", err, st)
	}
	if got := cache.GetSampler(exporter, 7); got != 1000 {
		t.Fatalf("sampler not learned: got %d want 1000", got)
	}

	// Flow template 256 (src,dst,bytes,pkts,proto) then a flow record.
	tfields := []V9TemplateField{{8, 4}, {12, 4}, {1, 4}, {2, 4}, {4, 1}}
	tb := make([]byte, 4+len(tfields)*4)
	binary.BigEndian.PutUint16(tb[0:2], 256)
	binary.BigEndian.PutUint16(tb[2:4], uint16(len(tfields)))
	for i, f := range tfields {
		binary.BigEndian.PutUint16(tb[4+i*4:6+i*4], f.Type)
		binary.BigEndian.PutUint16(tb[6+i*4:8+i*4], f.Length)
	}
	if _, _, err := ParseV9(buildV9FlowSet(7, 3, 0, tb), exporter, "t", receivedAt, cache); err != nil {
		t.Fatal(err)
	}
	db := make([]byte, 17)
	copy(db[0:4], net.ParseIP("10.0.0.1").To4())
	copy(db[4:8], net.ParseIP("10.0.0.2").To4())
	binary.BigEndian.PutUint32(db[8:12], 500)
	binary.BigEndian.PutUint32(db[12:16], 5)
	db[16] = 6
	recs, st, err := ParseV9(buildV9FlowSet(7, 4, 256, db), exporter, "t", receivedAt, cache)
	if err != nil || len(recs) != 1 {
		t.Fatalf("flow data: err=%v recs=%d stats=%+v", err, len(recs), st)
	}
	if recs[0].SamplingInterval != 1000 {
		t.Fatalf("flow record sampling interval = %d, want 1000 (raw bytes stay %d; collector multiplies at ingest)", recs[0].SamplingInterval, recs[0].Bytes)
	}
}

// TestParseV9NewFields verifies OUT_BYTES/OUT_PKTS (23/24) populate counters and
// flowEndMilliseconds (153) sets an absolute timestamp (BUG-06).
func TestParseV9NewFields(t *testing.T) {
	exporter := net.ParseIP("192.0.2.30").To4()
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	// Template 256: OUT_BYTES(23,4) OUT_PKTS(24,4) src(8,4) dst(12,4) proto(4,1) flowEndMs(153,8).
	tfields := []V9TemplateField{{23, 4}, {24, 4}, {8, 4}, {12, 4}, {4, 1}, {153, 8}}
	tb := make([]byte, 4+len(tfields)*4)
	binary.BigEndian.PutUint16(tb[0:2], 256)
	binary.BigEndian.PutUint16(tb[2:4], uint16(len(tfields)))
	for i, f := range tfields {
		binary.BigEndian.PutUint16(tb[4+i*4:6+i*4], f.Type)
		binary.BigEndian.PutUint16(tb[6+i*4:8+i*4], f.Length)
	}
	if _, _, err := ParseV9(buildV9FlowSet(1, 1, 0, tb), exporter, "t", receivedAt, cache); err != nil {
		t.Fatal(err)
	}
	const endMs = uint64(1_700_000_005_000)
	db := make([]byte, 25)
	binary.BigEndian.PutUint32(db[0:4], 9000) // OUT_BYTES
	binary.BigEndian.PutUint32(db[4:8], 12)    // OUT_PKTS
	copy(db[8:12], net.ParseIP("10.0.0.3").To4())
	copy(db[12:16], net.ParseIP("10.0.0.4").To4())
	db[16] = 17 // UDP
	binary.BigEndian.PutUint64(db[17:25], endMs)
	recs, _, err := ParseV9(buildV9FlowSet(1, 2, 256, db), exporter, "t", receivedAt, cache)
	if err != nil || len(recs) != 1 {
		t.Fatalf("err=%v recs=%d", err, len(recs))
	}
	r := recs[0]
	if r.Bytes != 9000 || r.Packets != 12 || r.Protocol != 17 {
		t.Fatalf("OUT_BYTES/OUT_PKTS not applied: %+v", r)
	}
	if uint64(r.Timestamp.UnixMilli()) != endMs {
		t.Fatalf("flowEndMilliseconds not applied: ts=%v want %d ms", r.Timestamp, endMs)
	}
}
