package netflow

import (
	"testing"
	"time"
)

// Cisco ASA/FTD NSEL (CR-18). The templates below follow the ASA NSEL guide:
// session records carry initiator/responder byte and packet deltas instead of
// IE 1/2, the firewall event, post-NAT addresses/ports and the event time.

const (
	nselConnID   uint16 = 148 // NF_F_CONN_ID
	nselExtEvent uint16 = 33002
)

var testEventMS = uint64(testExportSecs)*1000 - 1_500

// nselTeardown is an ASA flow-teardown record: 10.20.1.10:51000 ->
// 142.250.74.46:443, source-translated to 198.51.100.7:61000.
func nselTeardown(event int, fwdBytes, revBytes, fwdPkts, revPkts int) []fv {
	return []fv{
		{nselConnID, 4, 7001},
		{ieSrcIPv4, 4, "10.20.1.10"}, {ieDstIPv4, 4, "142.250.74.46"},
		{ieProtocol, 1, 6}, {ieSrcPort, 2, 51000}, {ieDstPort, 2, 443},
		{ieInputIf, 2, 3}, {ieOutputIf, 2, 4},
		{ieXlateSrcIPv4, 4, "198.51.100.7"}, {ieXlateDstIPv4, 4, "142.250.74.46"},
		{ieXlateSrcPort, 2, 61000}, {ieXlateDstPort, 2, 443},
		{ieFirewallEvent, 1, event}, {nselExtEvent, 2, 2030},
		{ieEventTimeMillis, 8, testEventMS}, {ieFlowStartMillis, 8, testEventMS - 30_000},
		{ieInitiatorOctets, 4, fwdBytes}, {ieResponderOctets, 4, revBytes},
		{ieInitiatorPackets, 4, fwdPkts}, {ieResponderPackets, 4, revPkts},
	}
}

func TestParseNSELTeardownSplitsSession(t *testing.T) {
	out, st := decodeV9(t, NewV9TemplateCache(), "192.0.2.70", 263, nselTeardown(int(FwEventDeleted), 5000, 80000, 40, 70))
	if len(out) != 2 || st.RecordsDecoded != 2 {
		t.Fatalf("a teardown with responder traffic is two directional records, got %d (%+v)", len(out), st)
	}
	fwd, rev := out[0], out[1]
	eventAt := time.UnixMilli(int64(testEventMS)).UTC()

	if fwd.Bytes != 5000 || fwd.Packets != 40 || fwd.FwEvent != FwEventDeleted {
		t.Fatalf("forward counters/event: %+v", fwd)
	}
	if fwd.SrcAddr.String() != "10.20.1.10" || fwd.DstAddr.String() != "142.250.74.46" || fwd.SrcPort != 51000 || fwd.DstPort != 443 {
		t.Fatalf("forward endpoints: %+v", fwd)
	}
	if fwd.XlateSrc.String() != "198.51.100.7" || fwd.XlateSrcPort != 61000 || fwd.XlateDst.String() != "142.250.74.46" || fwd.XlateDstPort != 443 {
		t.Fatalf("forward NAT: %+v", fwd)
	}
	if !fwd.Timestamp.Equal(eventAt) || fwd.LastSwitchedMS != testEventMS || fwd.FirstSwitchedMS != testEventMS-30_000 {
		t.Fatalf("event time: ts=%s last=%d first=%d", fwd.Timestamp, fwd.LastSwitchedMS, fwd.FirstSwitchedMS)
	}
	want := FieldTracked | FieldL4Ports | FieldInputIf | FieldOutputIf | FieldFlowTimes | FieldNAT | FieldFwEvent
	if fwd.FieldsPresent != want {
		t.Fatalf("forward fields_present %#x want %#x", fwd.FieldsPresent, want)
	}

	// The reverse half: everything swapped, the responder counters, same event.
	if rev.Bytes != 80000 || rev.Packets != 70 || rev.FwEvent != FwEventDeleted {
		t.Fatalf("reverse counters/event: %+v", rev)
	}
	if rev.SrcAddr.String() != "142.250.74.46" || rev.DstAddr.String() != "10.20.1.10" || rev.SrcPort != 443 || rev.DstPort != 51000 {
		t.Fatalf("reverse endpoints: %+v", rev)
	}
	if rev.InputSNMP != 4 || rev.OutputSNMP != 3 {
		t.Fatalf("reverse interfaces: in=%d out=%d", rev.InputSNMP, rev.OutputSNMP)
	}
	if rev.XlateSrc.String() != "142.250.74.46" || rev.XlateSrcPort != 443 || rev.XlateDst.String() != "198.51.100.7" || rev.XlateDstPort != 61000 {
		t.Fatalf("reverse NAT: %+v", rev)
	}
	if rev.FieldsPresent != want|FieldBiflowReverse || !rev.Timestamp.Equal(eventAt) {
		t.Fatalf("reverse fields_present %#x want %#x, ts %s", rev.FieldsPresent, want|FieldBiflowReverse, rev.Timestamp)
	}
}

// Create and deny events carry no traffic: one record each (the volume guard
// excludes fw_event 1/3 from every volume aggregate), never an empty reverse.
func TestParseNSELEventsWithoutTraffic(t *testing.T) {
	cache := NewV9TemplateCache()
	created, _ := decodeV9(t, cache, "192.0.2.70", 263, nselTeardown(int(FwEventCreated), 0, 0, 0, 0))
	if len(created) != 1 || created[0].FwEvent != FwEventCreated || created[0].Bytes != 0 {
		t.Fatalf("create event: %+v", created)
	}

	// An ICMP echo denied by the ingress ACL: type/code in IE 176/177, no NAT.
	deny := []fv{
		{nselConnID, 4, 0}, {ieSrcIPv4, 4, "203.0.113.66"}, {ieDstIPv4, 4, "198.51.100.10"},
		{ieProtocol, 1, 1}, {ieICMPTypeIPv4, 1, 8}, {ieICMPCodeIPv4, 1, 0},
		{ieInputIf, 2, 4}, {ieOutputIf, 2, 0},
		{ieFirewallEvent, 1, int(FwEventDenied)}, {nselExtEvent, 2, 1001},
		{ieEventTimeMillis, 8, testEventMS},
	}
	out, _ := decodeV9(t, cache, "192.0.2.70", 264, deny)
	if len(out) != 1 {
		t.Fatalf("deny event: %d records", len(out))
	}
	d := out[0]
	if d.FwEvent != FwEventDenied || d.DstPort != 0x0800 || d.Bytes != 0 || d.Packets != 0 || d.XlateSrc != nil {
		t.Fatalf("deny event: %+v", d)
	}
	if d.FieldsPresent&(FieldFwEvent|FieldICMPType) != FieldFwEvent|FieldICMPType || d.FieldsPresent&FieldNAT != 0 {
		t.Fatalf("deny fields_present %#x", d.FieldsPresent)
	}
}

// ASA releases before 8.4 export the vendor numbers 40001-40005; the IANA
// numbers win when a template carries both.
func TestParseNSELLegacyFields(t *testing.T) {
	legacy := []fv{
		{ieSrcIPv4, 4, "10.1.1.1"}, {ieDstIPv4, 4, "10.2.2.2"}, {ieProtocol, 1, 17},
		{ieNSELXlateSrc84, 4, "198.51.100.9"}, {ieNSELXlateDst84, 4, "0.0.0.0"},
		{ieNSELXlateSrcPort84, 2, 40001}, {ieNSELXlateDstPort84, 2, 53},
		{ieNSELFwEvent84, 1, int(FwEventUpdate)}, {ieInitiatorOctets, 4, 300},
	}
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.71", 270, legacy)
	r := out[0]
	if r.FwEvent != FwEventUpdate || r.XlateSrc.String() != "198.51.100.9" || r.XlateSrcPort != 40001 || r.XlateDstPort != 53 || r.Bytes != 300 {
		t.Fatalf("legacy NSEL fields: %+v", r)
	}
	// 0.0.0.0 means "not translated": stored as none, never as an endpoint.
	if r.XlateDst != nil || r.FieldsPresent&(FieldNAT|FieldFwEvent) != FieldNAT|FieldFwEvent {
		t.Fatalf("untranslated destination / presence: xlate_dst=%v fields=%#x", r.XlateDst, r.FieldsPresent)
	}

	both := []fv{
		{ieSrcIPv4, 4, "10.1.1.1"}, {ieDstIPv4, 4, "10.2.2.2"}, {ieProtocol, 1, 6},
		{ieNSELFwEvent84, 1, int(FwEventAlert)}, {ieFirewallEvent, 1, int(FwEventDeleted)},
		{ieNSELXlateSrc84, 4, "198.51.100.1"}, {ieXlateSrcIPv4, 4, "198.51.100.2"},
	}
	out, _ = decodeV9(t, NewV9TemplateCache(), "192.0.2.71", 271, both)
	if out[0].FwEvent != FwEventDeleted || out[0].XlateSrc.String() != "198.51.100.2" {
		t.Fatalf("IANA fields must win over the pre-8.4 ones: %+v", out[0])
	}
}

// In IPFIX 40005 carries the enterprise bit: it is some vendor's element, not
// the ASA firewall event, and must not be read as one.
func TestParseIPFIXEnterpriseFieldIsNotNSEL(t *testing.T) {
	rec := []fv{{ieSrcIPv4, 4, "10.1.1.1"}, {ieDstIPv4, 4, "10.2.2.2"}, {ieProtocol, 1, 6}, {ieNSELFwEvent84, 1, int(FwEventDenied)}, {ieBytes, 4, 10}}
	fields := fieldsOf(rec)
	fields[3].Enterprise = 9
	msg := ipfixMsg(1, 1, ipfixTemplate(256, fields), ipfixSet(256, encodeRecords(t, rec)))
	out, _, err := ParseIPFIX(msg, addr("192.0.2.72"), "t", testReceivedAt, NewV9TemplateCache())
	if err != nil || len(out) != 1 {
		t.Fatalf("err=%v out=%d", err, len(out))
	}
	if out[0].FwEvent != 0 || out[0].FieldsPresent&FieldFwEvent != 0 || out[0].Bytes != 10 {
		t.Fatalf("enterprise 40005 read as NSEL: %+v", out[0])
	}
}

// A template with both a flow end (IE 21) and the event time keeps the flow end.
func TestParseEventTimeIsFallback(t *testing.T) {
	rec := append(basic4("10.0.0.1", "10.0.0.2", 10, 1, 6), fv{ieEventTimeMillis, 8, testEventMS}, fv{ieLastSwitched, 4, 99_000})
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.73", 280, rec)
	if want := time.Unix(int64(testExportSecs), 0).Add(-time.Second); !out[0].Timestamp.Equal(want) {
		t.Fatalf("flow end must win over the event time: %s want %s", out[0].Timestamp, want)
	}
}

// RFC 5103 bidirectional IPFIX: reverse IEs are enterprise 29305 + the
// forward IE number. Reverse TCP flags are kept on the reverse record only.
func TestParseIPFIXRFC5103Biflow(t *testing.T) {
	rec := []fv{
		{ieSrcIPv6, 16, "2001:db8:1::10"}, {ieDstIPv6, 16, "2001:db8:2::20"}, {ieProtocol, 1, 6},
		{ieSrcPort, 2, 50000}, {ieDstPort, 2, 22}, {ieTCPFlags, 1, 0x02},
		{ieBytes, 8, 1200}, {iePackets, 8, 12},
		{0x8000 | ieBytes, 8, 64000}, {0x8000 | iePackets, 8, 50}, {0x8000 | ieTCPFlags, 1, 0x12},
		{ieNextHopIPv6, 16, "2001:db8::1"},
	}
	fields := fieldsOf(rec)
	for i := range fields {
		if fields[i].Type&0x8000 != 0 {
			fields[i].Enterprise = penReverse
		}
	}
	msg := ipfixMsg(5, 1, ipfixTemplate(300, fields), ipfixSet(300, encodeRecords(t, rec)))
	out, st, err := ParseIPFIX(msg, addr("2001:db8::99"), "t", testReceivedAt, NewV9TemplateCache())
	if err != nil || len(out) != 2 || st.DataRecords != 1 {
		t.Fatalf("err=%v records=%d data_records=%d", err, len(out), st.DataRecords)
	}
	fwd, rev := out[0], out[1]
	if fwd.Bytes != 1200 || fwd.Packets != 12 || fwd.TCPFlags != 0x02 || fwd.NextHop6 == nil {
		t.Fatalf("forward: %+v", fwd)
	}
	if rev.Bytes != 64000 || rev.Packets != 50 || rev.TCPFlags != 0x12 || rev.FieldsPresent&FieldTCPFlags == 0 {
		t.Fatalf("reverse counters/flags: %+v", rev)
	}
	if rev.SrcAddr6.String() != "2001:db8:2::20" || rev.DstAddr6.String() != "2001:db8:1::10" || rev.SrcPort != 22 || rev.DstPort != 50000 {
		t.Fatalf("reverse endpoints: %+v", rev)
	}
	// The next hop describes the forward path only.
	if rev.NextHop6 != nil || rev.FieldsPresent&FieldNextHop != 0 || rev.FieldsPresent&FieldBiflowReverse == 0 {
		t.Fatalf("reverse next hop / flags: %+v", rev)
	}
}

// Without exported reverse flags the reverse record has none: the reply half
// of a SYN-only probe must not become a second SYN-only flow. ICMP type/code
// is the request's, not the reply's.
func TestParseBiflowReverseDropsForwardOnlyFields(t *testing.T) {
	tcp := []fv{
		{ieSrcIPv4, 4, "10.0.0.1"}, {ieDstIPv4, 4, "10.0.0.2"}, {ieProtocol, 1, 6}, {ieTCPFlags, 1, 0x02},
		{ieSrcPort, 2, 40000}, {ieDstPort, 2, 80}, {ieNextHopIPv4, 4, "10.0.0.254"},
		{ieBytes, 4, 60}, {iePackets, 4, 1}, {ieOutBytes, 4, 40}, {ieOutPackets, 4, 1},
		{ieSrcAS, 4, 64500}, {ieDstAS, 4, 15169}, {ieSrcMask, 1, 24}, {ieDstMask, 1, 16},
	}
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.74", 290, tcp)
	if len(out) != 2 {
		t.Fatalf("records: %d", len(out))
	}
	rev := out[1]
	if rev.TCPFlags != 0 || rev.FieldsPresent&FieldTCPFlags != 0 || rev.NextHop != nil {
		t.Fatalf("reverse inherited forward-only fields: %+v", rev)
	}
	if rev.SrcAS != 15169 || rev.DstAS != 64500 || rev.SrcMask != 16 || rev.DstMask != 24 {
		t.Fatalf("reverse AS/masks not swapped: %+v", rev)
	}

	icmp := []fv{
		{ieSrcIPv4, 4, "10.0.0.1"}, {ieDstIPv4, 4, "10.0.0.2"}, {ieProtocol, 1, 1}, {ieICMPTypeCodeIPv4, 2, 0x0800},
		{ieBytes, 4, 84}, {iePackets, 4, 1}, {ieOutBytes, 4, 168}, {ieOutPackets, 4, 2},
	}
	out, _ = decodeV9(t, NewV9TemplateCache(), "192.0.2.74", 291, icmp)
	if out[0].DstPort != 0x0800 || out[1].DstPort != 0 || out[1].SrcPort != 0 || out[1].FieldsPresent&FieldICMPType != 0 {
		t.Fatalf("ICMP reverse: fwd=%+v rev=%+v", out[0], out[1])
	}
}
