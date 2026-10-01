package main

import (
	"context"
	"fmt"
	"net/netip"
	"sync"
	"time"

	"github.com/zenplus/poller/internal/sflow"
)

// ifCounterRow is one zenplus.flow_if_counters row: an sFlow generic
// interface counter sample as the agent sent it.
type ifCounterRow struct {
	TS       time.Time
	Exporter netip.Addr
	C        sflow.IfCounters
	// SampleIntervalS is the agent's counter polling interval, measured as
	// the agent-uptime difference to the previous sample of the same
	// interface (sFlow does not send it). 0 = not known yet: the first sample
	// after the collector or the agent started.
	SampleIntervalS uint32
}

type counterKey struct {
	exporter netip.Addr
	subAgent uint32
	ifIndex  uint32
}

type counterLast struct {
	uptimeMS uint32
	at       time.Time
}

type ifCounterBatch struct {
	token string
	rows  []ifCounterRow
}

// Interface counter buffering bounds.
const (
	// ifCounterCap: rows held while ClickHouse cannot take them. 1,000
	// interfaces polled every 30 s produce ~33 rows/s, so this covers ~25
	// minutes; beyond it the oldest batch is dropped and counted.
	ifCounterCap = 50_000
	// ifCounterBatchRows: rows per insert (and per deduplication token).
	ifCounterBatchRows = 5_000
	// maxCounterKeys bounds the polling-interval state (spoofed agents).
	maxCounterKeys = 100_000
	// counterKeyIdle: interval state of an interface silent for this long is
	// forgotten (its next sample reports an unknown interval again).
	counterKeyIdle = time.Hour
	// maxCounterInterval: a larger uptime step is not a polling interval
	// (the agent restarted, or samples were lost for a long time).
	maxCounterInterval = time.Hour
)

// ifCounterBuffer batches counter samples for flow_if_counters. Delivery is
// simpler than for flows (counters are cumulative, so a lost sample only
// widens the interval the next one describes): memory only, bounded, each
// batch retried with its own deduplication token until it is accepted.
type ifCounterBuffer struct {
	mu       sync.Mutex
	last     map[counterKey]counterLast
	pending  []ifCounterRow
	batches  []ifCounterBatch
	queued   int    // rows in pending + batches
	inflight string // token of the batch being inserted; never dropped
	seq      uint64
	prefix   string

	written, dropped uint64
	failingSince     time.Time
}

func newIfCounterBuffer(tokenPrefix string) *ifCounterBuffer {
	return &ifCounterBuffer{last: make(map[counterKey]counterLast), prefix: tokenPrefix}
}

// add queues the counter samples of one datagram (sFlow reader goroutine).
func (b *ifCounterBuffer) add(dg *sflow.Datagram, receivedAt time.Time) {
	if len(dg.Counters) == 0 {
		return
	}
	ts := receivedAt.UTC().Truncate(time.Second)
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, c := range dg.Counters {
		key := counterKey{dg.Exporter, dg.SubAgentID, c.IfIndex}
		row := ifCounterRow{TS: ts, Exporter: dg.Exporter, C: c}
		prev, known := b.last[key]
		if known {
			// uint32 arithmetic: correct across the agent's uptime wrap.
			if step := time.Duration(dg.UptimeMS-prev.uptimeMS) * time.Millisecond; step >= time.Second && step <= maxCounterInterval {
				row.SampleIntervalS = uint32((step + time.Second/2) / time.Second)
			}
		}
		if known || len(b.last) < maxCounterKeys {
			b.last[key] = counterLast{uptimeMS: dg.UptimeMS, at: receivedAt}
		}
		b.pending = append(b.pending, row)
		b.queued++
	}
	for b.queued > ifCounterCap {
		if !b.dropOldestLocked() {
			break
		}
	}
}

// dropOldestLocked discards the oldest queued rows (never the batch being
// inserted) and counts them.
func (b *ifCounterBuffer) dropOldestLocked() bool {
	for i, batch := range b.batches {
		if batch.token == b.inflight {
			continue
		}
		b.batches = append(b.batches[:i], b.batches[i+1:]...)
		b.queued -= len(batch.rows)
		b.dropped += uint64(len(batch.rows))
		return true
	}
	if len(b.pending) == 0 {
		return false
	}
	n := min(len(b.pending), ifCounterBatchRows)
	b.pending = b.pending[n:]
	b.queued -= n
	b.dropped += uint64(n)
	return true
}

// flush seals the pending rows into batches and inserts the batches oldest
// first, stopping at the first failure (kept for the next flush).
func (b *ifCounterBuffer) flush(ctx context.Context, now time.Time, insert func(context.Context, string, []ifCounterRow) error) {
	b.mu.Lock()
	for len(b.pending) > 0 {
		n := min(len(b.pending), ifCounterBatchRows)
		b.seq++
		b.batches = append(b.batches, ifCounterBatch{token: fmt.Sprintf("%s-ifc-%d", b.prefix, b.seq), rows: b.pending[:n:n]})
		b.pending = b.pending[n:]
	}
	b.pending = nil
	for k, l := range b.last {
		if now.Sub(l.at) > counterKeyIdle {
			delete(b.last, k)
		}
	}
	b.mu.Unlock()

	for {
		b.mu.Lock()
		if len(b.batches) == 0 {
			b.failingSince = time.Time{}
			b.mu.Unlock()
			return
		}
		batch := b.batches[0]
		b.inflight = batch.token
		b.mu.Unlock()

		err := insert(ctx, batch.token, batch.rows)

		b.mu.Lock()
		b.inflight = ""
		if err != nil {
			started := b.failingSince.IsZero()
			if started {
				b.failingSince = now
			}
			queued := b.queued
			b.mu.Unlock()
			if started { // one line per outage; /health shows it while it lasts
				fmt.Printf("WARN: netflow sFlow interface counters not written (%s); %d rows kept for retry\n", errorLabel(err), queued)
			}
			return
		}
		if len(b.batches) > 0 && b.batches[0].token == batch.token {
			b.batches = b.batches[1:]
			b.queued -= len(batch.rows)
		}
		b.written += uint64(len(batch.rows))
		b.mu.Unlock()
	}
}

// ifCounterView is the /health summary.
type ifCounterView struct {
	Written      uint64 `json:"rows_written"`
	Queued       int    `json:"rows_queued"`
	Dropped      uint64 `json:"rows_dropped"`
	WriteFailing bool   `json:"write_failing"`
}

func (b *ifCounterBuffer) view() ifCounterView {
	b.mu.Lock()
	defer b.mu.Unlock()
	return ifCounterView{Written: b.written, Queued: b.queued, Dropped: b.dropped, WriteFailing: !b.failingSince.IsZero()}
}
