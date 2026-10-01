package main

import (
	"net/netip"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

func TestSeqTracker(t *testing.T) {
	type step struct {
		seq       uint32
		units     int
		lost, rec uint64
	}
	cases := []struct {
		name  string
		steps []step
	}{
		{"v9 datagrams, one lost", []step{{10, 1, 0, 0}, {11, 1, 0, 0}, {13, 1, 1, 0}}},
		{"IPFIX records", []step{{100, 30, 0, 0}, {130, 30, 0, 0}, {190, 30, 30, 0}}},
		{"v5 flows", []step{{0, 30, 0, 0}, {30, 30, 0, 0}, {90, 30, 30, 0}}},
		{"wraparound without loss", []step{{4294967290, 4, 0, 0}, {4294967294, 4, 0, 0}, {2, 4, 0, 0}}},
		{"wraparound with loss", []step{{4294967294, 1, 0, 0}, {3, 1, 4, 0}}},
		{"exporter restart is not loss", []step{{5_000_000, 1, 0, 0}, {0, 1, 0, 0}, {1, 1, 0, 0}}},
		// A datagram behind the expectation is first taken as late (its loss is
		// tentatively given back); when the next one continues from it, it was a
		// restart and the give-back is reversed: net zero.
		{"restart close to zero is not loss", []step{{0, 30, 0, 0}, {30, 30, 0, 0}, {0, 30, 0, 30}, {30, 30, 30, 0}, {60, 30, 0, 0}}},
		{"late datagram gives back its loss", []step{{10, 1, 0, 0}, {12, 1, 1, 0}, {11, 1, 0, 1}, {13, 1, 0, 0}}},
		{"restart after a gap restores given-back loss", []step{{10, 1, 0, 0}, {12, 1, 1, 0}, {2, 1, 0, 1}, {3, 1, 1, 0}, {4, 1, 0, 0}}},
		{"unknown count re-anchors", []step{{100, -1, 0, 0}, {5000, 10, 0, 0}, {5010, 10, 0, 0}}},
	}
	for _, c := range cases {
		var tr seqTracker
		for i, s := range c.steps {
			lost, rec := tr.observe(s.seq, s.units)
			if lost != s.lost || rec != s.rec {
				t.Errorf("%s step %d: lost=%d recovered=%d, want %d/%d", c.name, i, lost, rec, s.lost, s.rec)
			}
		}
	}
}

// Stats rows are per-interval deltas; the process row always exists; the
// cumulative totals keep growing; clock offset is the median.
func TestStatsRollInterval(t *testing.T) {
	t0 := time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC)
	s := newStatsRegistry(t0)
	exp := netip.MustParseAddr("2001:db8::62")
	key := statsKey{exporter: exp, domain: 42, protocol: netflow.ProtocolIPFIX}
	for i, off := range []time.Duration{900 * time.Millisecond, -3 * time.Second, 1100 * time.Millisecond} {
		s.observeDatagram(datagramObservation{
			key: key, bytes: 1400, hasSequence: true, sequence: uint32(i * 10), seqUnits: 10,
			exportTime: t0.Add(time.Duration(i) * time.Second), receivedAt: t0.Add(time.Duration(i)*time.Second + off),
			decoded: 10, samplingMax: 100,
		})
	}
	s.recordsWritten(testRecordsKey(25, exp, 42, netflow.ProtocolIPFIX), t0)
	s.processDelta(func(c *counters) { c.InsertFailures += 2 })

	start, rows := s.rollInterval(t0.Add(time.Minute), gauges{QueueLen: 5, QueueCap: 100}, map[string]map[netflow.DomainKey]int{
		netflow.ProtocolIPFIX: {{Exporter: exp, Domain: 42}: 3},
	})
	if !start.Equal(t0) || len(rows) != 2 {
		t.Fatalf("start=%v rows=%+v", start, rows)
	}
	proc, ex := rows[0], rows[1]
	if proc.Exporter != "" || proc.C.InsertFailures != 2 || proc.G.QueueLen != 5 {
		t.Fatalf("process row: %+v", proc)
	}
	if ex.Exporter != "2001:db8::62" || ex.C.Datagrams != 3 || ex.C.RecordsDecoded != 30 || ex.C.RecordsWritten != 25 ||
		ex.ClockOffsetMS != 900 || ex.SamplingInterval != 100 || ex.TemplatesActive != 3 || ex.C.SequenceGaps != 0 {
		t.Fatalf("exporter row: %+v", ex)
	}
	// Next interval: only deltas, and the offset carries forward.
	s.observeDatagram(datagramObservation{key: key, bytes: 100, receivedAt: t0.Add(61 * time.Second)})
	_, rows = s.rollInterval(t0.Add(2*time.Minute), gauges{}, nil)
	if rows[1].C.Datagrams != 1 || rows[1].C.RecordsWritten != 0 || rows[1].ClockOffsetMS != 900 {
		t.Fatalf("second interval: %+v", rows[1])
	}
	totals, _, exporters := s.snapshot()
	if totals.Datagrams != 4 || totals.RecordsWritten != 25 || totals.InsertFailures != 2 || len(exporters) != 1 {
		t.Fatalf("totals=%+v exporters=%d", totals, len(exporters))
	}
}

// sequence_units is the loss denominator in the exporter's own units: an
// IPFIX datagram of 10 bidirectional records numbers 10 records but decodes
// into 20 rows, and a datagram whose record count is unknown adds nothing.
func TestStatsSequenceUnitsAreExporterUnits(t *testing.T) {
	t0 := time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC)
	s := newStatsRegistry(t0)
	exp := netip.MustParseAddr("192.0.2.70")
	key := statsKey{exporter: exp, domain: 5, protocol: netflow.ProtocolIPFIX}
	for _, o := range []struct {
		seq   uint32
		units int
	}{{0, 10}, {10, 10}, {30, 10}, {40, -1}} {
		s.observeDatagram(datagramObservation{key: key, hasSequence: true, sequence: o.seq, seqUnits: o.units,
			decoded: 20, receivedAt: t0})
	}
	_, rows := s.rollInterval(t0.Add(time.Minute), gauges{}, nil)
	ex := rows[1]
	if ex.C.SequenceUnits != 30 || ex.C.SequenceGaps != 10 || ex.C.RecordsDecoded != 80 {
		t.Fatalf("units=%d gaps=%d decoded=%d, want 30/10/80", ex.C.SequenceUnits, ex.C.SequenceGaps, ex.C.RecordsDecoded)
	}
}

func testRecordsKey(n int, exp netip.Addr, domain uint32, proto string) []netflow.Record {
	out := make([]netflow.Record, n)
	for i := range out {
		out[i] = netflow.Record{ExporterAddr: exp, ObservationDomain: domain, ExportProtocol: proto}
	}
	return out
}

// At the registry level a counter restart near zero adds no loss, and a
// genuinely late datagram removes the loss its gap had added.
func TestStatsSequenceRestartAndReorder(t *testing.T) {
	s := newStatsRegistry(time.Now())
	exp := netip.MustParseAddr("192.0.2.9")
	key := statsKey{exporter: exp, domain: 0, protocol: netflow.ProtocolNetFlow5}
	send := func(seq uint32) {
		s.observeDatagram(datagramObservation{key: key, hasSequence: true, sequence: seq, seqUnits: 30, receivedAt: time.Now()})
	}
	for _, seq := range []uint32{0, 30, 60, 0, 30, 60} { // restart after 90 flows
		send(seq)
	}
	if totals, _, _ := s.snapshot(); totals.SequenceGaps != 0 {
		t.Fatalf("restart counted as %d lost flows", totals.SequenceGaps)
	}
	for _, seq := range []uint32{90, 150, 120, 180} { // 120 arrives late
		send(seq)
	}
	if totals, _, _ := s.snapshot(); totals.SequenceGaps != 0 {
		t.Fatalf("reordered datagram left %d flows counted as lost", totals.SequenceGaps)
	}
	send(240) // 210 really lost
	if totals, _, _ := s.snapshot(); totals.SequenceGaps != 30 {
		t.Fatalf("loss = %d, want 30", totals.SequenceGaps)
	}
}

// A stream idle for a day is forgotten (bounded memory), but the cumulative
// totals in /health keep what it counted.
func TestStatsIdleStreamRetiredKeepsTotals(t *testing.T) {
	t0 := time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC)
	s := newStatsRegistry(t0)
	exp := netip.MustParseAddr("192.0.2.44")
	s.observeDatagram(datagramObservation{key: statsKey{exporter: exp, protocol: netflow.ProtocolNetFlow5}, bytes: 72, decoded: 1, receivedAt: t0})
	s.recordsWritten(testRecordsKey(1, exp, 0, netflow.ProtocolNetFlow5), t0)
	s.rollInterval(t0.Add(time.Minute), gauges{}, nil)
	_, rows := s.rollInterval(t0.Add(25*time.Hour), gauges{}, nil)
	if len(rows) != 1 || rows[0].Exporter != "" {
		t.Fatalf("an idle stream must not produce rows: %+v", rows)
	}
	totals, _, exporters := s.snapshot()
	if len(exporters) != 0 {
		t.Fatalf("idle stream still tracked: %+v", exporters)
	}
	if totals.Datagrams != 1 || totals.RecordsDecoded != 1 || totals.RecordsWritten != 1 {
		t.Fatalf("cumulative totals went backwards: %+v", totals)
	}
}
