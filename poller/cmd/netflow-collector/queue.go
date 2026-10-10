package main

import (
	"context"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// flowBatch is the unit of delivery: a fixed set of records with a stable
// insert_deduplication_token, so every retry of it — in this process or after
// a restart via the disk spool — is recognised by ClickHouse if an earlier
// attempt had in fact committed.
type flowBatch struct {
	Token   string
	Seq     uint64
	Created time.Time
	Records []netflow.Record

	spoolName string // set when the batch was loaded from the disk spool
}

// batchQueue decouples UDP intake from ClickHouse delivery (NF-06, X-13).
// The reader appends records without ever blocking on a timer or on I/O;
// full batches are sealed into a FIFO held in memory up to capRecords. Beyond
// that, the oldest batches overflow to the disk spool when one is configured;
// without one (or when it is full) the oldest data is dropped and counted —
// fresh traffic is worth more to an operator than traffic minutes old.
type batchQueue struct {
	mu           sync.Mutex
	pending      []netflow.Record
	pendingSince time.Time
	sealed       []*flowBatch
	memRecords   int // records in pending + sealed (not the one in flight)
	capRecords   int
	batchSize    int
	flushEvery   time.Duration
	closed       bool
	seq          uint64
	tokenPrefix  string

	spool   *diskSpool
	spillCh chan struct{} // wakes the spiller
	ready   chan struct{} // wakes the writer (capacity 1)

	// onDrop attributes dropped records to their exporters (records_dropped_queue);
	// dropped counts records lost without attribution (evicted spool segments).
	onDrop    func([]netflow.Record)
	onDropped func(n int)
	now       func() time.Time
}

func newBatchQueue(batchSize, capRecords int, flushEvery time.Duration, tokenPrefix string, spool *diskSpool) *batchQueue {
	if batchSize <= 0 {
		batchSize = 1000
	}
	if capRecords < batchSize {
		capRecords = batchSize
	}
	return &batchQueue{
		capRecords:  capRecords,
		batchSize:   batchSize,
		flushEvery:  flushEvery,
		tokenPrefix: tokenPrefix,
		spool:       spool,
		spillCh:     make(chan struct{}, 1),
		ready:       make(chan struct{}, 1),
		onDrop:      func([]netflow.Record) {},
		onDropped:   func(int) {},
		now:         time.Now,
	}
}

// Add appends decoded records (UDP reader hot path: one lock, no I/O).
func (q *batchQueue) Add(recs []netflow.Record) {
	if len(recs) == 0 {
		return
	}
	var dropped [][]netflow.Record
	q.mu.Lock()
	if q.closed {
		q.mu.Unlock()
		q.onDrop(recs)
		return
	}
	if len(q.pending) == 0 {
		q.pendingSince = q.now()
	}
	q.pending = append(q.pending, recs...)
	q.memRecords += len(recs)
	for len(q.pending) >= q.batchSize {
		q.sealLocked(q.batchSize)
	}
	if q.memRecords > q.capRecords {
		if q.spool != nil {
			select {
			case q.spillCh <- struct{}{}:
			default:
			}
		}
		// The spiller normally keeps memory under the cap; the hard limit
		// bounds memory if the disk is slower than intake (or absent).
		hard := q.capRecords
		if q.spool != nil {
			hard += q.capRecords / 4
		}
		for q.memRecords > hard && len(q.sealed) > 0 {
			b := q.sealed[0]
			q.sealed = q.sealed[1:]
			q.memRecords -= len(b.Records)
			dropped = append(dropped, b.Records)
		}
	}
	notify := len(q.sealed) > 0
	q.mu.Unlock()
	for _, d := range dropped {
		q.onDrop(d)
	}
	if notify {
		q.wake()
	}
}

func (q *batchQueue) wake() {
	select {
	case q.ready <- struct{}{}:
	default:
	}
}

// sealLocked turns the first n pending records into a batch.
func (q *batchQueue) sealLocked(n int) {
	if n > len(q.pending) {
		n = len(q.pending)
	}
	if n == 0 {
		return
	}
	recs := make([]netflow.Record, n)
	copy(recs, q.pending[:n])
	rest := copy(q.pending, q.pending[n:])
	q.pending = q.pending[:rest]
	q.seq++
	q.sealed = append(q.sealed, &flowBatch{
		Token:   fmt.Sprintf("%s-%d", q.tokenPrefix, q.seq),
		Seq:     q.seq,
		Created: q.now(),
		Records: recs,
	})
	if len(q.pending) > 0 {
		q.pendingSince = q.now()
	}
}

// SealDue seals a partial batch that has waited flushEvery, so quiet
// exporters are still written promptly.
func (q *batchQueue) SealDue() {
	q.mu.Lock()
	due := len(q.pending) > 0 && q.now().Sub(q.pendingSince) >= q.flushEvery
	if due {
		q.sealLocked(len(q.pending))
	}
	q.mu.Unlock()
	if due {
		q.wake()
	}
}

// Close stops intake and seals what is pending; Next keeps returning batches
// until everything queued has been handed out.
func (q *batchQueue) Close() {
	q.mu.Lock()
	q.closed = true
	q.sealLocked(len(q.pending))
	q.mu.Unlock()
	q.wake()
}

// Next returns the oldest batch: spooled segments first (they are older than
// anything in memory), then memory. It blocks until a batch is available,
// ctx is done, or the queue is closed and empty (nil).
func (q *batchQueue) Next(ctx context.Context) *flowBatch {
	for {
		if q.spool != nil && !q.spool.empty() {
			b, corrupt, err := q.spool.peek()
			if corrupt > 0 {
				q.onDropped(corrupt)
			}
			if err == nil && b != nil {
				return b
			}
		}
		b, closed, retryDisk := q.takeMemory()
		if retryDisk {
			continue
		}
		if b != nil {
			return b
		}
		if closed && (q.spool == nil || q.spool.empty()) {
			return nil
		}
		select {
		case <-q.ready:
		case <-ctx.Done():
			return nil
		}
	}
}

// takeMemory makes the disk-to-memory handoff atomic with spill commits.
// A spiller can publish an older batch after Next observes an empty spool;
// check again while holding the same lock it uses to remove that batch from
// memory. Only the spool index is checked here, never disk reads or decoding.
func (q *batchQueue) takeMemory() (batch *flowBatch, closed bool, retryDisk bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.spool != nil && !q.spool.empty() {
		return nil, false, true
	}
	if len(q.sealed) > 0 {
		b := q.sealed[0]
		q.sealed = q.sealed[1:]
		q.memRecords -= len(b.Records)
		return b, false, false
	}
	return nil, q.closed, false
}

// Finished reports whether the queue is closed and nothing is left in memory
// or in the spool (the writer may stop).
func (q *batchQueue) Finished() bool {
	q.mu.Lock()
	done := q.closed && len(q.sealed) == 0 && len(q.pending) == 0
	q.mu.Unlock()
	return done && (q.spool == nil || q.spool.empty())
}

// Delivered releases a batch after a successful insert (deleting its spool
// segment when it came from disk).
func (q *batchQueue) Delivered(b *flowBatch) {
	if b.spoolName != "" && q.spool != nil {
		q.spool.drop(b.spoolName)
	}
}

// Persist is the shutdown path for a batch that could not be delivered in
// time: memory batches go to the disk spool when there is one; a spooled
// batch simply stays where it is. It returns false when the records are lost.
func (q *batchQueue) Persist(b *flowBatch) bool {
	if b.spoolName != "" {
		q.spool.release(b.spoolName)
		return true
	}
	if q.spool == nil {
		q.onDrop(b.Records)
		return false
	}
	evicted, err := q.spool.write(b)
	if evicted > 0 {
		q.onDropped(evicted)
	}
	if err != nil {
		if err != errSpoolFull {
			fmt.Fprintf(os.Stderr, "ERROR: netflow spool write failed: %v\n", err)
		}
		q.onDrop(b.Records)
		return false
	}
	return true
}

// Drain removes every batch still held in memory (shutdown, after delivery
// stopped) so the caller can persist them.
func (q *batchQueue) Drain() []*flowBatch {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.sealLocked(len(q.pending))
	out := q.sealed
	q.sealed = nil
	q.memRecords = 0
	return out
}

// runSpiller moves the oldest in-memory batches to disk while memory is over
// its cap. Encoding and the fsync happen without the queue lock; the batch is
// only removed from memory if it is still the oldest one when the file is
// committed (the writer may have taken it meanwhile).
func (q *batchQueue) runSpiller(ctx context.Context) {
	if q.spool == nil {
		return
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-q.spillCh:
		}
		for {
			q.mu.Lock()
			if q.memRecords <= q.capRecords*3/4 || len(q.sealed) == 0 {
				q.mu.Unlock()
				break
			}
			b := q.sealed[0]
			q.mu.Unlock()

			data, err := encodeSegment(b)
			if err != nil {
				fmt.Fprintf(os.Stderr, "ERROR: netflow spool encode failed: %v\n", err)
				break
			}
			tmp, err := q.spool.writeTemp(b, data)
			if err != nil {
				fmt.Fprintf(os.Stderr, "ERROR: netflow spool write failed: %v\n", err)
				break
			}
			q.mu.Lock()
			if len(q.sealed) == 0 || q.sealed[0] != b {
				q.mu.Unlock()
				os.Remove(tmp)
				continue
			}
			evicted, cerr := q.spool.commit(tmp, b)
			if cerr == nil || cerr == errSpoolFull {
				// Spilled, or the spool is full even after evicting its oldest
				// segments: either way the oldest memory batch leaves memory.
				q.sealed = q.sealed[1:]
				q.memRecords -= len(b.Records)
			}
			q.mu.Unlock()
			if evicted > 0 {
				q.onDropped(evicted)
			}
			if cerr == errSpoolFull {
				q.onDrop(b.Records)
			}
			if cerr != nil && cerr != errSpoolFull {
				fmt.Fprintf(os.Stderr, "ERROR: netflow spool commit failed: %v\n", cerr)
				break
			}
			q.wake()
		}
	}
}

// Len reports the records held in memory and the memory cap (queue_len/queue_cap).
func (q *batchQueue) Len() (int, int) {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.memRecords, q.capRecords
}
