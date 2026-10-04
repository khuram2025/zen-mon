package netflow

import (
	"encoding/binary"
	"testing"
)

func TestParseIPFIXTemplateAndData(t *testing.T) {
	cache := NewV9TemplateCache()
	rec := []fv{{ieSrcIPv4, 4, "192.168.1.1"}, {ieDstIPv4, 4, "8.8.8.8"}, {ieBytes, 4, 4096}, {iePackets, 4, 20}, {ieProtocol, 1, 6}, {ieDstPort, 2, 443}}
	recs, stats := decodeIPFIX(t, cache, "203.0.113.5", 256, rec)
	if stats.TemplatesUpdated != 1 || stats.RecordsDecoded != 1 || len(recs) != 1 || stats.DataRecords != 1 || stats.Domain != 42 {
		t.Fatalf("stats=%+v recs=%d", stats, len(recs))
	}
	r := recs[0]
	if r.FlowVersion != 10 || r.ExportProtocol != ProtocolIPFIX || r.ObservationDomain != 42 {
		t.Fatalf("identity: %+v", r)
	}
	if r.SrcAddr.String() != "192.168.1.1" || r.DstAddr.String() != "8.8.8.8" {
		t.Fatalf("bad addrs: %+v", r)
	}
	if r.Bytes != 4096 || r.Packets != 20 || r.Protocol != 6 || r.DstPort != 443 {
		t.Fatalf("bad fields: %+v", r)
	}
	// No flow time exported: the record is placed at receipt time.
	if !r.Timestamp.Equal(testReceivedAt) || r.FieldsPresent&FieldFlowTimes != 0 {
		t.Fatalf("timestamp=%v bits=%#x", r.Timestamp, r.FieldsPresent)
	}
}

func TestParseIPFIXIPv6(t *testing.T) {
	rec := []fv{
		{ieSrcIPv6, 16, "2001:db8:20::10"}, {ieDstIPv6, 16, "2001:db8:ffff::443"},
		{ieBytes, 8, 30000}, {iePackets, 8, 25}, {ieProtocol, 1, 6}, {ieSrcPort, 2, 50000}, {ieDstPort, 2, 443},
		{ieFlowStartMillis, 8, 1_699_999_990_000}, {ieFlowEndMillis, 8, 1_700_000_000_000},
	}
	out, st := decodeIPFIX(t, NewV9TemplateCache(), "2001:db8::62", 257, rec)
	if len(out) != 1 || st.RecordsUnsupported != 0 {
		t.Fatalf("records=%d stats=%+v", len(out), st)
	}
	r := out[0]
	if r.IPVersion != 6 || r.SrcAddr6.String() != "2001:db8:20::10" || r.DstAddr6.String() != "2001:db8:ffff::443" || r.SrcAddr != nil {
		t.Fatalf("addresses: %+v", r)
	}
	if r.ExporterAddr != addr("2001:db8::62") || r.ExporterIP != nil {
		t.Fatalf("exporter identity: %v / %v", r.ExporterAddr, r.ExporterIP)
	}
	if r.FieldsPresent&FieldTCPFlags != 0 {
		t.Fatal("template without TCP flags must leave bit 0 clear")
	}
}

func TestParseIPFIXSamplingFromOptions(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("203.0.113.9")
	// Options template 300: scope observationDomainId(149,4) + samplingInterval(34,4).
	otpl := ipfixOptionsTemplate(300, []V9TemplateField{{Type: 149, Length: 4}}, []V9TemplateField{{Type: ieSamplingInterval, Length: 4}})
	odata := ipfixSet(300, encodeRecords(t, []fv{{149, 4, 42}, {ieSamplingInterval, 4, 2048}}))
	_, stats, err := ParseIPFIX(ipfixMsg(42, 1, otpl, odata), exp, "t", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.OptionsTemplatesUpdated != 1 || stats.SamplersLearned != 1 || stats.OptionsRecords != 1 || stats.DataRecords != 1 {
		t.Fatalf("stats=%+v", stats)
	}
	if n, ok := cache.Sampler(exp, 42, 0, false); !ok || n != 2048 {
		t.Fatalf("ipfix sampler = %d, want 2048", n)
	}
}

// PSAMP: a selector selecting 1 packet then skipping 99 is 1-in-100 (X-08),
// keyed by selectorId so flows naming the selector get its rate.
func TestParseIPFIXPSAMPSelector(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("203.0.113.10")
	otpl := ipfixOptionsTemplate(301, []V9TemplateField{{Type: ieSelectorID, Length: 8}}, []V9TemplateField{{Type: iePsampInterval, Length: 4}, {Type: iePsampSpace, Length: 4}})
	odata := ipfixSet(301, encodeRecords(t,
		[]fv{{ieSelectorID, 8, 5}, {iePsampInterval, 4, 1}, {iePsampSpace, 4, 99}},
		[]fv{{ieSelectorID, 8, 6}, {iePsampInterval, 4, 1}, {iePsampSpace, 4, 999}},
	))
	if _, _, err := ParseIPFIX(ipfixMsg(42, 1, otpl, odata), exp, "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	rec := append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieSelectorID, 8, 6})
	out, _ := decodeIPFIX(t, cache, "203.0.113.10", 256, rec)
	if out[0].SamplingInterval != 1000 {
		t.Fatalf("selector 6 rate = %d, want 1000", out[0].SamplingInterval)
	}
}

// IE 21/22 are relative to systemInitTimeMilliseconds (IE 160) in IPFIX.
func TestParseIPFIXSysUptimeWithInitTime(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("203.0.113.11")
	const initMS = uint64(1_699_990_000_000)
	otpl := ipfixOptionsTemplate(302, []V9TemplateField{{Type: 149, Length: 4}}, []V9TemplateField{{Type: ieSystemInitTimeMillis, Length: 8}})
	odata := ipfixSet(302, encodeRecords(t, []fv{{149, 4, 42}, {ieSystemInitTimeMillis, 8, initMS}}))
	rec := append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieFirstSwitched, 4, 1_000_000}, fv{ieLastSwitched, 4, 1_005_000})
	msg := ipfixMsg(42, 1, otpl, odata, ipfixTemplate(256, fieldsOf(rec)), ipfixSet(256, encodeRecords(t, rec)))
	out, _, err := ParseIPFIX(msg, exp, "t", testReceivedAt, cache)
	if err != nil || len(out) != 1 {
		t.Fatalf("err=%v out=%d", err, len(out))
	}
	if got := uint64(out[0].Timestamp.UnixMilli()); got != initMS+1_005_000 || out[0].FirstSwitchedMS != initMS+1_000_000 {
		t.Fatalf("uptime conversion: ts=%d first=%d", got, out[0].FirstSwitchedMS)
	}
	// Without an init time the receipt time is used.
	out, _ = decodeIPFIX(t, NewV9TemplateCache(), "203.0.113.12", 256, rec)
	if !out[0].Timestamp.Equal(testReceivedAt) {
		t.Fatalf("no init time: ts=%v", out[0].Timestamp)
	}
}

func TestParseIPFIXWithdrawal(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("203.0.113.13")
	rec := basic4("10.0.0.1", "10.0.0.2", 100, 1, 6)
	decodeIPFIX(t, cache, "203.0.113.13", 256, rec)
	decodeIPFIX(t, cache, "203.0.113.13", 257, rec)
	withdraw := make([]byte, 4)
	binary.BigEndian.PutUint16(withdraw[0:2], 256)
	if _, _, err := ParseIPFIX(ipfixMsg(42, 2, ipfixSet(2, withdraw)), exp, "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	if _, ok := cache.Get(exp, 42, 256); ok {
		t.Fatal("template 256 was not withdrawn")
	}
	if _, ok := cache.Get(exp, 42, 257); !ok {
		t.Fatal("template 257 must survive a single withdrawal")
	}
	binary.BigEndian.PutUint16(withdraw[0:2], 2) // all data templates
	_, _, _ = ParseIPFIX(ipfixMsg(42, 3, ipfixSet(2, withdraw)), exp, "t", testReceivedAt, cache)
	if _, ok := cache.Get(exp, 42, 257); ok {
		t.Fatal("withdraw-all left template 257")
	}
	// Data for a withdrawn template waits, and the record count is unknown.
	_, st, _ := ParseIPFIX(ipfixMsg(42, 4, ipfixSet(257, encodeRecords(t, rec))), exp, "t", testReceivedAt, cache)
	if st.DataSetsWaiting != 1 || st.DataRecords != -1 {
		t.Fatalf("stats=%+v", st)
	}
}

// Enterprise-specific and variable-length fields keep alignment and never
// alias standard IEs.
func TestParseIPFIXEnterpriseAndVarlen(t *testing.T) {
	cache := NewV9TemplateCache()
	fields := []V9TemplateField{
		{Type: ieSrcIPv4, Length: 4}, {Type: 0x8000 | ieBytes, Length: 4, Enterprise: 9}, // enterprise IE numbered like octetDeltaCount
		{Type: 96, Length: 0xFFFF}, // applicationName, variable length
		{Type: ieDstIPv4, Length: 4}, {Type: ieBytes, Length: 4}, {Type: iePackets, Length: 4},
	}
	var body []byte
	body = append(body, 10, 0, 0, 1)
	body = binary.BigEndian.AppendUint32(body, 777777) // enterprise value, must be ignored
	body = append(body, 5)
	body = append(body, "hello"...)
	body = append(body, 10, 0, 0, 2)
	body = binary.BigEndian.AppendUint32(body, 1234)
	body = binary.BigEndian.AppendUint32(body, 3)
	body = append(body, 0, 0, 0) // padding
	out, st, err := ParseIPFIX(ipfixMsg(42, 1, ipfixTemplate(256, fields), ipfixSet(256, body)), addr("203.0.113.14"), "t", testReceivedAt, cache)
	if err != nil || len(out) != 1 {
		t.Fatalf("err=%v out=%d st=%+v", err, len(out), st)
	}
	if out[0].Bytes != 1234 || out[0].DstAddr.String() != "10.0.0.2" || out[0].Packets != 3 {
		t.Fatalf("varlen/enterprise alignment: %+v", out[0])
	}
	if len(st.Templates) != 1 || st.Templates[0].Fields[1].Enterprise != 9 {
		t.Fatalf("enterprise number not kept for the registry: %+v", st.Templates)
	}
}

func TestParseIPFIXNTPAndDeltaTimes(t *testing.T) {
	endMS := uint64(testExportSecs)*1000 - 2_000
	ntp := func(ms uint64) uint64 {
		secs := ms/1000 + ntpEpochOffset
		frac := ((ms % 1000) << 32) / 1000
		return secs<<32 | frac
	}
	rec := append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieFlowEndMicros, 8, ntp(endMS)}, fv{ieFlowStartDeltaMicros, 4, 5_000_000})
	out, _ := decodeIPFIX(t, NewV9TemplateCache(), "203.0.113.15", 256, rec)
	if got := uint64(out[0].Timestamp.UnixMilli()); got < endMS-1 || got > endMS {
		t.Fatalf("NTP microseconds end time: %d want %d", got, endMS)
	}
	if want := uint64(testExportSecs)*1000 - 5_000; out[0].FirstSwitchedMS != want {
		t.Fatalf("delta start: %d want %d", out[0].FirstSwitchedMS, want)
	}
}

func TestParseIPFIXMalformed(t *testing.T) {
	cache := NewV9TemplateCache()
	rec := basic4("10.0.0.1", "10.0.0.2", 100, 1, 6)
	good := ipfixMsg(42, 1, ipfixTemplate(256, append(fieldsOf(rec), V9TemplateField{Type: 96, Length: 0xFFFF})), ipfixSet(256, append(encodeRecords(t, rec), 255, 0, 3, 'a', 'b', 'c')))
	for cut := 0; cut < len(good); cut++ {
		_, _, _ = ParseIPFIX(good[:cut], addr("203.0.113.99"), "t", testReceivedAt, cache)
	}
	junk := append([]byte(nil), good...)
	for i := IPFIXHeaderLen; i < len(junk); i++ {
		junk[i] ^= 0x5A
		_, _, _ = ParseIPFIX(junk, addr("203.0.113.99"), "t", testReceivedAt, cache)
	}
}

// NF-32 verification repro: two IPv6 exporters announce IPFIX template 256 in
// domain 0 with different field orders. Keyed by the full exporter address,
// each decodes with its own layout (previously both collapsed onto 0.0.0.0
// and A's data was read with B's template: garbage addresses and byte counts).
func TestParseIPFIXIPv6ExportersSharingTemplateID(t *testing.T) {
	cache := NewV9TemplateCache()
	a := []fv{{ieSrcIPv6, 16, "2001:db8:a::10"}, {ieDstIPv6, 16, "2001:db8:a::20"}, {ieBytes, 8, 5000}, {iePackets, 8, 5}, {ieProtocol, 1, 6}}
	b := []fv{{ieProtocol, 1, 17}, {iePackets, 4, 9}, {ieBytes, 4, 900}, {ieDstIPv4, 4, "10.9.9.2"}, {ieSrcIPv4, 4, "10.9.9.1"}}
	send := func(exp string, seq uint32, sets ...[]byte) []Record {
		out, _, err := ParseIPFIX(ipfixMsg(0, seq, sets...), addr(exp), "t", testReceivedAt, cache)
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	send("2001:db8::a", 1, ipfixTemplate(256, fieldsOf(a)))
	send("2001:db8::b", 1, ipfixTemplate(256, fieldsOf(b)))
	outA := send("2001:db8::a", 1, ipfixSet(256, encodeRecords(t, a)))
	outB := send("2001:db8::b", 1, ipfixSet(256, encodeRecords(t, b)))
	if len(outA) != 1 || outA[0].SrcAddr6.String() != "2001:db8:a::10" || outA[0].Bytes != 5000 || outA[0].ExporterAddr != addr("2001:db8::a") || outA[0].ExporterIP != nil {
		t.Fatalf("exporter A: %+v", outA)
	}
	if len(outB) != 1 || outB[0].SrcAddr.String() != "10.9.9.1" || outB[0].Bytes != 900 || outB[0].Protocol != 17 || outB[0].ExporterAddr != addr("2001:db8::b") {
		t.Fatalf("exporter B: %+v", outB)
	}
}
