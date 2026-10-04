package main

import (
	"context"
	"fmt"
	"net"
	"net/netip"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/zenplus/poller/internal/netflow"
	"github.com/zenplus/poller/internal/sflow"
)

// Integration test against a real ClickHouse: the INSERT column list and the
// Append argument order must line up for all 45 flow_records columns, which no
// fake store can prove. It runs only when NETFLOW_TEST_CLICKHOUSE=host:port is
// set (password in NETFLOW_TEST_CLICKHOUSE_PASSWORD) and works in a throwaway
// database that it drops afterwards.
func TestClickHouseRoundTrip(t *testing.T) {
	addr := os.Getenv("NETFLOW_TEST_CLICKHOUSE")
	if addr == "" {
		t.Skip("NETFLOW_TEST_CLICKHOUSE not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	opts := func(db string) *clickhouse.Options {
		return &clickhouse.Options{
			Addr: []string{addr},
			Auth: clickhouse.Auth{Database: db, Username: "default", Password: os.Getenv("NETFLOW_TEST_CLICKHOUSE_PASSWORD")},
		}
	}
	admin, err := openClickHouse(opts("default"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	db := "nf_collector_it_" + strconv.FormatInt(time.Now().UnixNano(), 36)
	if err := admin.Exec(ctx, "CREATE DATABASE "+db); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = admin.Exec(context.Background(), "DROP DATABASE IF EXISTS "+db) }()

	conn, err := openClickHouse(opts(db))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	store := &chStore{conn: conn, collectorID: "it"}
	for i := 0; i < 2; i++ { // ensureSchema is replay-safe
		if err := store.EnsureSchema(ctx); err != nil {
			t.Fatalf("ensure schema (run %d): %v", i+1, err)
		}
	}

	now := time.Now().UTC().Truncate(time.Millisecond)
	v4 := netflow.Record{
		Timestamp: now, ReceivedAt: now, CollectorID: "it",
		ExporterIP: net.IPv4(192, 0, 2, 61).To4(), ExporterAddr: netip.MustParseAddr("192.0.2.61"),
		FlowVersion: 9, FlowSequence: 7, SamplingInterval: 100,
		SrcAddr: net.IPv4(10, 0, 0, 1).To4(), DstAddr: net.IPv4(10, 0, 0, 2).To4(), NextHop: net.IPv4(10, 0, 0, 254).To4(),
		InputSNMP: 70000, OutputSNMP: 3, Packets: 10, Bytes: 1111, SrcPort: 40000, DstPort: 443, Protocol: 6, TCPFlags: 0x1b,
		IPVersion: 4, FieldsPresent: netflow.FieldTracked | netflow.FieldTCPFlags | netflow.FieldInputIf,
		ObservationDomain: 42, FlowDirection: 1, ExportProtocol: netflow.ProtocolNetFlow9, FlowEndReason: 3,
	}
	v6 := netflow.Record{
		Timestamp: now, ReceivedAt: now, CollectorID: "it",
		ExporterAddr: netip.MustParseAddr("2001:db8::62"),
		FlowVersion:  10, SamplingInterval: 1,
		SrcAddr6: net.ParseIP("2001:db8:20::10"), DstAddr6: net.ParseIP("2001:db8:ffff::443"), NextHop6: net.ParseIP("fe80::1"),
		Packets: 1, Bytes: 2222, Protocol: 58, DstPort: 0x8000,
		IPVersion: 6, FieldsPresent: netflow.FieldTracked | netflow.FieldICMPType,
		ObservationDomain: 6, FlowDirection: netflow.DirectionUnknown, ExportProtocol: netflow.ProtocolIPFIX,
	}
	recs := []netflow.Record{v4, v6}
	for i := 0; i < 2; i++ { // the second insert reuses the token: deduplicated
		if err := store.InsertFlows(ctx, "it-token-1", recs); err != nil {
			t.Fatalf("insert (attempt %d): %v", i+1, err)
		}
	}

	rows, err := conn.Query(ctx, `SELECT toString(exporter_ip), toString(exporter_ip6), toString(src_addr), toString(dst_addr),
	    toString(src_addr6), toString(dst_addr6), toString(next_hop), toString(next_hop6),
	    input_snmp, output_snmp, input_if, output_if, ip_version, fields_present, observation_domain,
	    flow_direction, export_protocol, sampling_interval, bytes, dst_port, flow_end_reason, flow_version
	    FROM flow_records ORDER BY bytes`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var (
			exp4, exp6, src4, dst4, src6, dst6, nh4, nh6, proto string
			inLegacy, outLegacy, dstPort                        uint16
			inIf, outIf, fields, domain, sampling               uint32
			ipv, dir, endReason, version                        uint8
			bytes                                               uint64
		)
		if err := rows.Scan(&exp4, &exp6, &src4, &dst4, &src6, &dst6, &nh4, &nh6, &inLegacy, &outLegacy, &inIf, &outIf,
			&ipv, &fields, &domain, &dir, &proto, &sampling, &bytes, &dstPort, &endReason, &version); err != nil {
			t.Fatal(err)
		}
		got = append(got, fmt.Sprintf("%s|%s|%s|%s|%s|%s|%s|%s|%d/%d|%d/%d|v%d|%#x|%d|%d|%s|%d|%d|%d|%d|%d",
			exp4, exp6, src4, dst4, src6, dst6, nh4, nh6, inLegacy, outLegacy, inIf, outIf,
			ipv, fields, domain, dir, proto, sampling, bytes, dstPort, endReason, version))
	}
	want := []string{
		// IPv4 exporter and flow: *6 columns are ::; ifIndex 70000 is full width
		// in input_if and 0 (unknown) in the legacy 16-bit column.
		"192.0.2.61|::|10.0.0.1|10.0.0.2|::|::|10.0.0.254|::|0/3|70000/3|v4|0x80000009|42|1|netflow9|100|1111|443|3|9",
		// IPv6 exporter and flow: the IPv4 columns are 0.0.0.0, never an endpoint.
		"0.0.0.0|2001:db8::62|0.0.0.0|0.0.0.0|2001:db8:20::10|2001:db8:ffff::443|0.0.0.0|fe80::1|0/0|0/0|v6|0x80000800|6|255|ipfix|1|2222|32768|0|10",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("stored rows (a retried token must not duplicate them):\n got %q\nwant %q", got, want)
	}

	statsRows := []statsRow{
		{TS: now.Truncate(time.Minute), C: counters{InsertFailures: 2}, G: gauges{QueueLen: 5, QueueCap: 100}},
		{TS: now.Truncate(time.Minute), Exporter: "2001:db8::62", ObservationDomain: 6, ExportProtocol: netflow.ProtocolIPFIX,
			C: counters{Datagrams: 3, RecordsDecoded: 30, RecordsWritten: 30, SequenceGaps: 1}, ClockOffsetMS: -1500, SamplingInterval: 1, TemplatesActive: 2},
	}
	for i := 0; i < 2; i++ {
		if err := store.InsertStats(ctx, "it-stats-1", statsRows); err != nil {
			t.Fatalf("insert stats: %v", err)
		}
	}
	var n uint64
	var offset int64
	if err := conn.QueryRow(ctx, "SELECT count(), sum(clock_offset_ms) FROM flow_collector_stats").Scan(&n, &offset); err != nil || n != 2 || offset != -1500 {
		t.Fatalf("stats rows=%d offset=%d err=%v", n, offset, err)
	}

	info := netflow.TemplateInfo{Exporter: netip.MustParseAddr("2001:db8::62"), Domain: 6, Protocol: netflow.ProtocolIPFIX, ID: 256,
		Fields: []netflow.V9TemplateField{{Type: 27, Length: 16}, {Type: 28, Length: 16}, {Type: 10, Length: 4}, {Type: 0x8000 | 12, Length: 4, Enterprise: 9}}}
	if err := store.InsertTemplates(ctx, []templateRow{{info: info, firstSeen: now, lastSeen: now}}); err != nil {
		t.Fatalf("insert templates: %v", err)
	}
	var ids string
	var hasIPv6, hasIf32 uint8
	if err := conn.QueryRow(ctx, "SELECT toString(field_ids), has_ipv6, has_ifindex32 FROM flow_templates FINAL WHERE exporter = '2001:db8::62'").Scan(&ids, &hasIPv6, &hasIf32); err != nil {
		t.Fatal(err)
	}
	if ids != "[27,28,10,12]" || hasIPv6 != 1 || hasIf32 != 1 {
		t.Fatalf("template row: ids=%s has_ipv6=%d has_ifindex32=%d", ids, hasIPv6, hasIf32)
	}

	// CR-18 / CR-12 columns: an NSEL session's reverse half with NAT, the
	// firewall event and an NBAR2 application id.
	nsel := netflow.Record{
		Timestamp: now, ReceivedAt: now, CollectorID: "it",
		ExporterIP: net.IPv4(192, 0, 2, 70).To4(), ExporterAddr: netip.MustParseAddr("192.0.2.70"),
		FlowVersion: 9, SamplingInterval: 1, IPVersion: 4,
		SrcAddr: net.IPv4(142, 250, 74, 46).To4(), DstAddr: net.IPv4(10, 20, 1, 10).To4(), SrcPort: 443, DstPort: 51000,
		XlateSrc: net.IPv4(142, 250, 74, 46).To4(), XlateDst: net.IPv4(198, 51, 100, 7).To4(), XlateSrcPort: 443, XlateDstPort: 61000,
		FwEvent: netflow.FwEventDeleted, AppID: 13<<32 | 453, Bytes: 80000, Packets: 70, Protocol: 6,
		FieldsPresent: netflow.FieldTracked | netflow.FieldNAT | netflow.FieldFwEvent | netflow.FieldAppID | netflow.FieldBiflowReverse,
		FlowDirection: netflow.DirectionUnknown, ExportProtocol: netflow.ProtocolNetFlow9,
	}
	if err := store.InsertFlows(ctx, "it-token-nsel", []netflow.Record{nsel}); err != nil {
		t.Fatalf("insert NSEL record: %v", err)
	}
	var xsrc, xdst string
	var xsport, xdport uint16
	var fw uint8
	var app uint64
	var fp uint32
	if err := conn.QueryRow(ctx, `SELECT toString(xlate_src_addr6), toString(xlate_dst_addr6), xlate_src_port, xlate_dst_port, fw_event, app_id, fields_present
	    FROM flow_records WHERE exporter_ip = toIPv4('192.0.2.70')`).Scan(&xsrc, &xdst, &xsport, &xdport, &fw, &app, &fp); err != nil {
		t.Fatal(err)
	}
	if xsrc != "::ffff:142.250.74.46" || xdst != "::ffff:198.51.100.7" || xsport != 443 || xdport != 61000 || fw != 2 || app != 13<<32|453 || fp&netflow.FieldBiflowReverse == 0 {
		t.Fatalf("NSEL row: %s %s %d %d fw=%d app=%#x fields=%#x", xsrc, xdst, xsport, xdport, fw, app, fp)
	}

	// flow_app_names: one merged row per application.
	appRows := []appNameRow{{AppID: 13<<32 | 453, Name: "ssl", Description: "Secure Socket Layer", Category: "browsing", Exporter: "192.0.2.70", UpdatedAt: now.Truncate(time.Second)}}
	if err := store.InsertAppNames(ctx, appRows); err != nil {
		t.Fatalf("insert app names: %v", err)
	}
	var appName, appCat string
	if err := conn.QueryRow(ctx, "SELECT name, category FROM flow_app_names FINAL WHERE app_id = ?", uint64(13<<32|453)).Scan(&appName, &appCat); err != nil || appName != "ssl" || appCat != "browsing" {
		t.Fatalf("app name row: %q %q err=%v", appName, appCat, err)
	}

	// flow_if_counters (sFlow counter samples), IPv4 and IPv6 agents; a
	// retried token is deduplicated.
	counterRows := []ifCounterRow{
		{TS: now.Truncate(time.Second), Exporter: netip.MustParseAddr("192.0.2.90"), SampleIntervalS: 30,
			C: sflow.IfCounters{IfIndex: 70001, Speed: 10_000_000_000, Direction: 1, OperUp: true, InOctets: 1 << 40, OutOctets: 7, InUcast: 5, OutUcast: 6, InErrors: 1, OutErrors: 2, InDiscards: 3, OutDiscards: 4}},
		{TS: now.Truncate(time.Second), Exporter: netip.MustParseAddr("2001:db8::90"), C: sflow.IfCounters{IfIndex: 1}},
	}
	for i := 0; i < 2; i++ {
		if err := store.InsertIfCounters(ctx, "it-ifc-1", counterRows); err != nil {
			t.Fatalf("insert counters: %v", err)
		}
	}
	var nc uint64
	var e4, e6 string
	var ifIndex, interval uint32
	var inOctets, speed, outDiscards uint64
	var operUp, dir uint8
	if err := conn.QueryRow(ctx, `SELECT count() OVER (), toString(exporter_ip), toString(exporter_ip6), ifindex, if_speed, if_direction, oper_up, in_octets, out_discards, sample_interval_s
	    FROM flow_if_counters ORDER BY ifindex DESC LIMIT 1`).Scan(&nc, &e4, &e6, &ifIndex, &speed, &dir, &operUp, &inOctets, &outDiscards, &interval); err != nil {
		t.Fatal(err)
	}
	if nc != 2 || e4 != "192.0.2.90" || e6 != "::" || ifIndex != 70001 || speed != 10_000_000_000 || dir != 1 || operUp != 1 || inOctets != 1<<40 || outDiscards != 4 || interval != 30 {
		t.Fatalf("counter row: n=%d %s %s if=%d speed=%d dir=%d up=%d in=%d outDiscards=%d interval=%d", nc, e4, e6, ifIndex, speed, dir, operUp, inOctets, outDiscards, interval)
	}
}
