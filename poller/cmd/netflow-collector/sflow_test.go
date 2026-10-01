package main

import (
	"context"
	"encoding/binary"
	"net/netip"
	"os"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// sflowDatagram builds an sFlow v5 datagram from agent 192.0.2.90 (sub-agent
// 2) with one flow sample (1-in-rate, an Ethernet/IPv4/TCP header of a
// frameLen-byte frame, input ifIndex 5) and one generic interface counter
// sample for ifIndex 5.
func sflowDatagram(agent [4]byte, seq, uptimeMS, rate, frameLen uint32, inOctets uint64) []byte {
	u32 := func(b []byte, v uint32) []byte { return binary.BigEndian.AppendUint32(b, v) }
	opaque := func(b, data []byte) []byte {
		b = u32(b, uint32(len(data)))
		b = append(b, data...)
		for len(b)%4 != 0 {
			b = append(b, 0)
		}
		return b
	}
	hdr := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0x08, 0x00}
	ip := make([]byte, 20)
	ip[0], ip[9] = 0x45, 6
	copy(ip[12:], []byte{10, 20, 1, 10})
	copy(ip[16:], []byte{142, 250, 74, 46})
	tcp := make([]byte, 20)
	binary.BigEndian.PutUint16(tcp[0:], 51000)
	binary.BigEndian.PutUint16(tcp[2:], 443)
	tcp[13] = 0x10
	hdr = append(append(hdr, ip...), tcp...)

	headerRec := u32(u32(u32(nil, 1), frameLen), 4)
	headerRec = opaque(headerRec, hdr)
	flow := u32(u32(u32(u32(u32(nil, 1), 5), rate), rate*10), 0)
	flow = u32(u32(flow, 5), 7)
	flow = u32(flow, 1)
	flow = opaque(u32(flow, 1), headerRec)

	ifc := u32(u32(nil, 5), 6)
	ifc = binary.BigEndian.AppendUint64(ifc, 1_000_000_000)
	ifc = u32(u32(ifc, 1), 3)
	ifc = binary.BigEndian.AppendUint64(ifc, inOctets)
	for i := 0; i < 6; i++ {
		ifc = u32(ifc, uint32(i))
	}
	ifc = binary.BigEndian.AppendUint64(ifc, 2*inOctets)
	for i := 0; i < 6; i++ {
		ifc = u32(ifc, uint32(10+i))
	}
	counters := u32(u32(u32(nil, 1), 5), 1)
	counters = opaque(u32(counters, 1), ifc)

	d := u32(nil, 5)
	d = u32(d, 1)
	d = append(d, agent[:]...)
	d = u32(u32(u32(d, 2), seq), uptimeMS)
	d = u32(d, 2)
	d = opaque(u32(d, 1), flow)
	d = opaque(u32(d, 2), counters)
	return d
}

var testAgent = [4]byte{192, 0, 2, 90}

// CR-03 end to end through the collector: an sFlow sample is one packet
// scaled by the sampling rate like any sampled record; the stream is keyed
// by the agent address and sub-agent; datagram sequence gaps are loss.
func TestPipelineSFlow(t *testing.T) {
	c := newTestCollector(clockClamp)
	src := netip.MustParseAddr("198.51.100.200") // UDP source differs from the agent address
	now := time.Now().UTC()
	c.handleSFlowDatagram(sflowDatagram(testAgent, 10, 60_000, 1000, 1518, 1_000), src, now)
	c.handleSFlowDatagram(sflowDatagram(testAgent, 13, 90_000, 1000, 64, 5_000), src, now.Add(30*time.Second))

	recs := drain(t, c)
	if len(recs) != 2 {
		t.Fatalf("records: %d", len(recs))
	}
	r := recs[0]
	if r.Packets != 1000 || r.Bytes != 1518*1000 || r.SamplingInterval != 1000 {
		t.Fatalf("sampling scale-up: packets=%d bytes=%d rate=%d", r.Packets, r.Bytes, r.SamplingInterval)
	}
	if r.ExportProtocol != netflow.ProtocolSFlow5 || r.FlowVersion != 0 || r.ExporterAddr != netip.MustParseAddr("192.0.2.90") ||
		r.ObservationDomain != 2 || r.FieldsPresent&netflow.FieldSampledSFlow == 0 || r.TCPFlags != 0x10 || r.InputSNMP != 5 {
		t.Fatalf("record: %+v", r)
	}

	_, _, exporters := c.stats.snapshot()
	if len(exporters) != 1 {
		t.Fatalf("streams: %+v", exporters)
	}
	e := exporters[0]
	if e.Exporter != "192.0.2.90" || e.ObservationDomain != 2 || e.ExportProtocol != netflow.ProtocolSFlow5 {
		t.Fatalf("stream key: %+v", e)
	}
	// Sequence 10 then 13: datagrams 11 and 12 were lost.
	if e.Datagrams != 2 || e.RecordsDecoded != 2 || e.OptionsRecords != 2 || e.SequenceGaps != 2 || e.SequenceUnits != 2 || e.SamplingInterval != 1000 {
		t.Fatalf("stream counters: %+v", e.counters)
	}
	if e.ClockOffsetMS != nil {
		t.Fatalf("sFlow carries no wall clock: offset must stay unknown, got %d", *e.ClockOffsetMS)
	}

	// Two counter samples of ifIndex 5, 30 s of agent uptime apart.
	var rows []ifCounterRow
	c.ifCounters.flush(context.Background(), now, func(_ context.Context, _ string, r []ifCounterRow) error {
		rows = append(rows, r...)
		return nil
	})
	if len(rows) != 2 || rows[0].SampleIntervalS != 0 || rows[1].SampleIntervalS != 30 {
		t.Fatalf("counter rows / polling interval: %+v", rows)
	}
	if rows[1].C.IfIndex != 5 || rows[1].C.InOctets != 5_000 || rows[1].C.OutOctets != 10_000 || !rows[1].C.OperUp || rows[1].Exporter != netip.MustParseAddr("192.0.2.90") {
		t.Fatalf("counter row: %+v", rows[1])
	}
}

// The allowlist applies to the agent identity; malformed sFlow is a parse
// error of its stream.
func TestPipelineSFlowAllowlistAndParseErrors(t *testing.T) {
	c := newTestCollector(clockClamp, "192.0.2.91")
	src := netip.MustParseAddr("192.0.2.91")
	c.handleSFlowDatagram(sflowDatagram(testAgent, 1, 1000, 10, 100, 1), src, time.Now().UTC())
	allowed := sflowDatagram([4]byte{192, 0, 2, 91}, 1, 1000, 10, 100, 1)
	c.handleSFlowDatagram(allowed[:len(allowed)-12], src, time.Now().UTC())

	totals, _, exporters := c.stats.snapshot()
	if totals.RecordsDroppedUnknownExporter != 1 || totals.ParseErrors != 1 {
		t.Fatalf("totals: %+v", totals)
	}
	// The truncated datagram's intact flow sample is kept.
	if recs := drain(t, c); len(recs) != 1 || recs[0].ExporterAddr != netip.MustParseAddr("192.0.2.91") {
		t.Fatalf("records: %+v", recs)
	}
	if len(exporters) != 2 {
		t.Fatalf("streams (the dropped agent is accounted too): %+v", exporters)
	}
}

// An sFlow listener that could not start, or counters that cannot be
// written, degrade the collector without taking NetFlow down.
func TestHealthSFlowListener(t *testing.T) {
	c := newTestCollector(clockClamp)
	c.cfg.SFlowListen = ":6343"
	c.sflowErr = "listen udp :6343: bind: address already in use"
	doc := c.health(time.Now().UTC())
	if doc.Status != "degraded" || doc.SFlowListen != ":6343" || doc.SFlowListening || doc.SFlowError == "" {
		t.Fatalf("sFlow listener down: %+v", doc)
	}

	c.sflowErr = ""
	c.sflowListening.Store(true)
	if doc := c.health(time.Now().UTC()); doc.Status != "ok" || !doc.SFlowListening {
		t.Fatalf("sFlow listening: %s %v", doc.Status, doc.Reasons)
	}
	c.ifCounters.add(counterDatagram(1000, 1), time.Now())
	c.ifCounters.flush(context.Background(), time.Now(), func(context.Context, string, []ifCounterRow) error { return context.DeadlineExceeded })
	if doc := c.health(time.Now().UTC()); doc.Status != "degraded" || !doc.IfCounters.WriteFailing || doc.IfCounters.Queued != 1 {
		t.Fatalf("counter writes failing: %s %+v", doc.Status, doc.IfCounters)
	}

	c.cfg.SFlowListen = "" // disabled on purpose: nothing to report
	c.ifCounters = newIfCounterBuffer("t")
	if doc := c.health(time.Now().UTC()); doc.Status != "ok" || doc.SFlowListen != "" {
		t.Fatalf("sFlow disabled: %s %v", doc.Status, doc.Reasons)
	}
}

// NETFLOW_SFLOW_LISTEN: unset = the default port, empty or "off" = disabled.
func TestSFlowListenConfig(t *testing.T) {
	for _, c := range []struct {
		set   bool
		value string
		want  string
	}{{false, "", ":6343"}, {true, "", ""}, {true, "off", ""}, {true, " :16343 ", ":16343"}} {
		if c.set {
			t.Setenv("NETFLOW_SFLOW_LISTEN", c.value)
		} else {
			t.Setenv("NETFLOW_SFLOW_LISTEN", "x")
			os.Unsetenv("NETFLOW_SFLOW_LISTEN")
		}
		if got := loadConfig().SFlowListen; got != c.want {
			t.Errorf("NETFLOW_SFLOW_LISTEN set=%v %q: %q want %q", c.set, c.value, got, c.want)
		}
	}
}
