package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

func newTestCollector(policy clockPolicy, allow ...string) *collector {
	cfg := collectorConfig{ID: "test", BatchSize: 1000, FlushInterval: time.Hour, QueueMaxRecords: 100000, ClockPolicy: policy, Listen: ":0"}
	cfg.AllowedExporters = parseExporterAllowlist(joinComma(allow))
	c := &collector{
		cfg:            cfg,
		stats:          newStatsRegistry(time.Now()),
		templates:      newTemplateRegistry(),
		appNames:       newAppNameRegistry(),
		ifCounters:     newIfCounterBuffer("test"),
		delivery:       &deliveryState{},
		v9Templates:    netflow.NewV9TemplateCache(),
		ipfixTemplates: netflow.NewV9TemplateCache(),
		startedAt:      time.Now().UTC(),
	}
	c.queue = newBatchQueue(cfg.BatchSize, cfg.QueueMaxRecords, cfg.FlushInterval, "test", nil)
	c.listening.Store(true)
	return c
}

func joinComma(v []string) string {
	out := ""
	for i, s := range v {
		if i > 0 {
			out += ","
		}
		out += s
	}
	return out
}

func drain(t *testing.T, c *collector) []netflow.Record {
	t.Helper()
	c.queue.Close()
	var out []netflow.Record
	for b := c.queue.Next(context.Background()); b != nil; b = c.queue.Next(context.Background()) {
		out = append(out, b.Records...)
	}
	return out
}

// v5Datagram builds a one-record NetFlow v5 datagram.
func v5Datagram(samplingWord uint16, bytes uint32, exportSecs uint32) []byte {
	buf := make([]byte, 24+48)
	binary.BigEndian.PutUint16(buf[0:2], 5)
	binary.BigEndian.PutUint16(buf[2:4], 1)
	binary.BigEndian.PutUint32(buf[4:8], 100_000)
	binary.BigEndian.PutUint32(buf[8:12], exportSecs)
	binary.BigEndian.PutUint32(buf[16:20], 7)
	binary.BigEndian.PutUint16(buf[22:24], samplingWord)
	r := buf[24:]
	copy(r[0:4], []byte{10, 0, 0, 1})
	copy(r[4:8], []byte{10, 0, 0, 2})
	binary.BigEndian.PutUint32(r[16:20], 1)
	binary.BigEndian.PutUint32(r[20:24], bytes)
	binary.BigEndian.PutUint32(r[24:28], 98_000)
	binary.BigEndian.PutUint32(r[28:32], 99_000)
	r[38] = 6
	return buf
}

// v9Datagram carries a template (src, dst, bytes, packets, flowEndMs) and one
// data record in a single datagram.
func v9Datagram(sourceID, seq uint32, exportSecs uint32, endMS uint64) []byte {
	fields := [][2]uint16{{8, 4}, {12, 4}, {1, 4}, {2, 4}, {153, 8}}
	var tpl []byte
	tpl = binary.BigEndian.AppendUint16(tpl, 256)
	tpl = binary.BigEndian.AppendUint16(tpl, uint16(len(fields)))
	for _, f := range fields {
		tpl = binary.BigEndian.AppendUint16(tpl, f[0])
		tpl = binary.BigEndian.AppendUint16(tpl, f[1])
	}
	rec := []byte{10, 1, 1, 1, 10, 2, 2, 2}
	rec = binary.BigEndian.AppendUint32(rec, 5000)
	rec = binary.BigEndian.AppendUint32(rec, 5)
	rec = binary.BigEndian.AppendUint64(rec, endMS)
	for len(rec)%4 != 0 {
		rec = append(rec, 0)
	}
	pkt := make([]byte, 20)
	binary.BigEndian.PutUint16(pkt[0:2], 9)
	binary.BigEndian.PutUint16(pkt[2:4], 2)
	binary.BigEndian.PutUint32(pkt[4:8], 100_000)
	binary.BigEndian.PutUint32(pkt[8:12], exportSecs)
	binary.BigEndian.PutUint32(pkt[12:16], seq)
	binary.BigEndian.PutUint32(pkt[16:20], sourceID)
	pkt = binary.BigEndian.AppendUint16(pkt, 0)
	pkt = binary.BigEndian.AppendUint16(pkt, uint16(4+len(tpl)))
	pkt = append(pkt, tpl...)
	pkt = binary.BigEndian.AppendUint16(pkt, 256)
	pkt = binary.BigEndian.AppendUint16(pkt, uint16(4+len(rec)))
	return append(pkt, rec...)
}

// X-02 end to end: header 0x4064 is 1-in-100, and the stored estimate is
// bytes x 100 (not x 16484).
func TestPipelineV5SamplingScaled(t *testing.T) {
	c := newTestCollector(clockClamp)
	now := time.Now().UTC()
	c.handleDatagram(v5Datagram(0x4064, 1500, uint32(now.Unix())), netip.MustParseAddr("192.0.2.5"), now)
	recs := drain(t, c)
	if len(recs) != 1 || recs[0].SamplingInterval != 100 || recs[0].Bytes != 150_000 || recs[0].Packets != 100 {
		t.Fatalf("sampled v5 record: %+v", recs)
	}
	_, _, ex := c.stats.snapshot()
	if len(ex) != 1 || ex[0].SamplingInterval != 100 || ex[0].ObservationDomain != 0 || ex[0].ExportProtocol != netflow.ProtocolNetFlow5 {
		t.Fatalf("stats: %+v", ex)
	}
}

func TestPipelineIPv6ExporterIdentity(t *testing.T) {
	c := newTestCollector(clockClamp)
	now := time.Now().UTC()
	c.handleDatagram(v9Datagram(3, 1, uint32(now.Unix()), uint64(now.UnixMilli())), netip.MustParseAddr("2001:db8::9"), now)
	recs := drain(t, c)
	if len(recs) != 1 || recs[0].ExporterAddr != netip.MustParseAddr("2001:db8::9") || recs[0].ExporterIP != nil || recs[0].ObservationDomain != 3 {
		t.Fatalf("record: %+v", recs)
	}
	_, _, ex := c.stats.snapshot()
	if len(ex) != 1 || ex[0].Exporter != "2001:db8::9" || ex[0].RecordsDecoded != 1 {
		t.Fatalf("stats: %+v", ex)
	}
	if got := exporterIP6Col(&recs[0]); netip.AddrFrom16(got) != netip.MustParseAddr("2001:db8::9") || ipv4Col(recs[0].ExporterIP) != 0 {
		t.Fatal("storage columns for an IPv6 exporter must be exporter_ip=0.0.0.0, exporter_ip6=address")
	}
}

// Clock policy: a flow stamped years in the past by a broken exporter clock
// is placed at receipt time (clamp) and counted, or kept as sent (trust).
func TestPipelineClockPolicy(t *testing.T) {
	now := time.Now().UTC()
	old := uint64(time.Date(2001, 1, 1, 0, 0, 0, 0, time.UTC).UnixMilli())
	for _, policy := range []clockPolicy{clockClamp, clockTrust} {
		c := newTestCollector(policy)
		c.handleDatagram(v9Datagram(1, 1, uint32(now.Unix()), old), netip.MustParseAddr("192.0.2.7"), now)
		recs := drain(t, c)
		totals, _, _ := c.stats.snapshot()
		clamped := recs[0].FieldsPresent&netflow.FieldClockClamped != 0
		if policy == clockClamp && (!clamped || !recs[0].Timestamp.Equal(now) || totals.ClockClampedRecords != 1 || recs[0].LastSwitchedMS != old) {
			t.Fatalf("clamp: %+v clamped=%d", recs[0], totals.ClockClampedRecords)
		}
		if policy == clockTrust && (clamped || uint64(recs[0].Timestamp.UnixMilli()) != old || totals.ClockClampedRecords != 0) {
			t.Fatalf("trust: %+v", recs[0])
		}
	}
}

// Sequence gaps and clock offset are measured per exporter domain.
func TestPipelineSequenceGapsAndClockOffset(t *testing.T) {
	c := newTestCollector(clockClamp)
	now := time.Now().UTC().Truncate(time.Second)
	exp := netip.MustParseAddr("192.0.2.8")
	for _, seq := range []uint32{1, 2, 5, 6} { // 3 and 4 lost
		c.handleDatagram(v9Datagram(1, seq, uint32(now.Unix())-120, uint64(now.UnixMilli())), exp, now) // exporter clock 2 min slow
	}
	_, _, ex := c.stats.snapshot()
	if len(ex) != 1 || ex[0].SequenceGaps != 2 || ex[0].ClockOffsetMS == nil || *ex[0].ClockOffsetMS != 120_000 {
		t.Fatalf("stats: %+v", ex)
	}
}

func TestPipelineAllowlistAndParseErrors(t *testing.T) {
	c := newTestCollector(clockClamp, "192.0.2.10")
	now := time.Now().UTC()
	c.handleDatagram(v5Datagram(0, 100, uint32(now.Unix())), netip.MustParseAddr("192.0.2.66"), now)
	c.handleDatagram([]byte{0, 7, 1, 2}, netip.MustParseAddr("192.0.2.10"), now)
	c.handleDatagram(v5Datagram(0, 100, uint32(now.Unix())), netip.MustParseAddr("::ffff:192.0.2.10"), now)
	recs := drain(t, c)
	totals, _, _ := c.stats.snapshot()
	if len(recs) != 1 || totals.RecordsDroppedUnknownExporter != 1 || totals.ParseErrors != 1 || totals.Datagrams != 3 {
		t.Fatalf("recs=%d totals=%+v", len(recs), totals)
	}
}

func TestHealthStatus(t *testing.T) {
	c := newTestCollector(clockClamp)
	get := func() (int, healthDoc) {
		rec := httptest.NewRecorder()
		c.healthHandler(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
		var doc healthDoc
		if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
			t.Fatal(err)
		}
		return rec.Code, doc
	}
	if code, doc := get(); code != 200 || doc.Status != "ok" || len(doc.Reasons) != 0 {
		t.Fatalf("fresh collector: %d %+v", code, doc)
	}
	now := time.Now().UTC()
	c.delivery.lastOK = now.Add(-10 * time.Second)
	c.delivery.lastErr = now
	c.delivery.lastErrLabel = "connection refused"
	c.delivery.failingSince = now.Add(-5 * time.Second)
	c.delivery.consecutiveFailures = 2
	if code, doc := get(); code != 200 || doc.Status != "degraded" || doc.Delivery.ConsecutiveFailures != 2 || doc.Delivery.LastInsertError != "connection refused" {
		t.Fatalf("retrying: %d %+v", code, doc)
	}
	c.delivery.failingSince = now.Add(-2 * time.Minute)
	if code, doc := get(); code != 503 || doc.Status != "down" || doc.Delivery.FailingSince == nil {
		t.Fatalf("failing for 2 minutes: %d %+v", code, doc)
	}
	c.delivery.consecutiveFailures = 0
	c.lastQueueDrop.Store(now.Add(-time.Minute).UnixNano())
	if _, doc := get(); doc.Status != "degraded" {
		t.Fatalf("recent queue drops: %+v", doc)
	}
	c.listening.Store(false)
	if code, doc := get(); code != 503 || doc.Status != "down" {
		t.Fatalf("not listening: %d %+v", code, doc)
	}
}

// Template lines are bounded per window; the window's overflow is counted
// and summarised by the next allow or periodic flush, then the budget resets.
func TestLogBudget(t *testing.T) {
	b := logBudget{window: 10 * time.Second, limit: 3}
	t0 := time.Unix(1_700_000_000, 0)
	allowed := 0
	for i := 0; i < 10; i++ {
		if b.allow(t0.Add(time.Duration(i) * time.Millisecond)) {
			allowed++
		}
	}
	if allowed != 3 || b.suppressed != 7 {
		t.Fatalf("allowed=%d suppressed=%d", allowed, b.suppressed)
	}
	b.flush(t0.Add(5 * time.Second)) // window not over: nothing changes
	if b.suppressed != 7 {
		t.Fatalf("flush inside the window reset the count: %d", b.suppressed)
	}
	b.flush(t0.Add(11 * time.Second))
	if b.suppressed != 0 || b.used != 0 || !b.allow(t0.Add(12*time.Second)) {
		t.Fatalf("window did not roll: used=%d suppressed=%d", b.used, b.suppressed)
	}
}

// A record dropped because its sampled counters overflow is not also counted
// as clock-clamped: clock_clamped_records counts stored rows only.
func TestIngestOverflowNotCountedAsClamped(t *testing.T) {
	c := newTestCollector(clockClamp)
	now := time.Now().UTC()
	exporter := netip.MustParseAddr("192.0.2.5")
	recs := []netflow.Record{{
		Timestamp: now.Add(24 * time.Hour), ReceivedAt: now, ExporterAddr: exporter, ExportProtocol: netflow.ProtocolNetFlow9,
		SamplingInterval: 1 << 31, Bytes: 900_000_000_000, Packets: 1, FieldsPresent: netflow.FieldTracked,
	}}
	obs := datagramObservation{key: statsKey{exporter: exporter, protocol: netflow.ProtocolNetFlow9}, receivedAt: now, decoded: 1}
	c.ingest(&obs, recs)
	totals, _, _ := c.stats.snapshot()
	if totals.RecordsDroppedImplausible != 1 || totals.ClockClampedRecords != 0 {
		t.Fatalf("implausible=%d clamped=%d", totals.RecordsDroppedImplausible, totals.ClockClampedRecords)
	}
	if n, _ := c.queue.Len(); n != 0 {
		t.Fatalf("dropped record queued: %d", n)
	}
}

// X-08: a record whose sampling rate is ambiguous is stored unscaled and
// counted per exporter (flow_collector_stats.sampling_ambiguous_records), so
// health can say why its volume is not an estimate.
func TestIngestCountsAmbiguousSampling(t *testing.T) {
	c := newTestCollector(clockTrust)
	now := time.Now().UTC()
	exporter := netip.MustParseAddr("192.0.2.95")
	recs := []netflow.Record{
		{Timestamp: now, ReceivedAt: now, ExporterAddr: exporter, ExportProtocol: netflow.ProtocolNetFlow9,
			SamplingInterval: 1, Bytes: 1000, Packets: 10, FieldsPresent: netflow.FieldTracked | netflow.FieldSamplingAmbiguous},
		{Timestamp: now, ReceivedAt: now, ExporterAddr: exporter, ExportProtocol: netflow.ProtocolNetFlow9,
			SamplingInterval: 100, Bytes: 1000, Packets: 10, FieldsPresent: netflow.FieldTracked},
	}
	obs := datagramObservation{key: statsKey{exporter: exporter, domain: 952, protocol: netflow.ProtocolNetFlow9}, receivedAt: now, decoded: 2}
	c.ingest(&obs, recs)
	totals, _, ex := c.stats.snapshot()
	if totals.SamplingAmbiguousRecords != 1 || len(ex) != 1 || ex[0].SamplingAmbiguousRecords != 1 {
		t.Fatalf("ambiguous: totals=%d rows=%+v", totals.SamplingAmbiguousRecords, ex)
	}
	_, rows := c.stats.rollInterval(now.Add(time.Minute), gauges{}, nil)
	var written uint64
	for _, r := range rows {
		if r.Exporter == exporter.String() {
			written += r.C.SamplingAmbiguousRecords
		}
	}
	if written != 1 {
		t.Fatalf("flow_collector_stats rows carry %d ambiguous records, want 1", written)
	}
	out := drain(t, c)
	if len(out) != 2 || out[0].Bytes != 1000 || out[1].Bytes != 100_000 {
		t.Fatalf("stored: %+v", out)
	}
}

// A collector that was quiet for a long time (last success an hour ago, or
// none since a start 14 minutes ago) and then hits a short ClickHouse blip is
// retrying, not down: the outage is as old as the failure streak, which the
// writer starts at the first failure and clears on success.
func TestHealthOutageMeasuredFromFailureStreak(t *testing.T) {
	c := newTestCollector(clockClamp)
	c.startedAt = time.Now().UTC().Add(-14 * time.Minute)
	clock := time.Now().UTC()
	w := &flowWriter{stats: c.stats, state: c.delivery, now: func() time.Time { return clock }}
	status := func() (int, healthDoc) {
		rec := httptest.NewRecorder()
		c.healthHandler(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
		var doc healthDoc
		if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
			t.Fatal(err)
		}
		return rec.Code, doc
	}
	for _, lastOK := range []time.Time{{}, clock.Add(-time.Hour)} {
		c.delivery.lastOK = lastOK
		w.failed(errRefused)
		if code, doc := status(); code != 200 || doc.Status != "degraded" ||
			len(doc.Reasons) != 1 || doc.Reasons[0] != "retrying ClickHouse inserts (connection refused)" {
			t.Fatalf("blip after quiet period (last ok %v): %d %+v", lastOK, code, doc)
		}
		clock = clock.Add(2 * time.Second)
		w.failed(errRefused)
		if _, doc := status(); doc.Status != "degraded" {
			t.Fatalf("second failure of a 2 s streak: %+v", doc)
		}
		w.succeeded(nil)
		if st := c.delivery.snapshot(); !st.failingSince.IsZero() || st.consecutiveFailures != 0 {
			t.Fatalf("success must end the streak: %+v", st)
		}
	}
	// A streak that does last: down after downAfter, with the streak's age
	// (the health handler reads the real clock, so the streak starts in the
	// real past here).
	clock = time.Now().UTC().Add(-(downAfter + 5*time.Second))
	w.failed(errRefused)
	clock = clock.Add(downAfter)
	w.failed(errRefused)
	if code, doc := status(); code != 503 || doc.Status != "down" ||
		doc.Reasons[0] != "ClickHouse inserts failing for 1m5s (connection refused)" {
		t.Fatalf("lasting streak: %d %+v", code, doc)
	}
}

// NETFLOW_RCVBUF larger than the kernel grants (net.core.rmem_max) is a
// configuration issue /health names; a granted buffer is not.
func TestHealthFlagsCappedReceiveBuffer(t *testing.T) {
	c := newTestCollector(clockClamp)
	c.cfg.RcvBuf = 8 << 20
	c.rcvbuf = 425984 // Linux reports twice the 208 KiB it applied
	rec := httptest.NewRecorder()
	c.healthHandler(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	var doc healthDoc
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	want := "receive buffer smaller than requested: the kernel granted 208 KiB of the 8 MiB requested " +
		"(NETFLOW_RCVBUF); raise net.core.rmem_max to at least 8388608"
	if rec.Code != 200 || doc.Status != "degraded" || !doc.RcvbufCapped || doc.RcvbufRequestedBytes != 8<<20 ||
		len(doc.Reasons) != 1 || doc.Reasons[0] != want {
		t.Fatalf("capped: %d %+v", rec.Code, doc)
	}
	c.rcvbuf = 16 << 20 // granted in full
	if rcvbufCapped(c.cfg.RcvBuf, c.rcvbuf) || rcvbufCapped(8<<20, 0) || rcvbufCapped(0, 425984) {
		t.Fatal("a granted, unknown or unrequested buffer is not capped")
	}
	if doc := c.health(time.Now().UTC()); doc.Status != "ok" || doc.RcvbufCapped {
		t.Fatalf("granted: %+v", doc)
	}
}
