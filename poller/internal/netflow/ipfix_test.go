package netflow

import (
	"encoding/binary"
	"net"
	"testing"
	"time"
)

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
	binary.BigEndian.PutUint32(msg[4:8], 1_700_000_000)
	binary.BigEndian.PutUint32(msg[8:12], seq)
	binary.BigEndian.PutUint32(msg[12:16], domain)
	copy(msg[16:], body)
	return msg
}

// ipfixTemplate builds a Template Set (setID 2) record for templateID with the
// given (ieID,len) fields.
func ipfixTemplate(templateID uint16, fields []V9TemplateField) []byte {
	c := make([]byte, 4+len(fields)*4)
	binary.BigEndian.PutUint16(c[0:2], templateID)
	binary.BigEndian.PutUint16(c[2:4], uint16(len(fields)))
	for i, f := range fields {
		binary.BigEndian.PutUint16(c[4+i*4:6+i*4], f.Type)
		binary.BigEndian.PutUint16(c[6+i*4:8+i*4], f.Length)
	}
	return ipfixSet(2, c)
}

func TestParseIPFIXTemplateAndData(t *testing.T) {
	exporter := net.ParseIP("203.0.113.5").To4()
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	fields := []V9TemplateField{{8, 4}, {12, 4}, {1, 4}, {2, 4}, {4, 1}, {11, 2}}
	tmplSet := ipfixTemplate(256, fields)

	rec := make([]byte, 19)
	copy(rec[0:4], net.ParseIP("192.168.1.1").To4())
	copy(rec[4:8], net.ParseIP("8.8.8.8").To4())
	binary.BigEndian.PutUint32(rec[8:12], 4096)
	binary.BigEndian.PutUint32(rec[12:16], 20)
	rec[16] = 6
	binary.BigEndian.PutUint16(rec[17:19], 443)
	dataSet := ipfixSet(256, rec)

	msg := ipfixMsg(42, 1, tmplSet, dataSet)
	recs, stats, err := ParseIPFIX(msg, exporter, "t", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.TemplatesUpdated != 1 || stats.RecordsDecoded != 1 || len(recs) != 1 {
		t.Fatalf("stats=%+v recs=%d", stats, len(recs))
	}
	r := recs[0]
	if r.FlowVersion != 10 {
		t.Fatalf("flow version = %d, want 10", r.FlowVersion)
	}
	if r.SrcAddr.String() != "192.168.1.1" || r.DstAddr.String() != "8.8.8.8" {
		t.Fatalf("bad addrs: %+v", r)
	}
	if r.Bytes != 4096 || r.Packets != 20 || r.Protocol != 6 || r.DstPort != 443 {
		t.Fatalf("bad fields: %+v", r)
	}
}

func TestParseIPFIXSamplingFromOptions(t *testing.T) {
	exporter := net.ParseIP("203.0.113.9").To4()
	cache := NewV9TemplateCache()
	receivedAt := time.Unix(1_700_000_010, 0)

	// Options template 300: scope obsDomain(149,4) + option samplingInterval(34,4).
	otc := make([]byte, 6+8)
	binary.BigEndian.PutUint16(otc[0:2], 300)
	binary.BigEndian.PutUint16(otc[2:4], 2) // field count
	binary.BigEndian.PutUint16(otc[4:6], 1) // scope count
	binary.BigEndian.PutUint16(otc[6:8], 149)
	binary.BigEndian.PutUint16(otc[8:10], 4)
	binary.BigEndian.PutUint16(otc[10:12], 34)
	binary.BigEndian.PutUint16(otc[12:14], 4)
	optTmplSet := ipfixSet(3, otc)

	odr := make([]byte, 8)
	binary.BigEndian.PutUint32(odr[4:8], 2048) // sampling interval
	optDataSet := ipfixSet(300, odr)

	msg := ipfixMsg(42, 1, optTmplSet, optDataSet)
	_, stats, err := ParseIPFIX(msg, exporter, "t", receivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.OptionsTemplatesUpdated != 1 || stats.SamplersLearned != 1 {
		t.Fatalf("stats=%+v", stats)
	}
	if got := cache.GetSampler(exporter, 42); got != 2048 {
		t.Fatalf("ipfix sampler = %d, want 2048", got)
	}
}
