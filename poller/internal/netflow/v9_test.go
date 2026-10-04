package netflow

import (
	"encoding/binary"
	"testing"
	"time"
)

func TestParseV9TemplateAndData(t *testing.T) {
	cache := NewV9TemplateCache()
	rec := []fv{
		{ieSrcIPv4, 4, "10.1.1.10"}, {ieDstIPv4, 4, "172.16.1.20"},
		{ieBytes, 4, 2048}, {iePackets, 4, 8},
		{ieSrcPort, 2, 51515}, {ieDstPort, 2, 443}, {ieProtocol, 1, 6},
		{ieLastSwitched, 4, 99_000},
	}
	tplPkt := v9Packet(77, 1, v9TemplateSet(256, fieldsOf(rec)))
	records, stats, err := ParseV9(tplPkt, addr("192.0.2.10"), "test", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 0 || stats.TemplatesUpdated != 1 || stats.TemplatesChanged != 1 || len(stats.Templates) != 1 {
		t.Fatalf("template parse stats=%+v records=%d", stats, len(records))
	}

	dataPkt := v9Packet(77, 2, v9Set(256, encodeRecords(t, rec)))
	records, stats, err = ParseV9(dataPkt, addr("192.0.2.10"), "test", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if stats.RecordsDecoded != 1 || len(records) != 1 || stats.Domain != 77 || stats.Sequence != 2 {
		t.Fatalf("data parse stats=%+v records=%d", stats, len(records))
	}
	r := records[0]
	if r.SrcAddr.String() != "10.1.1.10" || r.DstAddr.String() != "172.16.1.20" {
		t.Fatalf("bad addresses: %+v", r)
	}
	if r.Bytes != 2048 || r.Packets != 8 || r.DstPort != 443 || r.Protocol != 6 {
		t.Fatalf("bad fields: %+v", r)
	}
	if r.ObservationDomain != 77 || r.ExportProtocol != ProtocolNetFlow9 || r.FlowVersion != 9 {
		t.Fatalf("identity: %+v", r)
	}
	if want := time.Unix(int64(testExportSecs), 0).Add(-time.Second); !r.Timestamp.Equal(want) {
		t.Fatalf("timestamp %s want %s", r.Timestamp, want)
	}
	want := FieldTracked | FieldL4Ports | FieldFlowTimes
	if r.FieldsPresent != want {
		t.Fatalf("fields_present = %#x, want %#x", r.FieldsPresent, want)
	}
	// A refresh of the same template is not re-announced to the registry.
	_, stats, _ = ParseV9(tplPkt, addr("192.0.2.10"), "test", testReceivedAt, cache)
	if stats.TemplatesUpdated != 1 || stats.TemplatesChanged != 0 || len(stats.Templates) != 0 {
		t.Fatalf("unchanged template refresh must not be re-registered: %+v", stats)
	}
}

// A data set arriving before its template is NOT force-decoded (BUG-01).
func TestParseV9TemplatelessDataNotForceDecoded(t *testing.T) {
	cache := NewV9TemplateCache()
	body := make([]byte, 55)
	binary.BigEndian.PutUint64(body[0:8], 303_465_212_429_312)
	records, stats, err := ParseV9(buildV9FlowSet(1, 99, 256, body), addr("192.168.41.103"), "test", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 0 || stats.RecordsDecoded != 0 || stats.DataSetsWaiting != 1 {
		t.Fatalf("expected 0 decoded / 1 waiting, got %d records, %+v", len(records), stats)
	}
}

// Options data is metadata, never flows, and every record in the set is read.
func TestParseV9OptionsSamplersPerID(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("192.0.2.20")
	// Options template 300: scope System(1,4); options FLOW_SAMPLER_ID(48,1) + SAMPLING_INTERVAL(34,4).
	otpl := v9OptionsTemplateSet(300, []V9TemplateField{{Type: 1, Length: 4}}, []V9TemplateField{{Type: ieSamplerID, Length: 1}, {Type: ieSamplingInterval, Length: 4}})
	// Two sampler definitions in ONE options data set: 1 -> 1:100, 2 -> 1:1000.
	odata := v9Set(300, encodeRecords(t,
		[]fv{{1, 4, 0}, {ieSamplerID, 1, 1}, {ieSamplingInterval, 4, 100}},
		[]fv{{1, 4, 0}, {ieSamplerID, 1, 2}, {ieSamplingInterval, 4, 1000}},
	))
	recs, st, err := ParseV9(v9Packet(7, 1, otpl, odata), exp, "t", testReceivedAt, cache)
	if err != nil || len(recs) != 0 {
		t.Fatalf("options must not produce flows: err=%v recs=%d", err, len(recs))
	}
	if st.OptionsRecords != 2 || st.SamplersLearned != 2 {
		t.Fatalf("both options records must be read: %+v", st)
	}

	tagged := func(sampler int) []fv {
		return append(basic4("10.0.0.1", "10.0.0.2", 500, 5, 6), fv{ieSamplerID, 1, sampler})
	}
	out, _ := decodeV9(t, cache, "192.0.2.20", 256, tagged(2), tagged(1), tagged(9))
	if len(out) != 3 {
		t.Fatalf("records=%d", len(out))
	}
	if out[0].SamplingInterval != 1000 || out[1].SamplingInterval != 100 {
		t.Fatalf("per-sampler rates: got %d/%d want 1000/100", out[0].SamplingInterval, out[1].SamplingInterval)
	}
	// An unknown sampler ID on a domain whose samplers disagree and that has
	// no default is not scaled by an arbitrary one of them (X-08): it stays
	// 1 and is flagged.
	if out[2].SamplingInterval != 1 || out[2].FieldsPresent&FieldSamplingAmbiguous == 0 {
		t.Fatalf("unknown sampler fallback = %d bits=%#x", out[2].SamplingInterval, out[2].FieldsPresent)
	}
	for _, r := range out[:2] {
		if r.FieldsPresent&FieldSamplingAmbiguous != 0 {
			t.Fatal("a record naming its sampler is not ambiguous")
		}
	}
	for _, r := range out {
		if r.FieldsPresent&FieldSamplingInline != 0 {
			t.Fatal("options-learned sampling is not inline")
		}
	}
}

// X-08: a record that names no sampler must not take the rate of whichever
// per-ID sampler the exporter announced last. Same options, two orders: the
// untagged record's rate is the same (unscaled and flagged) both times; a
// domain default announced without an ID applies; one shared rate applies.
func TestUntaggedRecordsIgnoreSamplerAnnouncementOrder(t *testing.T) {
	otpl := v9OptionsTemplateSet(300, []V9TemplateField{{Type: 1, Length: 4}}, []V9TemplateField{{Type: ieSamplerID, Length: 1}, {Type: ieSamplingInterval, Length: 4}})
	sampler := func(id, rate int) []fv { return []fv{{1, 4, 0}, {ieSamplerID, 1, id}, {ieSamplingInterval, 4, rate}} }
	untagged := basic4("10.95.30.1", "10.95.30.2", 1000, 10, 6)
	resolve := func(cache *V9TemplateCache, exp string, sets ...[]byte) Record {
		t.Helper()
		if _, _, err := ParseV9(v9Packet(7, 1, sets...), addr(exp), "t", testReceivedAt, cache); err != nil { // decodeV9 uses source ID 7
			t.Fatal(err)
		}
		out, _ := decodeV9(t, cache, exp, 256, untagged)
		if len(out) != 1 {
			t.Fatalf("records=%d", len(out))
		}
		return out[0]
	}
	for _, order := range [][2][]fv{{sampler(1, 100), sampler(2, 1000)}, {sampler(2, 1000), sampler(1, 100)}} {
		cache := NewV9TemplateCache()
		r := resolve(cache, "192.0.2.95", otpl, v9Set(300, encodeRecords(t, order[0], order[1])))
		if r.SamplingInterval != 1 || r.Bytes != 1000 || r.FieldsPresent&FieldSamplingAmbiguous == 0 {
			t.Fatalf("order %v: interval=%d bytes=%d bits=%#x", order, r.SamplingInterval, r.Bytes, r.FieldsPresent)
		}
		// Re-announced in the other order on the same cache: still the same.
		r = resolve(cache, "192.0.2.95", v9Set(300, encodeRecords(t, order[1], order[0])))
		if r.SamplingInterval != 1 || r.FieldsPresent&FieldSamplingAmbiguous == 0 {
			t.Fatalf("re-announced: interval=%d bits=%#x", r.SamplingInterval, r.FieldsPresent)
		}
	}
	// An explicit domain default (options record without a sampler ID) is
	// the rate of untagged records, whatever per-ID samplers exist.
	cache := NewV9TemplateCache()
	dtpl := v9OptionsTemplateSet(301, []V9TemplateField{{Type: 1, Length: 4}}, []V9TemplateField{{Type: ieSamplingInterval, Length: 4}})
	r := resolve(cache, "192.0.2.96", otpl, dtpl,
		v9Set(300, encodeRecords(t, sampler(1, 100), sampler(2, 1000))),
		v9Set(301, encodeRecords(t, []fv{{1, 4, 0}, {ieSamplingInterval, 4, 10}})))
	if r.SamplingInterval != 10 || r.FieldsPresent&FieldSamplingAmbiguous != 0 {
		t.Fatalf("domain default: interval=%d bits=%#x", r.SamplingInterval, r.FieldsPresent)
	}
	// Samplers that all share one rate are unambiguous.
	cache = NewV9TemplateCache()
	r = resolve(cache, "192.0.2.97", otpl, v9Set(300, encodeRecords(t, sampler(1, 100), sampler(2, 100))))
	if r.SamplingInterval != 100 || r.FieldsPresent&FieldSamplingAmbiguous != 0 {
		t.Fatalf("one shared rate: interval=%d bits=%#x", r.SamplingInterval, r.FieldsPresent)
	}
	// A sampler changing its rate updates the index: 2 -> 100 makes the
	// domain unambiguous again.
	cache = NewV9TemplateCache()
	resolve(cache, "192.0.2.98", otpl, v9Set(300, encodeRecords(t, sampler(1, 100), sampler(2, 1000))))
	r = resolve(cache, "192.0.2.98", v9Set(300, encodeRecords(t, sampler(2, 100))))
	if r.SamplingInterval != 100 || r.FieldsPresent&FieldSamplingAmbiguous != 0 {
		t.Fatalf("after rate change: interval=%d bits=%#x", r.SamplingInterval, r.FieldsPresent)
	}
}

func TestParseV9SamplingPrecedence(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := "192.0.2.21"
	// Domain default 1:50 from an options record without a sampler ID.
	otpl := v9OptionsTemplateSet(300, []V9TemplateField{{Type: 1, Length: 4}}, []V9TemplateField{{Type: ieSamplingInterval, Length: 4}})
	odata := v9Set(300, encodeRecords(t, []fv{{1, 4, 0}, {ieSamplingInterval, 4, 50}}))
	if _, _, err := ParseV9(v9Packet(7, 1, otpl, odata), addr(exp), "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name   string
		extra  []fv
		want   uint32
		inline bool
	}{
		{"inline IE 34 wins", []fv{{ieSamplingInterval, 4, 512}}, 512, true},
		{"inline PSAMP 1 of 100", []fv{{iePsampInterval, 4, 1}, {iePsampSpace, 4, 99}}, 100, true},
		{"domain default", nil, 50, false},
	}
	for i, c := range cases {
		rec := append(basic4("10.0.0.1", "10.0.0.2", 500, 5, 6), c.extra...)
		out, _ := decodeV9(t, cache, exp, uint16(400+i), rec)
		if len(out) != 1 || out[0].SamplingInterval != c.want || (out[0].FieldsPresent&FieldSamplingInline != 0) != c.inline {
			t.Errorf("%s: got %+v", c.name, out)
		}
	}
	// No sampling information at all: unsampled.
	out, _ := decodeV9(t, NewV9TemplateCache(), exp, 256, basic4("10.0.0.1", "10.0.0.2", 500, 5, 6))
	if out[0].SamplingInterval != 1 {
		t.Fatalf("unsampled interval = %d", out[0].SamplingInterval)
	}
}

// NF-32: IPv6 endpoints are decoded and stored as IPv6, never as 0.0.0.0.
func TestParseV9IPv6(t *testing.T) {
	rec := []fv{
		{ieBytes, 8, 5000}, {iePackets, 8, 7}, {ieProtocol, 1, 6}, {ieTCPFlags, 1, 0x1b},
		{ieSrcPort, 2, 50000}, {ieSrcIPv6, 16, "2001:db8::10"}, {ieSrcMaskIPv6, 1, 64},
		{ieInputIf, 4, 3}, {ieDstPort, 2, 443}, {ieDstIPv6, 16, "2001:db8:ffff::20"},
		{ieDstMaskIPv6, 1, 48}, {ieOutputIf, 4, 4}, {ieNextHopIPv6, 16, "fe80::1"},
	}
	out, st := decodeV9(t, NewV9TemplateCache(), "192.0.2.30", 257, rec)
	if len(out) != 1 || st.RecordsUnsupported != 0 {
		t.Fatalf("records=%d stats=%+v", len(out), st)
	}
	r := out[0]
	if r.IPVersion != 6 || r.SrcAddr != nil || r.DstAddr != nil {
		t.Fatalf("IPv6 flow must leave the IPv4 columns empty: %+v", r)
	}
	if r.SrcAddr6.String() != "2001:db8::10" || r.DstAddr6.String() != "2001:db8:ffff::20" || r.NextHop6.String() != "fe80::1" {
		t.Fatalf("IPv6 addresses: src=%v dst=%v nh=%v", r.SrcAddr6, r.DstAddr6, r.NextHop6)
	}
	if r.SrcMask != 64 || r.DstMask != 48 || r.Bytes != 5000 || r.TCPFlags != 0x1b {
		t.Fatalf("fields: %+v", r)
	}
	want := FieldTracked | FieldTCPFlags | FieldL4Ports | FieldInputIf | FieldOutputIf | FieldMasks | FieldNextHop
	if r.FieldsPresent != want {
		t.Fatalf("fields_present %#x want %#x", r.FieldsPresent, want)
	}
	if !st.Templates[0].Capabilities().IPv6 || st.Templates[0].Capabilities().TCPFlags != true {
		t.Fatalf("capabilities: %+v", st.Templates[0].Capabilities())
	}
}

// A dual-stack template carries both families and zero-fills the unused one.
func TestParseV9DualStackTemplate(t *testing.T) {
	cache := NewV9TemplateCache()
	mk := func(s4, d4, s6, d6 string) []fv {
		return []fv{
			{ieSrcIPv4, 4, s4}, {ieDstIPv4, 4, d4}, {ieSrcIPv6, 16, s6}, {ieDstIPv6, 16, d6},
			{ieBytes, 4, 100}, {iePackets, 4, 1}, {ieProtocol, 1, 17},
		}
	}
	out, _ := decodeV9(t, cache, "192.0.2.31", 300,
		mk("10.0.0.1", "10.0.0.2", "::", "::"),
		mk("0.0.0.0", "0.0.0.0", "2001:db8::1", "2001:db8::2"),
	)
	if len(out) != 2 || out[0].IPVersion != 4 || out[0].SrcAddr.String() != "10.0.0.1" || out[0].SrcAddr6 != nil {
		t.Fatalf("v4 row: %+v", out)
	}
	if out[1].IPVersion != 6 || out[1].SrcAddr6.String() != "2001:db8::1" || out[1].SrcAddr != nil {
		t.Fatalf("v6 row: %+v", out[1])
	}
}

// A record without both endpoints is dropped and counted, never stored as 0.0.0.0.
func TestParseV9RecordWithoutAddressesDropped(t *testing.T) {
	cases := [][]fv{
		{{ieBytes, 4, 100}, {iePackets, 4, 1}, {ieProtocol, 1, 6}},
		{{ieSrcIPv4, 4, "10.0.0.1"}, {ieBytes, 4, 100}, {iePackets, 4, 1}},
		{{ieDstIPv6, 16, "2001:db8::1"}, {ieBytes, 4, 100}, {iePackets, 4, 1}},
	}
	for i, rec := range cases {
		out, st := decodeV9(t, NewV9TemplateCache(), "192.0.2.32", 256, rec)
		if len(out) != 0 || st.RecordsUnsupported != 1 {
			t.Errorf("case %d: records=%d unsupported=%d", i, len(out), st.RecordsUnsupported)
		}
	}
}

// NF-32 / X-12: two IPv6 exporters announcing the same template ID with
// different layouts must each decode with their own template.
func TestParseV9IPv6ExportersDoNotShareTemplates(t *testing.T) {
	cache := NewV9TemplateCache()
	a := []fv{{ieSrcIPv4, 4, "10.0.0.1"}, {ieDstIPv4, 4, "10.0.0.2"}, {ieBytes, 4, 1000}, {iePackets, 4, 10}}
	b := []fv{{iePackets, 4, 20}, {ieBytes, 4, 2000}, {ieDstIPv4, 4, "10.9.9.2"}, {ieSrcIPv4, 4, "10.9.9.1"}}
	pa := v9Packet(0, 1, v9TemplateSet(256, fieldsOf(a)))
	pb := v9Packet(0, 1, v9TemplateSet(256, fieldsOf(b)))
	if _, _, err := ParseV9(pa, addr("2001:db8::a"), "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ParseV9(pb, addr("2001:db8::b"), "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	outA, _, _ := ParseV9(v9Packet(0, 2, v9Set(256, encodeRecords(t, a))), addr("2001:db8::a"), "t", testReceivedAt, cache)
	outB, _, _ := ParseV9(v9Packet(0, 2, v9Set(256, encodeRecords(t, b))), addr("2001:db8::b"), "t", testReceivedAt, cache)
	if len(outA) != 1 || outA[0].SrcAddr.String() != "10.0.0.1" || outA[0].Bytes != 1000 || outA[0].ExporterAddr != addr("2001:db8::a") {
		t.Fatalf("exporter A decoded with the wrong template: %+v", outA)
	}
	if len(outB) != 1 || outB[0].SrcAddr.String() != "10.9.9.1" || outB[0].Bytes != 2000 || outB[0].ExporterAddr != addr("2001:db8::b") {
		t.Fatalf("exporter B decoded with the wrong template: %+v", outB)
	}
	if outA[0].ExporterIP != nil {
		t.Fatalf("IPv6 exporter must not be stored as an IPv4 address: %v", outA[0].ExporterIP)
	}
	if got := cache.ActiveTemplates(); got[DomainKey{addr("2001:db8::a"), 0}] != 1 || got[DomainKey{addr("2001:db8::b"), 0}] != 1 {
		t.Fatalf("active templates per exporter: %v", got)
	}
}

// NF-33: 32-bit ifIndexes survive; the 2-byte encoding still works.
func TestParseV9IfIndexWidth(t *testing.T) {
	cases := []struct {
		name      string
		in, out   fv
		wantIn    uint32
		wantOut   uint32
		legacyIn  uint16
		legacyOut uint16
	}{
		{"4-byte 70000", fv{ieInputIf, 4, 70000}, fv{ieOutputIf, 4, 436207616}, 70000, 436207616, 0, 0},
		{"2-byte", fv{ieInputIf, 2, 3}, fv{ieOutputIf, 2, 65535}, 3, 65535, 3, 65535},
		{"physical fallback", fv{ieIngressPhysIf, 4, 70001}, fv{ieEgressPhysIf, 4, 5}, 70001, 5, 0, 5},
	}
	for i, c := range cases {
		rec := append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), c.in, c.out)
		out, st := decodeV9(t, NewV9TemplateCache(), "192.0.2.40", uint16(256+i), rec)
		r := out[0]
		if r.InputSNMP != c.wantIn || r.OutputSNMP != c.wantOut {
			t.Errorf("%s: in=%d out=%d", c.name, r.InputSNMP, r.OutputSNMP)
		}
		if LegacyIfIndex(r.InputSNMP) != c.legacyIn || LegacyIfIndex(r.OutputSNMP) != c.legacyOut {
			t.Errorf("%s: legacy columns in=%d out=%d", c.name, LegacyIfIndex(r.InputSNMP), LegacyIfIndex(r.OutputSNMP))
		}
		if r.FieldsPresent&(FieldInputIf|FieldOutputIf) != FieldInputIf|FieldOutputIf {
			t.Errorf("%s: interface presence bits missing", c.name)
		}
		if c.wantIn > 0xFFFF && !st.Templates[0].Capabilities().IfIndex32 {
			t.Errorf("%s: template capability has_ifindex32 not set", c.name)
		}
	}
	// The logical ifIndex wins over the physical one when both are exported.
	rec := append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieIngressPhysIf, 4, 9}, fv{ieInputIf, 4, 70000})
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.40", 300, rec)
	if out[0].InputSNMP != 70000 {
		t.Fatalf("logical ifIndex must win: %d", out[0].InputSNMP)
	}
}

// NF-07: a template without TCP flags leaves bit 0 clear; an explicit zero sets it.
func TestParseV9TCPFlagsPresence(t *testing.T) {
	absent, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.50", 256, basic4("10.0.0.1", "10.0.0.2", 100, 1, 6))
	if absent[0].FieldsPresent&FieldTCPFlags != 0 || absent[0].FieldsPresent&FieldTracked == 0 {
		t.Fatalf("flags absent: fields_present=%#x", absent[0].FieldsPresent)
	}
	zero, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.50", 256, append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieTCPFlags, 1, 0}))
	if zero[0].FieldsPresent&FieldTCPFlags == 0 || zero[0].TCPFlags != 0 {
		t.Fatalf("explicit zero flags: fields_present=%#x flags=%#x", zero[0].FieldsPresent, zero[0].TCPFlags)
	}
	wide, _ := decodeIPFIX(t, NewV9TemplateCache(), "192.0.2.50", 256, append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieTCPFlags, 2, 0x0112}))
	if wide[0].TCPFlags != 0x12 {
		t.Fatalf("16-bit tcpControlBits: %#x", wide[0].TCPFlags)
	}
}

// X-07: running totals (IE 85/86) never overwrite the delta counters.
// X-06: OUT_BYTES/OUT_PKTS next to different IN_BYTES/IN_PKTS are the
// responder direction of a session record, emitted as a second record; on a
// template without IE 1/2 they are the (egress) counters of the flow itself.
func TestParseV9CounterPrecedence(t *testing.T) {
	cases := []struct {
		name              string
		counters          []fv
		bytes, pkts       uint64
		revBytes, revPkts uint64 // 0/0: no reverse record
	}{
		{"delta then total", []fv{{ieBytes, 8, 1000}, {iePackets, 8, 10}, {ieTotalBytes, 8, 900000}, {ieTotalPackets, 8, 9000}}, 1000, 10, 0, 0},
		{"total then delta", []fv{{ieTotalBytes, 8, 900000}, {ieTotalPackets, 8, 9000}, {ieBytes, 8, 1000}, {iePackets, 8, 10}}, 1000, 10, 0, 0},
		{"totals only", []fv{{ieTotalBytes, 8, 900000}, {ieTotalPackets, 8, 9000}}, 900000, 9000, 0, 0},
		{"egress-only template", []fv{{ieOutBytes, 4, 9000}, {ieOutPackets, 4, 12}}, 9000, 12, 0, 0},
		{"bi-flow: responder sent everything", []fv{{ieBytes, 4, 0}, {iePackets, 4, 0}, {ieOutBytes, 4, 700}, {ieOutPackets, 4, 7}}, 0, 0, 700, 7},
		{"bi-flow: both directions", []fv{{ieBytes, 4, 100}, {iePackets, 4, 1}, {ieOutBytes, 4, 700}, {ieOutPackets, 4, 7}}, 100, 1, 700, 7},
		{"bi-flow: out before in", []fv{{ieOutBytes, 4, 700}, {ieOutPackets, 4, 7}, {ieBytes, 4, 100}, {iePackets, 4, 1}}, 100, 1, 700, 7},
		{"OUT equal to IN is a copy, not a direction", []fv{{ieBytes, 4, 500}, {iePackets, 4, 5}, {ieOutBytes, 4, 500}, {ieOutPackets, 4, 5}}, 500, 5, 0, 0},
		{"explicit zero delta", []fv{{ieBytes, 4, 0}, {iePackets, 4, 0}, {ieTotalBytes, 8, 5}}, 0, 0, 0, 0},
	}
	for i, c := range cases {
		rec := append([]fv{{ieSrcIPv4, 4, "10.0.0.3"}, {ieDstIPv4, 4, "10.0.0.4"}, {ieProtocol, 1, 17}}, c.counters...)
		out, st := decodeV9(t, NewV9TemplateCache(), "192.0.2.60", uint16(256+i), rec)
		wantRecords := 1
		if c.revBytes > 0 || c.revPkts > 0 {
			wantRecords = 2
		}
		if len(out) != wantRecords || st.RecordsDecoded != wantRecords {
			t.Fatalf("%s: %d records (stats %d), want %d", c.name, len(out), st.RecordsDecoded, wantRecords)
		}
		if out[0].Bytes != c.bytes || out[0].Packets != c.pkts {
			t.Errorf("%s: bytes=%d packets=%d want %d/%d", c.name, out[0].Bytes, out[0].Packets, c.bytes, c.pkts)
		}
		if wantRecords == 2 {
			rv := out[1]
			if rv.Bytes != c.revBytes || rv.Packets != c.revPkts || rv.SrcAddr.String() != "10.0.0.4" || rv.DstAddr.String() != "10.0.0.3" ||
				rv.FieldsPresent&FieldBiflowReverse == 0 || out[0].FieldsPresent&FieldBiflowReverse != 0 {
				t.Errorf("%s: reverse record %+v", c.name, rv)
			}
		}
	}
}

// In IPFIX, IE 23/24 are postOctetDeltaCount/postPacketDeltaCount (RFC 5102):
// the same direction counted after the observation point, never the reply.
// IN/OUT both present and different must NOT become a reverse record there
// (that inference is NetFlow v9 only); IE 1/2 stay the counters, and IE 23/24
// stand in only when IE 1/2 are absent. Explicit reverse IEs still split.
func TestParseIPFIXPostCountersAreNotAReverseDirection(t *testing.T) {
	cases := []struct {
		name        string
		counters    []fv
		bytes, pkts uint64
		records     int
	}{
		{"pre and post NAT counts differ", []fv{{ieBytes, 4, 1000}, {iePackets, 4, 10}, {ieOutBytes, 4, 960}, {ieOutPackets, 4, 10}}, 1000, 10, 1},
		{"post counters only", []fv{{ieOutBytes, 4, 9000}, {ieOutPackets, 4, 12}}, 9000, 12, 1},
		{"post before pre", []fv{{ieOutBytes, 4, 700}, {ieOutPackets, 4, 7}, {ieBytes, 4, 100}, {iePackets, 4, 1}}, 100, 1, 1},
		{"responder counters still split", []fv{{ieInitiatorOctets, 4, 100}, {ieInitiatorPackets, 4, 1}, {ieResponderOctets, 4, 700}, {ieResponderPackets, 4, 7}}, 100, 1, 2},
	}
	for i, c := range cases {
		rec := append([]fv{{ieSrcIPv4, 4, "10.0.0.3"}, {ieDstIPv4, 4, "10.0.0.4"}, {ieProtocol, 1, 17}}, c.counters...)
		out, st := decodeIPFIX(t, NewV9TemplateCache(), "192.0.2.61", uint16(300+i), rec)
		if len(out) != c.records || st.RecordsDecoded != c.records {
			t.Fatalf("%s: %d records (stats %d), want %d", c.name, len(out), st.RecordsDecoded, c.records)
		}
		if out[0].Bytes != c.bytes || out[0].Packets != c.pkts || out[0].FieldsPresent&FieldBiflowReverse != 0 {
			t.Errorf("%s: forward %d/%d fields %032b, want %d/%d", c.name, out[0].Bytes, out[0].Packets, out[0].FieldsPresent, c.bytes, c.pkts)
		}
		if c.records == 2 && (out[1].FieldsPresent&FieldBiflowReverse == 0 || out[1].Bytes != 700) {
			t.Errorf("%s: reverse %+v", c.name, out[1])
		}
	}
}

// ICMP type/code is normalised into dst_port (type*256+code) from IE 32 /
// 139 / 176-179; non-ICMP traffic keeps its ports.
func TestParseICMPNormalisation(t *testing.T) {
	cases := []struct {
		name    string
		src     string
		dst     string
		proto   int
		fields  []fv
		want    uint16
		present bool
	}{
		{"v4 typecode IE 32", "10.0.0.1", "10.0.0.2", 1, []fv{{ieICMPTypeCodeIPv4, 2, 0x0303}}, 0x0303, true},
		{"v4 type/code IE 176/177", "10.0.0.1", "10.0.0.2", 1, []fv{{ieICMPTypeIPv4, 1, 8}, {ieICMPCodeIPv4, 1, 0}}, 0x0800, true},
		{"v6 IE 178/179", "2001:db8::1", "2001:db8::2", 58, []fv{{ieICMPTypeIPv6, 1, 128}, {ieICMPCodeIPv6, 1, 0}}, 0x8000, true},
		{"v6 typecode IE 139", "2001:db8::1", "2001:db8::2", 58, []fv{{ieICMPTypeCodeIPv6, 2, 0x0104}}, 0x0104, true},
		{"dst port convention kept", "10.0.0.1", "10.0.0.2", 1, []fv{{ieDstPort, 2, 0x0800}}, 0x0800, false},
		{"TCP ignores IE 32", "10.0.0.1", "10.0.0.2", 6, []fv{{ieDstPort, 2, 443}, {ieICMPTypeCodeIPv4, 2, 0}}, 443, false},
	}
	for i, c := range cases {
		n := 4
		srcIE, dstIE := ieSrcIPv4, ieDstIPv4
		if c.proto == 58 {
			n, srcIE, dstIE = 16, ieSrcIPv6, ieDstIPv6
		}
		rec := append([]fv{{srcIE, uint16(n), c.src}, {dstIE, uint16(n), c.dst}, {ieBytes, 4, 84}, {iePackets, 4, 1}, {ieProtocol, 1, c.proto}}, c.fields...)
		out, _ := decodeIPFIX(t, NewV9TemplateCache(), "192.0.2.70", uint16(256+i), rec)
		if out[0].DstPort != c.want || (out[0].FieldsPresent&FieldICMPType != 0) != c.present {
			t.Errorf("%s: dst_port=%#04x icmp bit=%v", c.name, out[0].DstPort, out[0].FieldsPresent&FieldICMPType != 0)
		}
	}
}

func TestParseV9DirectionEndReasonDomain(t *testing.T) {
	rec := append(basic4("10.0.0.1", "10.0.0.2", 100, 1, 6), fv{ieDirection, 1, 1}, fv{ieFlowEndReason, 1, 3})
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.80", 256, rec)
	r := out[0]
	if r.FlowDirection != 1 || r.FieldsPresent&FieldDirection == 0 || r.FlowEndReason != 3 || r.ObservationDomain != 7 {
		t.Fatalf("direction=%d end_reason=%d domain=%d bits=%#x", r.FlowDirection, r.FlowEndReason, r.ObservationDomain, r.FieldsPresent)
	}
	out, _ = decodeV9(t, NewV9TemplateCache(), "192.0.2.80", 256, basic4("10.0.0.1", "10.0.0.2", 100, 1, 6))
	if out[0].FlowDirection != DirectionUnknown || out[0].FieldsPresent&FieldDirection != 0 {
		t.Fatalf("absent direction must be unknown (255): %d", out[0].FlowDirection)
	}
}

// X-12: a data template and an options template sharing an ID evict each other.
func TestParseV9TemplateKindSwitch(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("192.0.2.90")
	flow := basic4("10.0.0.1", "10.0.0.2", 100, 1, 6)
	if _, _, err := ParseV9(v9Packet(7, 1, v9TemplateSet(300, fieldsOf(flow))), exp, "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	// Exporter reboots and re-uses ID 300 for its sampler options template.
	otpl := v9OptionsTemplateSet(300, []V9TemplateField{{Type: 1, Length: 4}}, []V9TemplateField{{Type: ieSamplingInterval, Length: 4}})
	odata := v9Set(300, encodeRecords(t, []fv{{1, 4, 0}, {ieSamplingInterval, 4, 64}}))
	recs, st, err := ParseV9(v9Packet(7, 2, otpl, odata), exp, "t", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 0 || st.OptionsRecords != 1 || st.SamplersLearned != 1 {
		t.Fatalf("options data decoded with the stale data template: recs=%d stats=%+v", len(recs), st)
	}
	if n, ok := cache.Sampler(exp, 7, 0, false); !ok || n != 64 {
		t.Fatalf("sampler not learned: %d %v", n, ok)
	}
	// ...and back: the data template evicts the options template.
	if _, _, err := ParseV9(v9Packet(7, 3, v9TemplateSet(300, fieldsOf(flow))), exp, "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	if _, ok := cache.GetOptions(exp, 7, 300); ok {
		t.Fatal("options template survived the data template that replaced it")
	}
	recs, _, _ = ParseV9(v9Packet(7, 4, v9Set(300, encodeRecords(t, flow))), exp, "t", testReceivedAt, cache)
	if len(recs) != 1 || recs[0].SamplingInterval != 64 {
		t.Fatalf("flow after switch back: %+v", recs)
	}
}

func TestParseV9AbsoluteTimes(t *testing.T) {
	const endMs = uint64(1_700_000_005_000)
	rec := append(basic4("10.0.0.3", "10.0.0.4", 9000, 12, 17), fv{ieFlowStartMillis, 8, endMs - 4000}, fv{ieFlowEndMillis, 8, endMs})
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.95", 256, rec)
	r := out[0]
	if uint64(r.Timestamp.UnixMilli()) != endMs || r.LastSwitchedMS != endMs || r.FirstSwitchedMS != endMs-4000 {
		t.Fatalf("flowEnd/StartMilliseconds: ts=%v first=%d last=%d", r.Timestamp, r.FirstSwitchedMS, r.LastSwitchedMS)
	}
	if r.FieldsPresent&FieldFlowTimes == 0 {
		t.Fatal("flow times bit missing")
	}
}

// Garbage and truncated input never panics.
func TestParseV9Malformed(t *testing.T) {
	cache := NewV9TemplateCache()
	rec := basic4("10.0.0.1", "10.0.0.2", 100, 1, 6)
	good := v9Packet(7, 1, v9TemplateSet(256, fieldsOf(rec)), v9Set(256, encodeRecords(t, rec)))
	for cut := 0; cut < len(good); cut++ {
		_, _, _ = ParseV9(good[:cut], addr("192.0.2.99"), "t", testReceivedAt, cache)
	}
	junk := append([]byte(nil), good...)
	for i := V9HeaderLen; i < len(junk); i++ {
		junk[i] ^= 0xA5
		_, _, _ = ParseV9(junk, addr("192.0.2.99"), "t", testReceivedAt, cache)
	}
}
