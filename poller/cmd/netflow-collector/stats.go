package main

import (
	"net/netip"
	"sort"
	"sync"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// statsKey is the dimension of zenplus.flow_collector_stats: one stream per
// exporter, observation domain and export protocol. The zero key is the
// process-wide row (empty exporter).
type statsKey struct {
	exporter netip.Addr
	domain   uint32
	protocol string
}

// counters are the additive columns of flow_collector_stats. The same struct
// holds an interval's deltas and the cumulative totals shown by /health.
type counters struct {
	Datagrams                     uint64 `json:"datagrams"`
	DatagramBytes                 uint64 `json:"datagram_bytes"`
	RecordsDecoded                uint64 `json:"records_decoded"`
	RecordsWritten                uint64 `json:"records_written"`
	RecordsDroppedImplausible     uint64 `json:"records_dropped_implausible"`
	RecordsDroppedQueue           uint64 `json:"records_dropped_queue"`
	RecordsDroppedUnknownExporter uint64 `json:"records_dropped_unknown_exporter"`
	RecordsUnsupported            uint64 `json:"records_unsupported"`
	ParseErrors                   uint64 `json:"parse_errors"`
	DataSetsWaitingTemplate       uint64 `json:"data_sets_waiting_template"`
	OptionsRecords                uint64 `json:"options_records"`
	SequenceGaps                  uint64 `json:"sequence_gaps"`
	// SequenceUnits is the denominator of SequenceGaps: units received on a
	// sequenced stream (v5 flows, v9/sFlow datagrams, IPFIX data and options
	// records as the exporter numbered them, before a bidirectional record is
	// split into two rows).
	SequenceUnits       uint64 `json:"sequence_units"`
	ClockClampedRecords uint64 `json:"clock_clamped_records"`
	InsertFailures      uint64 `json:"insert_failures"`
	InsertRetries       uint64 `json:"insert_retries"`
	KernelRcvbufErrors  uint64 `json:"kernel_rcvbuf_errors"`
	// RecordsRejected are rows ClickHouse refused individually (isolated by
	// bisecting a failing batch). flow_collector_stats has no column of its
	// own for them; they are written under records_dropped_implausible, the
	// "record could not be stored as data" bucket, and shown separately here.
	RecordsRejected uint64 `json:"records_rejected"`
	// SamplingAmbiguousRecords are records stored unscaled because they named
	// no sampler while the exporter domain announced several with different
	// rates and no default (X-08; FieldSamplingAmbiguous).
	SamplingAmbiguousRecords uint64 `json:"sampling_ambiguous_records"`
}

func (c *counters) add(o counters) {
	c.Datagrams += o.Datagrams
	c.DatagramBytes += o.DatagramBytes
	c.RecordsDecoded += o.RecordsDecoded
	c.RecordsWritten += o.RecordsWritten
	c.RecordsDroppedImplausible += o.RecordsDroppedImplausible
	c.RecordsDroppedQueue += o.RecordsDroppedQueue
	c.RecordsDroppedUnknownExporter += o.RecordsDroppedUnknownExporter
	c.RecordsUnsupported += o.RecordsUnsupported
	c.ParseErrors += o.ParseErrors
	c.DataSetsWaitingTemplate += o.DataSetsWaitingTemplate
	c.OptionsRecords += o.OptionsRecords
	c.SequenceGaps += o.SequenceGaps
	c.SequenceUnits += o.SequenceUnits
	c.ClockClampedRecords += o.ClockClampedRecords
	c.InsertFailures += o.InsertFailures
	c.InsertRetries += o.InsertRetries
	c.KernelRcvbufErrors += o.KernelRcvbufErrors
	c.RecordsRejected += o.RecordsRejected
	c.SamplingAmbiguousRecords += o.SamplingAmbiguousRecords
}

func (c counters) zero() bool { return c == counters{} }

// maxOffsetSamples bounds the per-interval clock-offset samples kept per
// stream; the median of the first samples of a minute is representative.
const maxOffsetSamples = 128

// maxStreams bounds the per-exporter map: spoofed source addresses must not
// grow memory without limit. Streams beyond it are accounted on the process row.
const maxStreams = 4096

// streamIdle is how long a stream without traffic keeps its sequence and clock
// state before it is forgotten.
const streamIdle = 24 * time.Hour

type stream struct {
	cur, total counters

	samplingMax  uint32 // max sampling interval of records decoded this interval
	lastSampling uint32 // last non-zero interval seen (health)
	offsets      []int64
	offsetMS     int64 // median of the last interval with samples
	hasOffset    bool
	seq          seqTracker
	lastDatagram time.Time
	lastWritten  time.Time
}

// seqTracker turns export header sequence numbers into a loss estimate:
// NetFlow v5 counts flows, v9 counts datagrams per source ID, IPFIX counts
// data records per observation domain. uint32 arithmetic handles wraparound.
type seqTracker struct {
	valid    bool
	expected uint32
	// A datagram behind the expected sequence is either late (reordered) or
	// the first of an exporter that restarted its counter. The next datagram
	// tells them apart: if it continues from the "late" one, it was a restart.
	behind      bool
	behindNext  uint32
	behindGiven uint64 // loss given back for that datagram, restored on restart
}

const (
	// maxSequenceGap: a forward jump larger than this is an exporter restart
	// (sequence reset), not loss.
	maxSequenceGap = 1 << 24
	// maxLateWindow: how far behind the expected value a datagram may be and
	// still be treated as late rather than as a restart.
	maxLateWindow = 1 << 16
)

// observe records a datagram with sequence seq carrying units sequence units
// (units < 0: unknown, the next datagram re-anchors). It returns the loss to
// add and the loss to take back (a late datagram that was counted as lost).
func (s *seqTracker) observe(seq uint32, units int) (lost uint64, recovered uint64) {
	wasBehind, behindNext, given := s.behind, s.behindNext, s.behindGiven
	s.behind, s.behindGiven = false, 0
	if !s.valid || (wasBehind && seq == behindNext) {
		// First datagram, or the stream continues from a datagram that looked
		// late: the exporter restarted its counter. Re-anchor; the loss given
		// back for that datagram was not really recovered.
		if units >= 0 {
			s.valid, s.expected = true, seq+uint32(units)
		} else {
			s.valid = false
		}
		return given, 0
	}
	gap := seq - s.expected
	switch {
	case gap == 0:
	case gap < maxSequenceGap:
		lost = uint64(gap)
	case -gap <= maxLateWindow:
		// Behind the expected sequence: probably a reordered datagram whose
		// units were counted as lost when the gap opened, so give them back
		// and keep the expectation; the next datagram confirms or refutes it.
		if units > 0 {
			s.behind, s.behindNext = true, seq+uint32(units)
			recovered = uint64(units)
			s.behindGiven = recovered
		}
		return 0, recovered
	}
	if units < 0 {
		s.valid = false
		return lost, 0
	}
	s.expected = seq + uint32(units)
	return lost, 0
}

// statsRegistry holds every counter the collector reports.
type statsRegistry struct {
	mu        sync.Mutex
	streams   map[statsKey]*stream
	process   stream
	intervalS time.Time // start of the current interval
	// retired keeps the cumulative totals of streams forgotten after
	// streamIdle, so the /health totals never go backwards.
	retired counters
}

func newStatsRegistry(now time.Time) *statsRegistry {
	return &statsRegistry{streams: make(map[statsKey]*stream), intervalS: now}
}

// streamLocked returns the stream for key, or the process row when the map
// is at capacity (the counts stay exact in total, only the attribution is lost).
func (s *statsRegistry) streamLocked(key statsKey) *stream {
	if !key.exporter.IsValid() {
		return &s.process
	}
	st, ok := s.streams[key]
	if !ok {
		if len(s.streams) >= maxStreams {
			return &s.process
		}
		st = &stream{}
		s.streams[key] = st
	}
	return st
}

// datagramObservation is everything one decoded datagram contributes.
type datagramObservation struct {
	key          statsKey
	bytes        int
	parseError   bool
	hasSequence  bool
	sequence     uint32
	seqUnits     int
	exportTime   time.Time
	receivedAt   time.Time
	decoded      int
	unsupported  int
	implausible  int
	clamped      int
	ambiguous    int // records whose sampling rate was ambiguous (left unscaled)
	waiting      int
	options      int
	samplingMax  uint32
	unknownDrops bool // exporter not in NETFLOW_ALLOWED_EXPORTERS
}

func (s *statsRegistry) observeDatagram(o datagramObservation) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := s.streamLocked(o.key)
	st.lastDatagram = o.receivedAt
	st.cur.Datagrams++
	st.cur.DatagramBytes += uint64(o.bytes)
	if o.unknownDrops {
		st.cur.RecordsDroppedUnknownExporter++
		return
	}
	if o.parseError {
		st.cur.ParseErrors++
	}
	st.cur.RecordsDecoded += uint64(o.decoded)
	st.cur.RecordsUnsupported += uint64(o.unsupported)
	st.cur.RecordsDroppedImplausible += uint64(o.implausible)
	st.cur.ClockClampedRecords += uint64(o.clamped)
	st.cur.SamplingAmbiguousRecords += uint64(o.ambiguous)
	st.cur.DataSetsWaitingTemplate += uint64(o.waiting)
	st.cur.OptionsRecords += uint64(o.options)
	if o.samplingMax > st.samplingMax {
		st.samplingMax = o.samplingMax
	}
	if o.samplingMax > 0 {
		st.lastSampling = o.samplingMax
	}
	if o.hasSequence {
		if o.seqUnits > 0 {
			st.cur.SequenceUnits += uint64(o.seqUnits)
		}
		lost, recovered := st.seq.observe(o.sequence, o.seqUnits)
		st.cur.SequenceGaps += lost
		if recovered > 0 {
			// Take back loss counted for a datagram that arrived late; never
			// below what this interval counted (earlier rows are already written).
			if recovered > st.cur.SequenceGaps {
				recovered = st.cur.SequenceGaps
			}
			st.cur.SequenceGaps -= recovered
			st.seq.behindGiven = recovered
		}
	}
	if !o.exportTime.IsZero() && len(st.offsets) < maxOffsetSamples {
		st.offsets = append(st.offsets, o.receivedAt.Sub(o.exportTime).Milliseconds())
	}
}

// addPerRecord attributes per-record outcomes of the delivery pipeline.
func (s *statsRegistry) addPerRecord(recs []netflow.Record, apply func(*counters, uint64)) {
	if len(recs) == 0 {
		return
	}
	counts := make(map[statsKey]uint64, 4)
	for i := range recs {
		counts[recordKey(&recs[i])]++
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, n := range counts {
		apply(&s.streamLocked(k).cur, n)
	}
}

func recordKey(r *netflow.Record) statsKey {
	return statsKey{exporter: r.ExporterAddr, domain: r.ObservationDomain, protocol: r.ExportProtocol}
}

func (s *statsRegistry) recordsWritten(recs []netflow.Record, at time.Time) {
	s.addPerRecord(recs, func(c *counters, n uint64) { c.RecordsWritten += n })
	s.mu.Lock()
	for i := range recs {
		if st, ok := s.streams[recordKey(&recs[i])]; ok {
			st.lastWritten = at
		}
	}
	s.mu.Unlock()
}

func (s *statsRegistry) recordsDroppedQueue(recs []netflow.Record) {
	s.addPerRecord(recs, func(c *counters, n uint64) { c.RecordsDroppedQueue += n })
}

func (s *statsRegistry) recordsRejected(recs []netflow.Record) {
	s.addPerRecord(recs, func(c *counters, n uint64) { c.RecordsRejected += n })
}

// processDelta adds to the process-wide row (insert failures/retries, kernel
// drops, records lost without exporter attribution).
func (s *statsRegistry) processDelta(apply func(*counters)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	apply(&s.process.cur)
}

// gauges are the point-in-time columns of the process row.
type gauges struct {
	SpoolRecords, SpoolBytes, QueueLen, QueueCap uint64
}

// statsRow is one flow_collector_stats row.
type statsRow struct {
	TS                time.Time
	Exporter          string
	ObservationDomain uint32
	ExportProtocol    string
	C                 counters
	ClockOffsetMS     int64
	SamplingInterval  uint32
	TemplatesActive   uint32
	G                 gauges
}

// rollInterval closes the current interval: it returns one row per stream
// that had activity plus the process row, folds the deltas into the totals
// and forgets streams idle for streamIdle. templates maps exporter domains
// (per protocol) to their live template counts.
func (s *statsRegistry) rollInterval(now time.Time, g gauges, templates map[string]map[netflow.DomainKey]int) (time.Time, []statsRow) {
	s.mu.Lock()
	defer s.mu.Unlock()
	start := s.intervalS
	// ts is the interval start. Regular intervals start on a wall-clock
	// minute; the first one starts when the process did, so it keeps its own
	// second rather than colliding with the previous process's last interval
	// (readers bucket by toStartOfMinute(ts): counters are summed, the gauges
	// of the process row are point-in-time values to take the max of).
	ts := start.UTC().Truncate(time.Second)
	s.intervalS = now
	var rows []statsRow
	for k, st := range s.streams {
		if !st.cur.zero() || len(st.offsets) > 0 {
			if len(st.offsets) > 0 {
				st.offsetMS, st.hasOffset = median(st.offsets), true
			}
			rows = append(rows, statsRow{
				TS:                ts,
				Exporter:          k.exporter.String(),
				ObservationDomain: k.domain,
				ExportProtocol:    k.protocol,
				C:                 st.cur,
				ClockOffsetMS:     st.offsetMS,
				SamplingInterval:  st.samplingMax,
				TemplatesActive:   uint32(templates[k.protocol][netflow.DomainKey{Exporter: k.exporter, Domain: k.domain}]),
			})
		}
		st.total.add(st.cur)
		st.cur = counters{}
		st.offsets = st.offsets[:0]
		st.samplingMax = 0
		if now.Sub(st.lastDatagram) > streamIdle && now.Sub(st.lastWritten) > streamIdle {
			s.retired.add(st.total)
			delete(s.streams, k)
		}
	}
	rows = append(rows, statsRow{TS: ts, C: s.process.cur, G: g})
	s.process.total.add(s.process.cur)
	s.process.cur = counters{}
	sort.Slice(rows, func(i, j int) bool { return rows[i].Exporter < rows[j].Exporter })
	return start, rows
}

func median(v []int64) int64 {
	c := append([]int64(nil), v...)
	sort.Slice(c, func(i, j int) bool { return c[i] < c[j] })
	n := len(c)
	if n%2 == 1 {
		return c[n/2]
	}
	return (c[n/2-1] + c[n/2]) / 2
}

// exporterHealth is one stream in the /health document.
type exporterHealth struct {
	Exporter          string    `json:"exporter"`
	ObservationDomain uint32    `json:"observation_domain"`
	ExportProtocol    string    `json:"export_protocol"`
	LastDatagramAt    time.Time `json:"last_datagram_at"`
	ClockOffsetMS     *int64    `json:"clock_offset_ms"` // median receipt - export header time; null until measured
	SamplingInterval  uint32    `json:"sampling_interval"`
	TemplatesActive   int       `json:"templates_active"`
	counters
}

// snapshot returns cumulative totals (process + every stream, including
// streams since forgotten as idle) and per-stream detail for /health.
func (s *statsRegistry) snapshot() (totals counters, process counters, exporters []exporterHealth) {
	s.mu.Lock()
	defer s.mu.Unlock()
	process = s.process.total
	process.add(s.process.cur)
	totals = process
	totals.add(s.retired)
	for k, st := range s.streams {
		c := st.total
		c.add(st.cur)
		totals.add(c)
		e := exporterHealth{
			Exporter:          k.exporter.String(),
			ObservationDomain: k.domain,
			ExportProtocol:    k.protocol,
			LastDatagramAt:    st.lastDatagram,
			SamplingInterval:  st.lastSampling,
			counters:          c,
		}
		switch {
		case len(st.offsets) > 0:
			m := median(st.offsets)
			e.ClockOffsetMS = &m
		case st.hasOffset:
			m := st.offsetMS
			e.ClockOffsetMS = &m
		}
		exporters = append(exporters, e)
	}
	sort.Slice(exporters, func(i, j int) bool {
		if exporters[i].Exporter != exporters[j].Exporter {
			return exporters[i].Exporter < exporters[j].Exporter
		}
		if exporters[i].ObservationDomain != exporters[j].ObservationDomain {
			return exporters[i].ObservationDomain < exporters[j].ObservationDomain
		}
		return exporters[i].ExportProtocol < exporters[j].ExportProtocol
	})
	return totals, process, exporters
}
