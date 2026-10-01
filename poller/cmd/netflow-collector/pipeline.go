package main

import (
	"errors"
	"fmt"
	"math"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// maxPlausibleFlowBytes / maxPlausibleFlowPackets bound a single flow record.
// Anything above is treated as a decode/corruption artefact (BUG-01 defence in
// depth) — dropped before insert, counted, and never written to ClickHouse.
const (
	maxPlausibleFlowBytes   = uint64(1_000_000_000_000) // 1 TB in one flow record
	maxPlausibleFlowPackets = uint64(10_000_000_000)    // 10 B packets in one flow record
)

// datagramResult is what decoding one datagram produced.
type datagramResult struct {
	protocol    string
	domain      uint32
	exportTime  time.Time
	hasSequence bool
	sequence    uint32
	seqUnits    int // v5: flows in the datagram; v9: 1 datagram; IPFIX: data records (-1 unknown)
	records     []netflow.Record
	unsupported int
	waiting     int
	options     int
	templates   []netflow.TemplateInfo
	changed     int
	refused     int // new templates refused by the template cache bounds
	appNames    []netflow.AppName
	err         error
}

func protocolForVersion(data []byte) string {
	if len(data) < 2 {
		return ""
	}
	switch uint16(data[0])<<8 | uint16(data[1]) {
	case 5:
		return netflow.ProtocolNetFlow5
	case 9:
		return netflow.ProtocolNetFlow9
	case 10:
		return netflow.ProtocolIPFIX
	}
	return ""
}

func (c *collector) decode(data []byte, exporter netip.Addr, receivedAt time.Time) datagramResult {
	res := datagramResult{protocol: protocolForVersion(data)}
	switch res.protocol {
	case netflow.ProtocolNetFlow5:
		pkt, err := netflow.ParseV5(data, exporter, c.cfg.ID, receivedAt)
		if err != nil {
			res.err = err
			return res
		}
		res.domain = pkt.ObservationDomain()
		res.exportTime = pkt.ExportTime()
		res.hasSequence, res.sequence, res.seqUnits = true, pkt.FlowSequence, len(pkt.Records)
		res.records = pkt.Records
	case netflow.ProtocolNetFlow9, netflow.ProtocolIPFIX:
		var (
			recs []netflow.Record
			st   netflow.V9Stats
			err  error
		)
		if res.protocol == netflow.ProtocolNetFlow9 {
			recs, st, err = netflow.ParseV9(data, exporter, c.cfg.ID, receivedAt, c.v9Templates)
			res.hasSequence, res.seqUnits = len(data) >= netflow.V9HeaderLen, 1
		} else {
			recs, st, err = netflow.ParseIPFIX(data, exporter, c.cfg.ID, receivedAt, c.ipfixTemplates)
			res.hasSequence, res.seqUnits = len(data) >= netflow.IPFIXHeaderLen, st.DataRecords
			if err != nil {
				res.seqUnits = -1 // records after the malformed set were not counted
			}
		}
		// Records decoded before a malformed trailing set are kept: they come
		// from complete, well-framed sets.
		res.err = err
		res.domain, res.exportTime = st.Domain, st.ExportTime
		res.sequence = st.Sequence
		res.records = recs
		res.unsupported = st.RecordsUnsupported
		res.waiting = st.DataSetsWaiting
		res.options = st.OptionsRecords
		res.templates = st.Templates
		res.changed = st.TemplatesChanged
		res.refused = st.TemplatesRefused
		res.appNames = st.AppNames
	default:
		res.err = errUnsupportedVersion
	}
	return res
}

var errUnsupportedVersion = errors.New("unsupported flow export version")

// handleDatagram decodes one NetFlow v5/v9 or IPFIX datagram and queues its
// records. It runs on the NetFlow socket's reader goroutine (the only user of
// the v9/IPFIX template caches' write path) and never blocks on the database.
func (c *collector) handleDatagram(data []byte, from netip.Addr, receivedAt time.Time) {
	exporter := netflow.CanonicalAddr(from)
	if !c.allowed(exporter) {
		c.stats.observeDatagram(datagramObservation{
			key:          statsKey{exporter: exporter, protocol: protocolForVersion(data)},
			bytes:        len(data),
			receivedAt:   receivedAt,
			unknownDrops: true,
		})
		return
	}
	res := c.decode(data, exporter, receivedAt)
	obs := datagramObservation{
		key:         statsKey{exporter: exporter, domain: res.domain, protocol: res.protocol},
		bytes:       len(data),
		parseError:  res.err != nil,
		hasSequence: res.hasSequence,
		sequence:    res.sequence,
		seqUnits:    res.seqUnits,
		exportTime:  res.exportTime,
		receivedAt:  receivedAt,
		decoded:     len(res.records),
		unsupported: res.unsupported,
		waiting:     res.waiting,
		options:     res.options,
	}
	if res.err != nil {
		c.logf(&c.lastParseLog, "WARN: dropped malformed flow data from %s: %v", exporter, res.err)
	}
	if res.changed > 0 {
		for _, t := range res.templates {
			if !c.templateLogs.allow(receivedAt) {
				continue
			}
			kind := "template"
			if t.IsOptions {
				kind = "options template"
			}
			fmt.Printf("INFO: %s %s %d learned from %s (domain %d, %d fields)\n", t.Protocol, kind, t.ID, t.Exporter, t.Domain, len(t.Fields))
		}
	}
	if res.refused > 0 {
		c.logf(&c.lastRefusedLog, "WARN: %s template cache full for %s: %d new templates refused; their data is counted as waiting for a template", res.protocol, exporter, res.refused)
	}
	c.templates.observe(res.templates, receivedAt)
	c.appNames.observe(res.appNames, receivedAt)
	c.ingest(&obs, res.records)
}

// ingest applies the per-record rules every export protocol shares (the
// plausibility bound, the clock policy, sampling scale-up), records the
// datagram's statistics and queues the records. It never blocks on the database.
func (c *collector) ingest(obs *datagramObservation, records []netflow.Record) {
	kept := records[:0]
	for _, r := range records {
		if !sane(r) {
			obs.implausible++
			c.logf(&c.lastImplausibleLog, "WARN: dropping implausible flow from %s (bytes=%d packets=%d) — possible decode/corruption", r.ExporterAddr, r.Bytes, r.Packets)
			continue
		}
		// BUG-05: scale to estimated traffic on sampled exporters. The real
		// 1-in-N factor is kept in SamplingInterval so raw = bytes/N. A record
		// whose scaled counters overflow is dropped before anything else is
		// counted for it (clock_clamped_records counts stored rows only).
		if r.SamplingInterval > 1 {
			n := uint64(r.SamplingInterval)
			if r.Bytes > math.MaxUint64/n || r.Packets > math.MaxUint64/n {
				obs.implausible++
				continue
			}
			r.Bytes *= n
			r.Packets *= n
		}
		if c.cfg.ClockPolicy == clockClamp && netflow.ClampClock(&r) {
			obs.clamped++
		}
		if r.FieldsPresent&netflow.FieldSamplingAmbiguous != 0 {
			obs.ambiguous++
		}
		if r.SamplingInterval > obs.samplingMax {
			obs.samplingMax = r.SamplingInterval
		}
		kept = append(kept, r)
	}
	c.stats.observeDatagram(*obs)
	c.queue.Add(kept)
}

// sane reports whether a decoded flow record has plausible counters. Checked
// on the RAW decoded counters, before any sampling multiplication.
func sane(r netflow.Record) bool {
	return r.Bytes <= maxPlausibleFlowBytes && r.Packets <= maxPlausibleFlowPackets
}

// allowed reports whether flows from this exporter should be accepted. No
// allowlist accepts everything; a configured allowlist drops + counts
// packets from any other source (BUG-07).
func (c *collector) allowed(exporter netip.Addr) bool {
	if c.cfg.AllowedExporters == nil {
		return true
	}
	return c.cfg.AllowedExporters[exporter]
}

// logf rate-limits a repetitive warning to one line per 10 s per call site,
// so a misbehaving exporter cannot flood the journal (the counters carry the
// exact numbers).
func (c *collector) logf(last *atomic.Int64, format string, args ...any) {
	now := time.Now().UnixNano()
	prev := last.Load()
	if now-prev < int64(10*time.Second) || !last.CompareAndSwap(prev, now) {
		return
	}
	fmt.Printf(format+"\n", args...)
}

// logBudget allows a bounded number of log lines per window and summarises
// the rest: an exporter that re-defines its templates in every datagram (or
// forged datagrams doing so) must not flood the journal, while the normal
// "template learned" lines of a handful of exporters all get through. The
// summary of a window is printed by the next allow or flush after it ends
// (flush runs from the registry ticker, so it is never lost when traffic
// stops).
type logBudget struct {
	mu          sync.Mutex
	window      time.Duration
	limit       int
	windowStart time.Time
	used        int
	suppressed  int
}

func (b *logBudget) allow(now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.rollLocked(now)
	if b.used < b.limit {
		b.used++
		return true
	}
	b.suppressed++
	return false
}

func (b *logBudget) flush(now time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.rollLocked(now)
}

func (b *logBudget) rollLocked(now time.Time) {
	if now.Sub(b.windowStart) < b.window {
		return
	}
	if b.suppressed > 0 {
		fmt.Printf("INFO: %d more template lines suppressed (every template is in zenplus.flow_templates)\n", b.suppressed)
	}
	b.windowStart, b.used, b.suppressed = now, 0, 0
}

// runUDP reads datagrams until the socket is closed and hands each to handle
// (handleDatagram for NetFlow/IPFIX, handleSFlowDatagram for sFlow). Each
// socket has its own reader; the buffer is reused, so handlers copy whatever
// they keep.
func (c *collector) runUDP(conn *net.UDPConn, handle func(data []byte, from netip.Addr, receivedAt time.Time)) {
	buf := make([]byte, 64*1024)
	for {
		n, from, err := conn.ReadFromUDPAddrPort(buf)
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			c.logf(&c.lastReadLog, "WARN: udp read failed: %v", err)
			continue
		}
		handle(buf[:n], from.Addr(), time.Now().UTC())
	}
}
