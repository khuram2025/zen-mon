package netflow

import (
	"encoding/binary"
	"net/netip"
	"testing"
	"time"
)

// Test packet builders. Each record is a list of fv (IE, length, value) so a
// test states exactly which fields its template carries; the template is
// derived from the first record's field list.

type fv struct {
	ie  uint16
	len uint16
	val any // uint64/int (big-endian integer), string (IP address), []byte (raw)
}

var (
	testReceivedAt = time.Unix(1_700_000_010, 0).UTC()
	testExportSecs = uint32(1_700_000_000)
)

const testSysUptime = 100_000

func addr(s string) netip.Addr { return netip.MustParseAddr(s) }

func encodeFV(t testing.TB, f fv) []byte {
	t.Helper()
	b := make([]byte, f.len)
	switch v := f.val.(type) {
	case int:
		putUint(b, uint64(v))
	case uint64:
		putUint(b, v)
	case string:
		a := netip.MustParseAddr(v)
		if f.len == 4 {
			a4 := a.As4()
			copy(b, a4[:])
		} else {
			a16 := a.As16()
			copy(b, a16[:])
		}
	case []byte:
		copy(b, v)
	default:
		t.Fatalf("unsupported test value %T", f.val)
	}
	return b
}

func putUint(b []byte, v uint64) {
	for i := len(b) - 1; i >= 0; i-- {
		b[i] = byte(v)
		v >>= 8
	}
}

func fieldsOf(rec []fv) []V9TemplateField {
	out := make([]V9TemplateField, len(rec))
	for i, f := range rec {
		out[i] = V9TemplateField{Type: f.ie, Length: f.len}
	}
	return out
}

func encodeRecords(t testing.TB, recs ...[]fv) []byte {
	t.Helper()
	var body []byte
	for _, rec := range recs {
		for _, f := range rec {
			body = append(body, encodeFV(t, f)...)
		}
	}
	return body
}

func pad4(b []byte) []byte {
	for len(b)%4 != 0 {
		b = append(b, 0)
	}
	return b
}

// ── NetFlow v9 ────────────────────────────────────────────────────────────

func v9Set(id uint16, body []byte) []byte {
	body = pad4(body)
	out := make([]byte, 4+len(body))
	binary.BigEndian.PutUint16(out[0:2], id)
	binary.BigEndian.PutUint16(out[2:4], uint16(4+len(body)))
	copy(out[4:], body)
	return out
}

func v9Packet(sourceID, seq uint32, sets ...[]byte) []byte {
	var body []byte
	for _, s := range sets {
		body = append(body, s...)
	}
	pkt := make([]byte, V9HeaderLen+len(body))
	binary.BigEndian.PutUint16(pkt[0:2], 9)
	binary.BigEndian.PutUint16(pkt[2:4], 1)
	binary.BigEndian.PutUint32(pkt[4:8], testSysUptime)
	binary.BigEndian.PutUint32(pkt[8:12], testExportSecs)
	binary.BigEndian.PutUint32(pkt[12:16], seq)
	binary.BigEndian.PutUint32(pkt[16:20], sourceID)
	copy(pkt[V9HeaderLen:], body)
	return pkt
}

// buildV9FlowSet wraps a single FlowSet body in a NetFlow v9 packet header.
func buildV9FlowSet(sourceID, sequence uint32, flowSetID uint16, body []byte) []byte {
	return v9Packet(sourceID, sequence, v9Set(flowSetID, body))
}

func v9TemplateSet(id uint16, fields []V9TemplateField) []byte {
	b := make([]byte, 4+4*len(fields))
	binary.BigEndian.PutUint16(b[0:2], id)
	binary.BigEndian.PutUint16(b[2:4], uint16(len(fields)))
	for i, f := range fields {
		binary.BigEndian.PutUint16(b[4+i*4:], f.Type)
		binary.BigEndian.PutUint16(b[6+i*4:], f.Length)
	}
	return v9Set(0, b)
}

func v9OptionsTemplateSet(id uint16, scope, fields []V9TemplateField) []byte {
	b := make([]byte, 6)
	binary.BigEndian.PutUint16(b[0:2], id)
	binary.BigEndian.PutUint16(b[2:4], uint16(4*len(scope)))
	binary.BigEndian.PutUint16(b[4:6], uint16(4*len(fields)))
	for _, f := range append(append([]V9TemplateField{}, scope...), fields...) {
		b = binary.BigEndian.AppendUint16(b, f.Type)
		b = binary.BigEndian.AppendUint16(b, f.Length)
	}
	return v9Set(1, b)
}

// decodeV9 sends the template for recs[0] and then the data set in one
// datagram and returns the decoded records.
func decodeV9(t *testing.T, cache *V9TemplateCache, exporter string, id uint16, recs ...[]fv) ([]Record, V9Stats) {
	t.Helper()
	pkt := v9Packet(7, 1, v9TemplateSet(id, fieldsOf(recs[0])), v9Set(id, encodeRecords(t, recs...)))
	out, st, err := ParseV9(pkt, addr(exporter), "test", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	return out, st
}

// ── IPFIX ─────────────────────────────────────────────────────────────────

func ipfixSet(setID uint16, content []byte) []byte {
	b := make([]byte, 4+len(content))
	binary.BigEndian.PutUint16(b[0:2], setID)
	binary.BigEndian.PutUint16(b[2:4], uint16(4+len(content)))
	copy(b[4:], content)
	return b
}

func ipfixMsg(domain, seq uint32, sets ...[]byte) []byte {
	var body []byte
	for _, s := range sets {
		body = append(body, s...)
	}
	msg := make([]byte, IPFIXHeaderLen+len(body))
	binary.BigEndian.PutUint16(msg[0:2], 10)
	binary.BigEndian.PutUint16(msg[2:4], uint16(IPFIXHeaderLen+len(body)))
	binary.BigEndian.PutUint32(msg[4:8], testExportSecs)
	binary.BigEndian.PutUint32(msg[8:12], seq)
	binary.BigEndian.PutUint32(msg[12:16], domain)
	copy(msg[16:], body)
	return msg
}

// ipfixTemplate builds a Template Set (set 2) record for templateID.
func ipfixTemplate(templateID uint16, fields []V9TemplateField) []byte {
	var c []byte
	c = binary.BigEndian.AppendUint16(c, templateID)
	c = binary.BigEndian.AppendUint16(c, uint16(len(fields)))
	for _, f := range fields {
		c = binary.BigEndian.AppendUint16(c, f.Type)
		c = binary.BigEndian.AppendUint16(c, f.Length)
		if f.Type&0x8000 != 0 {
			c = binary.BigEndian.AppendUint32(c, f.Enterprise)
		}
	}
	return ipfixSet(2, c)
}

func ipfixOptionsTemplate(templateID uint16, scope, fields []V9TemplateField) []byte {
	var c []byte
	c = binary.BigEndian.AppendUint16(c, templateID)
	c = binary.BigEndian.AppendUint16(c, uint16(len(scope)+len(fields)))
	c = binary.BigEndian.AppendUint16(c, uint16(len(scope)))
	for _, f := range append(append([]V9TemplateField{}, scope...), fields...) {
		c = binary.BigEndian.AppendUint16(c, f.Type)
		c = binary.BigEndian.AppendUint16(c, f.Length)
	}
	return ipfixSet(3, c)
}

func decodeIPFIX(t *testing.T, cache *V9TemplateCache, exporter string, id uint16, recs ...[]fv) ([]Record, V9Stats) {
	t.Helper()
	msg := ipfixMsg(42, 1, ipfixTemplate(id, fieldsOf(recs[0])), ipfixSet(id, encodeRecords(t, recs...)))
	out, st, err := ParseIPFIX(msg, addr(exporter), "test", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	return out, st
}

// basic4 is a minimal IPv4 flow: addresses, counters, protocol and ports.
func basic4(src, dst string, bytes, packets int, proto int) []fv {
	return []fv{
		{ieSrcIPv4, 4, src}, {ieDstIPv4, 4, dst},
		{ieBytes, 4, bytes}, {iePackets, 4, packets},
		{ieProtocol, 1, proto}, {ieSrcPort, 2, 40000}, {ieDstPort, 2, 443},
	}
}
