package main

import (
	"context"
	"encoding/binary"
	"errors"
	"net/netip"
	"sort"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/netflow"
	"github.com/zenplus/poller/internal/sflow"
)

func collectAppRows(t *testing.T, a *appNameRegistry, now time.Time, fail bool) []appNameRow {
	t.Helper()
	var got []appNameRow
	a.flush(context.Background(), now, func(_ context.Context, rows []appNameRow) error {
		if fail {
			return errors.New("clickhouse down")
		}
		got = append(got, rows...)
		return nil
	})
	sort.Slice(got, func(i, j int) bool { return got[i].AppID < got[j].AppID })
	return got
}

// CR-12: name and category arrive in separate options records and are
// merged into one complete row; nothing is written without a name; unchanged
// definitions are rewritten daily, not on every announcement; a failed write
// is retried.
func TestAppNameRegistry(t *testing.T) {
	a := newAppNameRegistry()
	exp := netip.MustParseAddr("192.0.2.80")
	t0 := time.Date(2026, 9, 30, 10, 0, 0, 0, time.UTC)
	const yt, ssl, unnamed = 13<<32 | 1411, 13<<32 | 453, 13<<32 | 9

	a.observe([]netflow.AppName{
		{Exporter: exp, AppID: yt, Category: "consumer-streaming"}, // attributes before the table
		{Exporter: exp, AppID: ssl, Name: "ssl", Description: "Secure Socket Layer"},
		{Exporter: exp, AppID: unnamed, Category: "other"},
	}, t0)
	rows := collectAppRows(t, a, t0, false)
	if len(rows) != 1 || rows[0].AppID != ssl {
		t.Fatalf("only named definitions are written: %+v", rows)
	}

	a.observe([]netflow.AppName{{Exporter: exp, AppID: yt, Name: "youtube", Description: "YouTube"}}, t0.Add(time.Second))
	rows = collectAppRows(t, a, t0.Add(10*time.Second), false)
	if len(rows) != 1 || rows[0] != (appNameRow{AppID: yt, Name: "youtube", Description: "YouTube", Category: "consumer-streaming", Exporter: "192.0.2.80", UpdatedAt: t0.Add(10 * time.Second)}) {
		t.Fatalf("merged row: %+v", rows)
	}

	// Re-announced unchanged: not rewritten until a day has passed.
	again := []netflow.AppName{{Exporter: exp, AppID: ssl, Name: "ssl"}, {Exporter: exp, AppID: yt, Category: "consumer-streaming"}}
	a.observe(again, t0.Add(time.Hour))
	if rows := collectAppRows(t, a, t0.Add(time.Hour), false); len(rows) != 0 {
		t.Fatalf("unchanged definitions rewritten: %+v", rows)
	}
	a.observe(again, t0.Add(25*time.Hour))
	if rows := collectAppRows(t, a, t0.Add(25*time.Hour), false); len(rows) != 2 {
		t.Fatalf("daily refresh: %+v", rows)
	}

	// A change is written; a failed write stays pending and is retried.
	a.observe([]netflow.AppName{{Exporter: exp, AppID: ssl, Category: "browsing"}}, t0.Add(26*time.Hour))
	if rows := collectAppRows(t, a, t0.Add(26*time.Hour), true); len(rows) != 0 {
		t.Fatal("failed insert returned rows")
	}
	if known, pending := a.counts(); known != 2 || pending != 1 {
		t.Fatalf("counts after a failed write: known=%d pending=%d", known, pending)
	}
	rows = collectAppRows(t, a, t0.Add(26*time.Hour+10*time.Second), false)
	if len(rows) != 1 || rows[0].Category != "browsing" || rows[0].Name != "ssl" || rows[0].Description != "Secure Socket Layer" {
		t.Fatalf("retried row: %+v", rows)
	}
}

func counterDatagram(uptimeMS uint32, ifIndexes ...uint32) *sflow.Datagram {
	dg := &sflow.Datagram{Exporter: netip.MustParseAddr("192.0.2.90"), SubAgentID: 1, UptimeMS: uptimeMS}
	for _, i := range ifIndexes {
		dg.Counters = append(dg.Counters, sflow.IfCounters{IfIndex: i, InOctets: uint64(uptimeMS)})
	}
	return dg
}

// The polling interval is measured from agent uptime, per interface; an
// agent restart (uptime going back) makes it unknown again.
func TestIfCounterIntervals(t *testing.T) {
	b := newIfCounterBuffer("t")
	now := time.Now()
	b.add(counterDatagram(10_000, 1, 2), now)
	b.add(counterDatagram(40_000, 1), now)
	b.add(counterDatagram(69_600, 1, 2), now)
	b.add(counterDatagram(500, 1), now) // agent rebooted
	var rows []ifCounterRow
	b.flush(context.Background(), now, func(_ context.Context, _ string, r []ifCounterRow) error {
		rows = append(rows, r...)
		return nil
	})
	var got []uint32
	for _, r := range rows {
		got = append(got, r.SampleIntervalS)
	}
	if want := []uint32{0, 0, 30, 30, 60, 0}; len(got) != len(want) || got[2] != 30 || got[3] != 30 || got[4] != 60 || got[5] != 0 || got[0] != 0 || got[1] != 0 {
		t.Fatalf("intervals %v want %v", got, want)
	}
	if v := b.view(); v.Written != 6 || v.Queued != 0 {
		t.Fatalf("view: %+v", v)
	}
}

// Delivery: a failed batch keeps its token for the retry; beyond the cap the
// oldest rows are dropped and counted, never the batch in flight.
func TestIfCounterDelivery(t *testing.T) {
	b := newIfCounterBuffer("t")
	now := time.Now()
	for i := 0; i < ifCounterBatchRows+10; i++ {
		b.add(counterDatagram(1000, uint32(i)), now)
	}
	var tokens []string
	fail := true
	insert := func(_ context.Context, token string, rows []ifCounterRow) error {
		tokens = append(tokens, token)
		if fail {
			return errors.New("down")
		}
		return nil
	}
	b.flush(context.Background(), now, insert)
	if v := b.view(); !v.WriteFailing || v.Queued != ifCounterBatchRows+10 || len(tokens) != 1 {
		t.Fatalf("after a failed flush: %+v tokens=%v", v, tokens)
	}
	fail = false
	b.flush(context.Background(), now, insert)
	if len(tokens) != 3 || tokens[1] != tokens[0] || tokens[2] == tokens[1] {
		t.Fatalf("the failed batch must be retried under its token: %v", tokens)
	}
	if v := b.view(); v.WriteFailing || v.Queued != 0 || v.Written != ifCounterBatchRows+10 {
		t.Fatalf("after recovery: %+v", v)
	}

	// Over the cap while a batch is in flight: the in-flight batch survives.
	b.mu.Lock()
	b.batches = []ifCounterBatch{{token: "inflight", rows: make([]ifCounterRow, 10)}}
	b.queued, b.inflight = 10, "inflight"
	b.mu.Unlock()
	for i := 0; i < ifCounterCap+ifCounterBatchRows; i++ {
		b.add(counterDatagram(2000, uint32(i%1000)), now)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.queued > ifCounterCap || b.batches[0].token != "inflight" || b.dropped == 0 {
		t.Fatalf("cap: queued=%d dropped=%d first=%s", b.queued, b.dropped, b.batches[0].token)
	}
}

// NBAR2 options data and an NSEL session record travel through the NetFlow
// path: the application name reaches the registry, the session becomes two
// directional records in the queue.
func TestPipelineAppNamesAndBiflow(t *testing.T) {
	c := newTestCollector(clockClamp)
	u16 := func(b []byte, v uint16) []byte { return binary.BigEndian.AppendUint16(b, v) }
	u32 := func(b []byte, v uint32) []byte { return binary.BigEndian.AppendUint32(b, v) }
	set := func(id uint16, body []byte) []byte {
		for len(body)%4 != 0 {
			body = append(body, 0)
		}
		return append(u16(u16(nil, id), uint16(4+len(body))), body...)
	}
	// Options template 300: scope System(4); applicationId(4) + applicationName(16).
	otpl := u16(u16(u16(nil, 300), 4), 8)
	otpl = u16(u16(u16(u16(u16(u16(otpl, 1), 4), 95), 4), 96), 16)
	odata := u32(nil, 0)
	odata = append(odata, 13, 0, 1, 0xC5)
	odata = append(odata, []byte("ssl\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")...)
	// Data template 263: src, dst, proto, 231, 232, 233.
	fields := [][2]uint16{{8, 4}, {12, 4}, {4, 1}, {231, 4}, {232, 4}, {233, 1}}
	dtpl := u16(u16(nil, 263), uint16(len(fields)))
	for _, f := range fields {
		dtpl = u16(u16(dtpl, f[0]), f[1])
	}
	rec := []byte{10, 20, 1, 10, 142, 250, 74, 46, 6}
	rec = u32(u32(rec, 4000), 90000)
	rec = append(rec, netflow.FwEventDeleted)

	pkt := u16(u16(nil, 9), 4)
	pkt = u32(u32(u32(u32(pkt, 100_000), uint32(time.Now().Unix())), 1), 0)
	pkt = append(pkt, set(1, otpl)...)
	pkt = append(pkt, set(300, odata)...)
	pkt = append(pkt, set(0, dtpl)...)
	pkt = append(pkt, set(263, rec)...)
	c.handleDatagram(pkt, netip.MustParseAddr("192.0.2.70"), time.Now().UTC())

	if known, pending := c.appNames.counts(); known != 1 || pending != 1 {
		t.Fatalf("app names: known=%d pending=%d", known, pending)
	}
	recs := drain(t, c)
	if len(recs) != 2 || recs[0].Bytes != 4000 || recs[1].Bytes != 90000 || recs[1].FieldsPresent&netflow.FieldBiflowReverse == 0 ||
		recs[0].FwEvent != netflow.FwEventDeleted || recs[1].SrcAddr.String() != "142.250.74.46" {
		t.Fatalf("session records: %+v", recs)
	}
}
