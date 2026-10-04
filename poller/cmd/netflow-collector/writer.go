package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/zenplus/poller/internal/netflow"
)

// flowStore is what the writer needs from ClickHouse; tests use a fake.
type flowStore interface {
	Ping(ctx context.Context) error
	EnsureSchema(ctx context.Context) error
	InsertFlows(ctx context.Context, token string, recs []netflow.Record) error
}

// errClass says what to do about a failed insert.
type errClass int

const (
	// errTransient: network, timeout, server overload, unknown — retry the
	// same batch with backoff; nothing about the rows is wrong.
	errTransient errClass = iota
	// errSchema: table/column/database missing — re-run ensureSchema, retry.
	errSchema
	// errData: the server rejected the content (a type/parse/constraint error
	// raised while inserting or while pushing to a materialized view) — bisect
	// the batch to isolate the offending rows instead of retrying forever.
	errData
)

func (c errClass) String() string {
	switch c {
	case errSchema:
		return "schema"
	case errData:
		return "data"
	default:
		return "transient"
	}
}

// ClickHouse error codes that describe the rows rather than the server.
var dataErrorCodes = map[int32]bool{
	6:   true, // CANNOT_PARSE_TEXT
	27:  true, // CANNOT_PARSE_INPUT_ASSERTION_FAILED
	41:  true, // CANNOT_PARSE_DATETIME
	53:  true, // TYPE_MISMATCH
	69:  true, // ARGUMENT_OUT_OF_BOUND
	70:  true, // CANNOT_CONVERT_TYPE
	72:  true, // CANNOT_PARSE_NUMBER
	117: true, // INCORRECT_DATA
	321: true, // VALUE_IS_OUT_OF_RANGE_OF_DATA_TYPE
	395: true, // FUNCTION_THROW_IF_VALUE_IS_NON_ZERO (a view's throwIf)
	407: true, // DECIMAL_OVERFLOW
}

var schemaErrorCodes = map[int32]bool{
	16: true, // NO_SUCH_COLUMN_IN_TABLE
	47: true, // UNKNOWN_IDENTIFIER
	60: true, // UNKNOWN_TABLE
	81: true, // UNKNOWN_DATABASE
}

// appendError marks a client-side conversion failure for one row.
type appendError struct {
	index int
	err   error
}

func (e *appendError) Error() string { return fmt.Sprintf("row %d: %v", e.index, e.err) }
func (e *appendError) Unwrap() error { return e.err }

func classify(err error) errClass {
	var ae *appendError
	if errors.As(err, &ae) {
		return errData
	}
	var ex *clickhouse.Exception
	if errors.As(err, &ex) {
		switch {
		case schemaErrorCodes[ex.Code]:
			return errSchema
		case dataErrorCodes[ex.Code]:
			return errData
		}
	}
	return errTransient
}

// errorLabel is a short, non-sensitive description for /health and logs of
// the health consumer (never the server's message text).
func errorLabel(err error) string {
	var ex *clickhouse.Exception
	switch {
	case errors.As(err, &ex):
		return fmt.Sprintf("clickhouse code %d (%s)", ex.Code, classify(err))
	case errors.Is(err, context.DeadlineExceeded):
		return "insert timeout"
	case errors.Is(err, syscall.ECONNREFUSED):
		return "connection refused"
	case errors.Is(err, io.EOF), errors.Is(err, syscall.ECONNRESET), errors.Is(err, syscall.EPIPE):
		return "connection lost"
	}
	var ne net.Error
	if errors.As(err, &ne) {
		if ne.Timeout() {
			return "network timeout"
		}
		return "network error"
	}
	return classify(err).String() + " error"
}

// deliveryView is what /health reports about writing to ClickHouse.
type deliveryView struct {
	lastOK       time.Time
	lastErr      time.Time
	lastErrLabel string
	// failingSince is the first failure of the current streak (zero while
	// inserts succeed): an outage is as old as its streak, not as the last
	// success, which on a quiet collector can be an hour back.
	failingSince        time.Time
	consecutiveFailures int
	schemaReady         bool
	lastRejectAt        time.Time
	lastDropAt          time.Time
	heldRecords         int // single rows held after a data error (see flowWriter.held)
}

// deliveryState is the writer's deliveryView, shared with the health handler.
type deliveryState struct {
	mu sync.Mutex
	deliveryView
}

func (d *deliveryState) snapshot() deliveryView {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.deliveryView
}

// flowWriter delivers queued batches to ClickHouse: bounded per-insert
// timeout, exponential backoff with jitter, a stable deduplication token per
// batch, and bisection that isolates rows the server rejects so one poison row
// cannot block the pipeline or take its whole batch down with it.
type flowWriter struct {
	store         flowStore
	queue         *batchQueue
	stats         *statsRegistry
	state         *deliveryState
	insertTimeout time.Duration
	stopBy        *atomic.Int64 // shutdown deadline (unix ns) once a stop began; see opContext
	backoffMin    time.Duration
	backoffMax    time.Duration
	deadLetter    *deadLetter
	now           func() time.Time
	sleep         func(ctx context.Context, d time.Duration) bool

	// held are parts whose rows failed with a data error while nothing else
	// in their batch was accepted (a single-row batch, or a batch every row of
	// which failed on its own), so the failure cannot yet be told apart from
	// a fault that is not in the rows. They wait, without blocking newer
	// batches, until ClickHouse accepts other rows: then rows that still fail
	// on their own are proven poison and dead-lettered, while rows that were
	// victims of a passing, systemic fault are written. Held parts live in
	// memory only (the spool segment of their batch is released); if the
	// collector stops while they are held, they are spooled again under their
	// tokens. Bounded by maxHeldParts / maxHeldRecords: beyond that the fault
	// is judged systemic and the batch is retried in place (delivery stalls
	// visibly instead of dropping data). Only the writer goroutine touches it.
	held       []*part
	heldRetest time.Duration // heldRetestEvery; shorter in tests
}

// Bounds of the held parts: enough for several fully poisoned batches from a
// misbehaving exporter, small enough that retesting them all after each
// accepted batch stays cheap.
const (
	maxHeldParts   = 64
	maxHeldRecords = 20_000
)

// canHold reports whether parts fit within the held bounds.
func (w *flowWriter) canHold(parts []*part) bool {
	n := 0
	for _, p := range w.held {
		n += len(p.recs)
	}
	for _, p := range parts {
		n += len(p.recs)
	}
	return len(w.held)+len(parts) <= maxHeldParts && n <= maxHeldRecords
}

// hold moves the undecided parts of batch b to the held list.
func (w *flowWriter) hold(b *flowBatch, parts []*part) {
	for _, p := range parts {
		p.batchSeq, p.created = b.Seq, b.Created
		w.held = append(w.held, p)
	}
	w.setHeld()
}

// run delivers until the queue is closed and empty, or stop is cancelled
// (the shutdown deadline). Whatever could not be delivered by then is handed
// back to the queue to be spooled to disk (or counted as dropped).
func (w *flowWriter) run(stop context.Context) {
	for {
		b := w.next(stop)
		if b == nil {
			if len(w.held) > 0 && stop.Err() == nil && !w.queue.Finished() {
				// Idle with held rows: give them another try. Only a success
				// changes anything; nothing new was accepted to judge them by.
				w.retestHeld(stop, false)
				continue
			}
			break
		}
		remaining, accepted, ok := w.deliver(stop, b)
		if !ok {
			w.abandon(b, remaining)
			break
		}
		w.queue.Delivered(b)
		if accepted && len(w.held) > 0 {
			w.retestHeld(stop, true)
		}
	}
	if len(w.held) > 0 && stop.Err() == nil {
		w.retestHeld(stop, false)
	}
	for _, p := range w.held {
		w.abandon(&flowBatch{Token: p.token, Seq: p.batchSeq, Created: p.created, Records: p.recs}, nil)
	}
	w.held = nil
	w.setHeld()
	for _, b := range w.queue.Drain() {
		w.abandon(b, nil)
	}
}

// heldRetestEvery is how often held rows are re-tried while no new batch
// arrives (a quiet exporter), so a row held through a passing fault is
// written without waiting for more traffic.
const heldRetestEvery = 30 * time.Second

// next waits for the next batch; with rows held it gives up after
// heldRetestEvery even when nothing new arrived (returning nil).
func (w *flowWriter) next(stop context.Context) *flowBatch {
	if len(w.held) == 0 {
		return w.queue.Next(stop)
	}
	ctx, cancel := context.WithTimeout(stop, w.heldRetest)
	defer cancel()
	return w.queue.Next(ctx)
}

// retestHeld re-tries every held part once. evidence means another batch was
// just accepted, so ClickHouse is taking rows; a held part accepted in this
// round is the same evidence. With evidence, a part that still fails with a
// data error is judged on its own rows: a single row is dead-lettered, a
// larger part is bisected (isolate) so only its failing rows are. Any other
// outcome keeps the part held. Failures here count as insert failures but do
// not start a delivery-failure streak (new batches are being written meanwhile).
func (w *flowWriter) retestHeld(stop context.Context, evidence bool) {
	var kept, refused []*part
	for i, p := range w.held {
		w.stats.processDelta(func(c *counters) { c.InsertRetries++ })
		err := w.insert(stop, p.token, p.recs)
		switch {
		case err == nil:
			w.succeeded(p.recs)
			evidence = true
		case stop.Err() != nil:
			w.held = append(append(kept, refused...), w.held[i:]...)
			w.setHeld()
			return
		default:
			w.stats.processDelta(func(c *counters) { c.InsertFailures++ })
			if classify(err) == errData {
				refused = append(refused, p)
			} else {
				kept = append(kept, p)
			}
		}
	}
	if !evidence {
		w.held = append(kept, refused...)
		w.setHeld()
		return
	}
	var poison []*part
	for i, p := range refused {
		if len(p.recs) == 1 {
			poison = append(poison, p)
			continue
		}
		rest, bad := w.isolate(stop, p)
		kept = append(kept, rest...)
		poison = append(poison, bad...)
		if stop.Err() != nil {
			kept = append(kept, refused[i+1:]...)
			break
		}
	}
	if len(poison) > 0 {
		w.reject(poison, 0)
	}
	w.held = kept
	w.setHeld()
}

// isolate bisects a held part that still fails with a data error while
// ClickHouse accepts other rows. Each piece is retried once under its own
// token before it is split further (a piece whose rows committed while a view
// push failed is then a deduplicated no-op); single rows that still fail are
// returned as poison, accepted pieces are written, and pieces hit by any other
// error (or by the shutdown deadline) stay held.
func (w *flowWriter) isolate(stop context.Context, p *part) (keep, poison []*part) {
	mid := len(p.recs) / 2
	work := []*part{{token: p.token + "L", recs: p.recs[:mid]}, {token: p.token + "R", recs: p.recs[mid:]}}
	for len(work) > 0 {
		q := work[0]
		w.stats.processDelta(func(c *counters) { c.InsertRetries++ })
		err := w.insert(stop, q.token, q.recs)
		switch {
		case err == nil:
			w.succeeded(q.recs)
			work = work[1:]
			continue
		case stop.Err() != nil:
			return append(keep, work...), poison
		}
		w.stats.processDelta(func(c *counters) { c.InsertFailures++ })
		if classify(err) != errData {
			return append(keep, work...), poison
		}
		if q.dataFailures++; q.dataFailures < 2 {
			continue
		}
		work = work[1:]
		if len(q.recs) == 1 {
			poison = append(poison, q)
			continue
		}
		mid := len(q.recs) / 2
		work = append([]*part{{token: q.token + "L", recs: q.recs[:mid]}, {token: q.token + "R", recs: q.recs[mid:]}}, work...)
	}
	return keep, poison
}

// setHeld publishes the number of held records for /health.
func (w *flowWriter) setHeld() {
	n := 0
	for _, p := range w.held {
		n += len(p.recs)
	}
	w.state.mu.Lock()
	w.state.heldRecords = n
	w.state.mu.Unlock()
}

// abandon keeps what is left of a batch for the next start. A batch that was
// partly delivered (bisection in progress) is kept as its remaining parts, each
// under its own token, so the delivered part is never inserted twice.
func (w *flowWriter) abandon(b *flowBatch, remaining []*part) {
	lost := 0
	if remaining == nil || (len(remaining) == 1 && remaining[0].token == b.Token) {
		if !w.queue.Persist(b) {
			lost = len(b.Records)
		}
	} else {
		for i, p := range remaining {
			pb := &flowBatch{Token: p.token, Seq: b.Seq, Created: b.Created.Add(time.Duration(i)), Records: p.recs}
			if !w.queue.Persist(pb) {
				lost += len(p.recs)
			}
		}
		w.queue.Delivered(b) // the original segment is superseded by its parts
	}
	if lost > 0 {
		w.state.mu.Lock()
		w.state.lastDropAt = w.now()
		w.state.mu.Unlock()
		fmt.Printf("ERROR: netflow collector stopped with %d undelivered records it could not keep\n", lost)
	}
}

// part is a slice of a batch with its own stable token: the batch itself, or
// a half produced by bisection. Parts are never merged back together, because
// a part whose insert failed may still have committed (the rows landed and a
// view push failed), and only its own token makes its retry a no-op.
type part struct {
	token        string
	recs         []netflow.Record
	dataFailures int
	retested     bool // a single row already re-tried after other rows succeeded

	// Origin of a held part, so it is spooled in its original place in the
	// delivery order if the collector stops while it is held.
	batchSeq uint64
	created  time.Time
}

// maxBisectProbes caps single-row failures without any success in a batch:
// bisection then stops (it cannot degenerate into one insert per row) and the
// rest of the batch is held, or retried as a systemic fault when the held
// list is full.
const maxBisectProbes = 16

// deliver inserts one batch, retrying until it succeeds or stop is done.
// A batch the server rejects for its content is bisected; a row is only
// dead-lettered (and counted) when it fails on its own both before and after
// other rows of the batch were accepted, so one poison row can neither block
// the pipeline nor take its batch down with it, while a fault that is not in
// the rows (every row fails) keeps the batch and retries it like an outage.
//
// When nothing in the batch was accepted there is no batch-mate to compare
// with (a single-row batch, or a batch every row of which fails on its own):
// the undecided rows are held (not dead-lettered) and delivery moves on to
// the next batch, whose acceptance run() then uses as the evidence. Without
// that, one batch of bad rows from a single exporter would stall every flow
// behind it for good. When the held bounds are reached, the failure is judged
// systemic and the batch is retried here with backoff.
// accepted reports whether any row of the batch was written.
func (w *flowWriter) deliver(stop context.Context, b *flowBatch) (remaining []*part, accepted bool, ok bool) {
	parts := []*part{{token: b.Token, recs: b.Records}}
	var (
		suspects     []*part // single rows that failed with a data error
		anySucceeded bool
		systemic     bool
		attempt      int
	)
	for len(parts) > 0 || len(suspects) > 0 {
		if len(parts) == 0 {
			// Only suspects left. Re-test each once now that other rows were
			// accepted (the failure may have been passing). If nothing was
			// accepted, hold them for evidence from later batches, or, with
			// no room left, judge the fault systemic.
			if !anySucceeded && len(b.Records) > 1 {
				if w.canHold(suspects) {
					w.hold(b, suspects)
					return nil, false, true
				}
				systemic = true
				attempt = 1
			}
			for _, p := range suspects {
				p.retested = true
			}
			parts, suspects = suspects, nil
		}
		p := parts[0]
		if attempt > 0 {
			w.stats.processDelta(func(c *counters) { c.InsertRetries++ })
			if !w.sleep(stop, w.backoff(attempt)) {
				return append(suspects, parts...), anySucceeded, false
			}
		}
		if !w.ensureReady(stop) {
			attempt++
			continue
		}
		err := w.insert(stop, p.token, p.recs)
		if err == nil {
			w.succeeded(p.recs)
			anySucceeded = true
			// ClickHouse takes rows again, which refutes the systemic
			// hypothesis: a part that still fails from here on is judged on
			// its own rows (bisected, then dead-lettered if it fails alone).
			systemic = false
			attempt = 0
			parts = parts[1:]
			continue
		}
		if stop.Err() != nil {
			return append(suspects, parts...), anySucceeded, false
		}
		w.failed(err)
		switch classify(err) {
		case errSchema:
			w.state.schemaSuspect()
			attempt++
			continue
		case errTransient:
			attempt++
			continue
		}
		// Data error. Retry once with the same token first: if the rows had
		// committed and only a view push failed, that retry is deduplicated.
		p.dataFailures++
		if systemic || (p.dataFailures < 2 && !p.retested) {
			attempt++
			continue
		}
		attempt = 0
		parts = parts[1:]
		switch {
		case len(p.recs) > 1:
			mid := len(p.recs) / 2
			parts = append([]*part{{token: p.token + "L", recs: p.recs[:mid]}, {token: p.token + "R", recs: p.recs[mid:]}}, parts...)
		case p.retested:
			w.reject([]*part{p}, len(b.Records))
		case len(b.Records) == 1 && w.canHold([]*part{p}):
			w.hold(b, []*part{p})
		case len(b.Records) == 1:
			// Lone row after lone row fails and the held list is full: not
			// a row problem. Keep this one and retry it with backoff until
			// ClickHouse takes it.
			systemic = true
			parts = append([]*part{p}, parts...)
			attempt = 1
		default:
			suspects = append(suspects, p)
			if !anySucceeded && len(suspects) >= maxBisectProbes {
				// Row after row fails alone and none was accepted: stop
				// bisecting. Hold the rest of the batch for evidence from
				// later batches or, with no room left, judge the fault
				// systemic and retry everything with backoff.
				undecided := append(suspects, parts...)
				if w.canHold(undecided) {
					w.hold(b, undecided)
					return nil, false, true
				}
				systemic = true
				parts = undecided
				suspects = nil
				attempt = 1
			}
		}
	}
	return nil, anySucceeded, true
}

// reject dead-letters rows ClickHouse refused on their own and counts them.
// batchLen is the size of the batch they came from (0: rows that were held).
func (w *flowWriter) reject(ps []*part, batchLen int) {
	var rows []netflow.Record
	for _, p := range ps {
		rows = append(rows, p.recs...)
	}
	for _, r := range rows {
		w.deadLetter.write(r)
	}
	w.stats.recordsRejected(rows)
	w.stats.addPerRecord(rows, func(c *counters, n uint64) { c.RecordsDroppedImplausible += n })
	w.state.mu.Lock()
	w.state.lastRejectAt = w.now()
	w.state.mu.Unlock()
	if batchLen > 0 {
		fmt.Printf("ERROR: netflow: ClickHouse rejected %d record(s) of a %d-record batch on their own; dead-lettered\n", len(rows), batchLen)
	} else {
		fmt.Printf("ERROR: netflow: ClickHouse still rejected %d held record(s) on their own after accepting other rows; dead-lettered\n", len(rows))
	}
}

// insert runs one attempt with the per-insert timeout.
func (w *flowWriter) insert(stop context.Context, token string, recs []netflow.Record) error {
	ctx, cancel := opContext(stop, w.insertTimeout, w.stopBy)
	defer cancel()
	return w.store.InsertFlows(ctx, token, recs)
}

func (w *flowWriter) ensureReady(stop context.Context) bool {
	ctx, cancel := opContext(stop, w.insertTimeout, w.stopBy)
	defer cancel()
	if err := prepareSchema(ctx, w.store, w.state); err != nil {
		if stop.Err() == nil {
			w.failed(err)
		}
		return false
	}
	return true
}

// prepareSchema makes sure the collector's tables exist before the first
// write, and again after a write reported a missing table or column. An
// unreachable server is an error; failing DDL on a reachable server is only
// logged (the tables may be right already, e.g. without ALTER privilege),
// and the next write decides.
func prepareSchema(ctx context.Context, store flowStore, state *deliveryState) error {
	state.mu.Lock()
	ready := state.schemaReady
	state.mu.Unlock()
	if ready {
		return nil
	}
	if err := store.Ping(ctx); err != nil {
		return err
	}
	if err := store.EnsureSchema(ctx); err != nil {
		fmt.Printf("WARN: netflow ensure clickhouse schema: %v\n", err)
	}
	state.mu.Lock()
	state.schemaReady = true
	state.mu.Unlock()
	return nil
}

// schemaSuspect makes the next write re-run prepareSchema.
func (d *deliveryState) schemaSuspect() {
	d.mu.Lock()
	d.schemaReady = false
	d.mu.Unlock()
}

func (w *flowWriter) succeeded(recs []netflow.Record) {
	now := w.now()
	w.stats.recordsWritten(recs, now)
	w.state.mu.Lock()
	recovered := w.state.consecutiveFailures > 0
	w.state.lastOK = now
	w.state.consecutiveFailures = 0
	w.state.failingSince = time.Time{}
	w.state.mu.Unlock()
	if recovered {
		fmt.Println("INFO: netflow inserts recovered")
	}
}

func (w *flowWriter) failed(err error) {
	w.stats.processDelta(func(c *counters) { c.InsertFailures++ })
	label := errorLabel(err)
	w.state.mu.Lock()
	w.state.lastErr = w.now()
	w.state.lastErrLabel = label
	if w.state.consecutiveFailures == 0 {
		w.state.failingSince = w.state.lastErr
	}
	w.state.consecutiveFailures++
	n := w.state.consecutiveFailures
	w.state.mu.Unlock()
	// One line per failure streak step (1, 2, 4, 8, ...) keeps an outage
	// visible in the journal without a line per retry.
	if n&(n-1) == 0 {
		fmt.Printf("ERROR: netflow insert failed (%s, attempt %d): %v\n", label, n, err)
	}
}

func (w *flowWriter) backoff(attempt int) time.Duration {
	d := w.backoffMin << min(attempt-1, 16)
	if d <= 0 || d > w.backoffMax {
		d = w.backoffMax
	}
	jitter := time.Duration(rand.Int64N(int64(d)/5 + 1))
	return d - d/10 + jitter
}

// opContext bounds one ClickHouse operation: timeout from now, and never past
// the shutdown deadline once a stop has begun (stopBy, unix nanoseconds; 0 or
// nil = running). The driver only applies a context's DEADLINE to the socket
// (cancellation is checked between packets), so an operation blocked on a
// hung server ends at its deadline and nowhere earlier: without the cap an
// insert started during the shutdown drain could outlive the stop timeout
// and get the process killed with its queue still in memory.
func opContext(parent context.Context, timeout time.Duration, stopBy *atomic.Int64) (context.Context, context.CancelFunc) {
	deadline := time.Now().Add(timeout)
	if stopBy != nil {
		if by := stopBy.Load(); by != 0 && time.Unix(0, by).Before(deadline) {
			deadline = time.Unix(0, by)
		}
	}
	return context.WithDeadline(parent, deadline)
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// deadLetter keeps rows ClickHouse refused, as text, for inspection
// (NETFLOW_SPOOL_DIR/rejected.log, bounded). Without a spool directory they
// are only counted and logged.
type deadLetter struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
}

func (d *deadLetter) write(r netflow.Record) {
	if d == nil || d.path == "" {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if info, err := os.Stat(d.path); err == nil && info.Size() > d.maxBytes {
		_ = os.Rename(d.path, d.path+".1")
	}
	f, err := os.OpenFile(d.path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o640)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s rejected: %+v\n", time.Now().UTC().Format(time.RFC3339), r)
}
