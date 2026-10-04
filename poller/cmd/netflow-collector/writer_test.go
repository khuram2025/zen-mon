package main

import (
	"context"
	"errors"
	"fmt"
	"net/netip"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/zenplus/poller/internal/netflow"
)

// fakeStore behaves like flow_records with a deduplication window: a token
// that already committed is a no-op, whatever the payload.
type fakeStore struct {
	mu        sync.Mutex
	fail      func(call int, token string, recs []netflow.Record) (err error, commitFirst bool)
	calls     int
	tokens    []string
	committed map[string]bool
	rows      []netflow.Record
	pingErr   error
}

func newFakeStore() *fakeStore { return &fakeStore{committed: make(map[string]bool)} }

func (f *fakeStore) Ping(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.pingErr
}

func (f *fakeStore) EnsureSchema(context.Context) error { return nil }

func (f *fakeStore) InsertFlows(ctx context.Context, token string, recs []netflow.Record) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	f.tokens = append(f.tokens, token)
	if f.fail != nil {
		if err, commitFirst := f.fail(f.calls, token, recs); err != nil {
			if commitFirst && !f.committed[token] {
				// Ambiguous outcome: the rows landed, the client saw an error.
				f.committed[token] = true
				f.rows = append(f.rows, recs...)
			}
			return err
		}
	}
	if f.committed[token] {
		return nil
	}
	f.committed[token] = true
	f.rows = append(f.rows, recs...)
	return nil
}

func (f *fakeStore) snapshot() (rows []netflow.Record, tokens []string, calls int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]netflow.Record(nil), f.rows...), append([]string(nil), f.tokens...), f.calls
}

func testRecords(n int, exporter string) []netflow.Record {
	out := make([]netflow.Record, n)
	addr := netip.MustParseAddr(exporter)
	for i := range out {
		out[i] = netflow.Record{
			ExporterAddr:      addr,
			ObservationDomain: 7,
			ExportProtocol:    netflow.ProtocolNetFlow9,
			Bytes:             uint64(1000 + i),
			Packets:           1,
		}
	}
	return out
}

func newTestWriter(store flowStore, q *batchQueue, stats *statsRegistry) *flowWriter {
	return &flowWriter{
		store:         store,
		queue:         q,
		stats:         stats,
		state:         &deliveryState{},
		insertTimeout: time.Second,
		backoffMin:    time.Millisecond,
		backoffMax:    5 * time.Millisecond,
		deadLetter:    &deadLetter{},
		now:           time.Now,
		sleep:         sleepCtx,
		heldRetest:    5 * time.Millisecond,
	}
}

var errRefused = fmt.Errorf("dial tcp 127.0.0.1:19000: %w", syscall.ECONNREFUSED)

// NF-06: an outage fails several attempts, the batch is retried with the
// SAME token until it lands, and nothing is lost or duplicated.
func TestWriterOutageRetriesWithStableToken(t *testing.T) {
	store := newFakeStore()
	store.fail = func(call int, _ string, _ []netflow.Record) (error, bool) {
		if call <= 3 {
			return errRefused, false
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(100, 1000, time.Hour, "test-boot", nil)
	q.Add(testRecords(100, "192.0.2.1"))
	q.Close()
	w := newTestWriter(store, q, stats)
	w.run(context.Background())

	rows, tokens, calls := store.snapshot()
	if len(rows) != 100 || calls != 4 {
		t.Fatalf("rows=%d calls=%d, want 100 rows after 4 attempts", len(rows), calls)
	}
	for _, tok := range tokens {
		if tok != tokens[0] {
			t.Fatalf("token changed across retries: %v", tokens)
		}
	}
	totals, process, _ := stats.snapshot()
	if totals.RecordsWritten != 100 || process.InsertFailures != 3 || process.InsertRetries != 3 {
		t.Fatalf("written=%d failures=%d retries=%d", totals.RecordsWritten, process.InsertFailures, process.InsertRetries)
	}
	if st := w.state.snapshot(); st.consecutiveFailures != 0 || st.lastOK.IsZero() {
		t.Fatalf("delivery state not recovered: %+v", st)
	}
}

// An insert that committed but reported an error (timeout after the server
// accepted the block) is retried with the same token and deduplicated.
func TestWriterAmbiguousInsertNotDuplicated(t *testing.T) {
	store := newFakeStore()
	store.fail = func(call int, _ string, _ []netflow.Record) (error, bool) {
		if call == 1 {
			return context.DeadlineExceeded, true
		}
		return nil, false
	}
	q := newBatchQueue(50, 1000, time.Hour, "test-boot", nil)
	q.Add(testRecords(50, "192.0.2.1"))
	q.Close()
	newTestWriter(store, q, newStatsRegistry(time.Now())).run(context.Background())
	if rows, _, calls := store.snapshot(); len(rows) != 50 || calls != 2 {
		t.Fatalf("rows=%d calls=%d: the retry must be a deduplicated no-op", len(rows), calls)
	}
}

// A poison row the server rejects is isolated by bisection: it alone is
// dead-lettered and counted, every other row is written exactly once, and a
// second delivery of the same batch reproduces the same tokens.
func TestWriterPoisonRowBisected(t *testing.T) {
	poison := &clickhouse.Exception{Code: 53, Message: "Type mismatch"}
	run := func() (*fakeStore, *statsRegistry) {
		store := newFakeStore()
		store.fail = func(_ int, _ string, recs []netflow.Record) (error, bool) {
			for _, r := range recs {
				if r.Bytes == 1037 {
					return poison, false
				}
			}
			return nil, false
		}
		stats := newStatsRegistry(time.Now())
		q := newBatchQueue(100, 1000, time.Hour, "test-boot", nil)
		q.Add(testRecords(100, "192.0.2.1"))
		q.Close()
		newTestWriter(store, q, stats).run(context.Background())
		return store, stats
	}
	store, stats := run()
	rows, tokens, _ := store.snapshot()
	if len(rows) != 99 {
		t.Fatalf("rows written = %d, want 99", len(rows))
	}
	seen := map[uint64]int{}
	for _, r := range rows {
		seen[r.Bytes]++
		if r.Bytes == 1037 {
			t.Fatal("poison row was written")
		}
	}
	for b, n := range seen {
		if n != 1 {
			t.Fatalf("row %d written %d times", b, n)
		}
	}
	totals, _, _ := stats.snapshot()
	if totals.RecordsRejected != 1 || totals.RecordsDroppedImplausible != 1 || totals.RecordsWritten != 99 {
		t.Fatalf("rejected=%d implausible=%d written=%d", totals.RecordsRejected, totals.RecordsDroppedImplausible, totals.RecordsWritten)
	}
	store2, _ := run()
	_, tokens2, _ := store2.snapshot()
	if fmt.Sprint(tokens) != fmt.Sprint(tokens2) {
		t.Fatalf("bisection tokens are not deterministic:\n%v\n%v", tokens, tokens2)
	}
}

// waitFor polls cond until it holds or the deadline passes.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(time.Millisecond)
	}
}

// If every row fails on its own the fault may not be in the rows (for
// example a broken view): nothing is dead-lettered, the rows are held while
// newer batches keep flowing, and they are written exactly once when the
// server recovers.
func TestWriterSystemicDataErrorNotDeadLettered(t *testing.T) {
	var broken sync.Mutex
	failing := true
	store := newFakeStore()
	store.fail = func(_ int, _ string, _ []netflow.Record) (error, bool) {
		broken.Lock()
		defer broken.Unlock()
		if failing {
			return &clickhouse.Exception{Code: 395, Message: "view throwIf"}, false
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(40, 1000, time.Hour, "test-boot", nil)
	w := newTestWriter(store, q, stats)
	done := make(chan struct{})
	go func() { w.run(context.Background()); close(done) }()
	q.Add(testRecords(40, "192.0.2.1"))
	waitFor(t, "the failing rows to be held", func() bool { return w.state.snapshot().heldRecords == 40 })
	if totals, _, _ := stats.snapshot(); totals.RecordsRejected != 0 || totals.RecordsWritten != 0 {
		t.Fatalf("rejected=%d written=%d while the fault lasted", totals.RecordsRejected, totals.RecordsWritten)
	}
	broken.Lock()
	failing = false
	broken.Unlock()
	waitFor(t, "the held rows to be written", func() bool { return w.state.snapshot().heldRecords == 0 })
	q.Close()
	<-done
	rows, _, _ := store.snapshot()
	totals, _, _ := stats.snapshot()
	if len(rows) != 40 || totals.RecordsRejected != 0 || totals.RecordsWritten != 40 {
		t.Fatalf("rows=%d rejected=%d written=%d: a systemic failure must not dead-letter rows", len(rows), totals.RecordsRejected, totals.RecordsWritten)
	}
	seen := map[uint64]bool{}
	for _, r := range rows {
		if seen[r.Bytes] {
			t.Fatalf("row %d written twice", r.Bytes)
		}
		seen[r.Bytes] = true
	}
}

// One batch whose every row the server refuses (a single misbehaving
// exporter in a quiet minute) must not stall the batches behind it: they are
// written, and the refused rows are dead-lettered once other rows were
// accepted. Previously such a batch was judged systemic and retried forever,
// stopping all flow collection.
func TestWriterAllPoisonBatchDoesNotBlockLaterBatches(t *testing.T) {
	poison := &clickhouse.Exception{Code: 53, Message: "Type mismatch"}
	store := newFakeStore()
	store.fail = func(_ int, _ string, recs []netflow.Record) (error, bool) {
		for _, r := range recs {
			if r.Bytes >= 5000 {
				return poison, false
			}
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(10, 1000, 0, "test-boot", nil)
	bad := testRecords(3, "192.0.2.66")
	for i := range bad {
		bad[i].Bytes = uint64(5000 + i)
	}
	q.Add(bad)
	q.SealDue()
	q.Add(testRecords(10, "192.0.2.1"))
	q.Close()
	w := newTestWriter(store, q, stats)
	done := make(chan struct{})
	go func() { w.run(context.Background()); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("a batch of refused rows blocked the batches behind it")
	}
	rows, _, _ := store.snapshot()
	totals, _, _ := stats.snapshot()
	if len(rows) != 10 || totals.RecordsWritten != 10 || totals.RecordsRejected != 3 {
		t.Fatalf("rows=%d written=%d rejected=%d", len(rows), totals.RecordsWritten, totals.RecordsRejected)
	}
	if st := w.state.snapshot(); st.heldRecords != 0 {
		t.Fatalf("rows still held: %d", st.heldRecords)
	}
}

// The held list is bounded: once it is full, a batch that fails entirely is
// judged systemic and retried in place (delivery stalls visibly) rather than
// dead-lettered or dropped; everything is written once the server recovers.
func TestWriterHeldBoundTurnsIntoSystemicRetry(t *testing.T) {
	var mu sync.Mutex
	failing := true
	store := newFakeStore()
	store.fail = func(int, string, []netflow.Record) (error, bool) {
		mu.Lock()
		defer mu.Unlock()
		if failing {
			return &clickhouse.Exception{Code: 70, Message: "while pushing to view"}, false
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(10, 1000, time.Hour, "test-boot", nil)
	w := newTestWriter(store, q, stats)
	done := make(chan struct{})
	go func() { w.run(context.Background()); close(done) }()
	// Each 10-row batch is held as 10 single-row parts; the 7th exceeds
	// maxHeldParts (64) and is retried in place instead.
	for i := 0; i < 7; i++ {
		recs := testRecords(10, "192.0.2.1")
		for j := range recs {
			recs[j].Bytes = uint64(i*100 + j)
		}
		q.Add(recs)
	}
	waitFor(t, "the held list to fill", func() bool { return w.state.snapshot().heldRecords == 60 })
	waitFor(t, "the 7th batch to be retried in place", func() bool {
		_, _, calls := store.snapshot()
		return calls > 400 && w.state.snapshot().consecutiveFailures > 0
	})
	if totals, _, _ := stats.snapshot(); totals.RecordsRejected != 0 {
		t.Fatalf("rejected %d rows during a systemic fault", totals.RecordsRejected)
	}
	mu.Lock()
	failing = false
	mu.Unlock()
	waitFor(t, "recovery", func() bool {
		totals, _, _ := stats.snapshot()
		return w.state.snapshot().heldRecords == 0 && totals.RecordsWritten == 70
	})
	q.Close()
	<-done
	rows, _, _ := store.snapshot()
	if totals, _, _ := stats.snapshot(); len(rows) != 70 || totals.RecordsRejected != 0 {
		t.Fatalf("rows=%d rejected=%d", len(rows), totals.RecordsRejected)
	}
}

// X-03: records queued at shutdown are delivered with a fresh context, not
// lost with the cancelled signal context.
func TestWriterShutdownDrainsQueue(t *testing.T) {
	store := newFakeStore()
	q := newBatchQueue(1000, 10000, time.Hour, "test-boot", nil)
	for i := 0; i < 7; i++ {
		q.Add(testRecords(100, "192.0.2.1")) // 700 records, one partial batch
	}
	// main closes the queue once the reader has stopped and runs the writer
	// on its own context, which is only cancelled at the shutdown deadline.
	q.Close()
	newTestWriter(store, q, newStatsRegistry(time.Now())).run(context.Background())
	if rows, _, _ := store.snapshot(); len(rows) != 700 {
		t.Fatalf("delivered %d of 700 queued records at shutdown", len(rows))
	}
}

// When ClickHouse stays down through the shutdown deadline, undelivered
// batches go to the disk spool and a new process replays them with their
// original tokens.
func TestWriterShutdownSpoolsAndReplays(t *testing.T) {
	dir := t.TempDir()
	sp, err := openDiskSpool(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	down := newFakeStore()
	down.fail = func(int, string, []netflow.Record) (error, bool) { return errRefused, false }
	q := newBatchQueue(100, 10000, time.Hour, "boot-a", sp)
	q.Add(testRecords(250, "192.0.2.1"))
	q.Close()
	stop, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	newTestWriter(down, q, newStatsRegistry(time.Now())).run(stop)
	if n, _ := sp.usage(); n != 250 {
		t.Fatalf("spooled %d of 250 records", n)
	}
	_, firstTokens, _ := down.snapshot()

	sp2, err := openDiskSpool(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	up := newFakeStore()
	q2 := newBatchQueue(100, 10000, time.Hour, "boot-b", sp2)
	q2.Close()
	stats := newStatsRegistry(time.Now())
	newTestWriter(up, q2, stats).run(context.Background())
	rows, tokens, _ := up.snapshot()
	if len(rows) != 250 {
		t.Fatalf("replayed %d of 250 records", len(rows))
	}
	// Batch 1 was attempted before the stop; 2 and 3 were spooled unsent.
	// All three replay under the tokens they were created with.
	if len(firstTokens) == 0 || firstTokens[0] != "boot-a-1" {
		t.Fatalf("first process tokens: %v", firstTokens)
	}
	if fmt.Sprint(tokens) != "[boot-a-1 boot-a-2 boot-a-3]" {
		t.Fatalf("replay tokens %v, want the original batch tokens in order", tokens)
	}
	if n, _ := sp2.usage(); n != 0 {
		t.Fatalf("spool not emptied after replay: %d records", n)
	}
	if totals, _, _ := stats.snapshot(); totals.RecordsWritten != 250 {
		t.Fatalf("records_written = %d", totals.RecordsWritten)
	}
}

// A shutdown in the middle of bisection keeps the undelivered parts under
// their own tokens; the already-delivered half is not re-sent.
func TestWriterShutdownMidBisectKeepsRemainingParts(t *testing.T) {
	dir := t.TempDir()
	sp, _ := openDiskSpool(dir, 64<<20)
	store := newFakeStore()
	var stopOnce sync.Once
	stop, cancel := context.WithCancel(context.Background())
	store.fail = func(_ int, token string, recs []netflow.Record) (error, bool) {
		if len(recs) == 100 {
			return &clickhouse.Exception{Code: 53}, false
		}
		if len(recs) == 50 && token[len(token)-1] == 'R' {
			stopOnce.Do(cancel)
			return errRefused, false
		}
		return nil, false
	}
	q := newBatchQueue(100, 1000, time.Hour, "boot-a", sp)
	q.Add(testRecords(100, "192.0.2.1"))
	q.Close()
	newTestWriter(store, q, newStatsRegistry(time.Now())).run(stop)
	rows, _, _ := store.snapshot()
	if len(rows) != 50 {
		t.Fatalf("left half should be delivered: %d rows", len(rows))
	}
	if n, _ := sp.usage(); n != 50 {
		t.Fatalf("spool should hold only the undelivered half: %d", n)
	}
}

func TestClassify(t *testing.T) {
	cases := []struct {
		err  error
		want errClass
	}{
		{errRefused, errTransient},
		{context.DeadlineExceeded, errTransient},
		{&clickhouse.Exception{Code: 252}, errTransient}, // TOO_MANY_PARTS
		{&clickhouse.Exception{Code: 16}, errSchema},
		{&clickhouse.Exception{Code: 60}, errSchema},
		{&clickhouse.Exception{Code: 53}, errData},
		{fmt.Errorf("wrapped: %w", &clickhouse.Exception{Code: 70}), errData},
		{&appendError{index: 3, err: errors.New("convert")}, errData},
	}
	for _, c := range cases {
		if got := classify(c.err); got != c.want {
			t.Errorf("classify(%v) = %v, want %v", c.err, got, c.want)
		}
	}
	if l := errorLabel(errRefused); l != "connection refused" {
		t.Errorf("label %q", l)
	}
}

// A single-row batch has no batch-mates to prove its row is at fault: its
// row is held until another batch is accepted, and only then dead-lettered
// if it still fails on its own.
func TestWriterLonePoisonRowJudgedAfterOtherBatch(t *testing.T) {
	poison := &clickhouse.Exception{Code: 53, Message: "Type mismatch"}
	store := newFakeStore()
	store.fail = func(_ int, _ string, recs []netflow.Record) (error, bool) {
		for _, r := range recs {
			if r.Bytes == 666 {
				return poison, false
			}
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(10, 1000, 0, "test-boot", nil)
	lone := testRecords(1, "192.0.2.1")
	lone[0].Bytes = 666
	q.Add(lone)
	q.SealDue() // a one-record batch, as a quiet exporter produces
	q.Add(testRecords(10, "192.0.2.1"))
	q.Close()
	w := newTestWriter(store, q, stats)
	w.run(context.Background())

	rows, _, _ := store.snapshot()
	totals, _, _ := stats.snapshot()
	if len(rows) != 10 || totals.RecordsRejected != 1 || totals.RecordsWritten != 10 {
		t.Fatalf("rows=%d rejected=%d written=%d", len(rows), totals.RecordsRejected, totals.RecordsWritten)
	}
	if st := w.state.snapshot(); st.heldRecords != 0 || st.lastRejectAt.IsZero() {
		t.Fatalf("delivery state: %+v", st)
	}
}

// When single-row batches fail because of a fault that is not in the rows
// (a broken view on a low-traffic appliance), they are held, not
// dead-lettered, and all of them are written once ClickHouse recovers.
func TestWriterLoneRowsSystemicFaultNotDeadLettered(t *testing.T) {
	var mu sync.Mutex
	failing := true
	store := newFakeStore()
	store.fail = func(int, string, []netflow.Record) (error, bool) {
		mu.Lock()
		defer mu.Unlock()
		if failing {
			return &clickhouse.Exception{Code: 70, Message: "while pushing to view"}, false
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(10, 1000, 0, "test-boot", nil)
	w := newTestWriter(store, q, stats)
	done := make(chan struct{})
	go func() { w.run(context.Background()); close(done) }()
	for i := 0; i < 5; i++ {
		q.Add(testRecords(1, "192.0.2.1"))
		q.SealDue()
	}
	deadline := time.Now().Add(3 * time.Second)
	for w.state.snapshot().heldRecords < 5 {
		if time.Now().After(deadline) {
			t.Fatalf("rows not held: %+v", w.state.snapshot())
		}
		time.Sleep(time.Millisecond)
	}
	mu.Lock()
	failing = false
	mu.Unlock()
	q.Close()
	<-done
	rows, _, _ := store.snapshot()
	totals, _, _ := stats.snapshot()
	if len(rows) != 5 || totals.RecordsRejected != 0 || totals.RecordsWritten != 5 {
		t.Fatalf("rows=%d rejected=%d written=%d: a systemic fault must not dead-letter rows", len(rows), totals.RecordsRejected, totals.RecordsWritten)
	}
}

// Rows still held when the collector stops are spooled under their original
// tokens and replayed by the next process.
func TestWriterHeldRowsSpooledAtShutdown(t *testing.T) {
	dir := t.TempDir()
	sp, _ := openDiskSpool(dir, 64<<20)
	store := newFakeStore()
	store.fail = func(int, string, []netflow.Record) (error, bool) {
		return &clickhouse.Exception{Code: 53}, false
	}
	q := newBatchQueue(10, 1000, 0, "boot-a", sp)
	q.Add(testRecords(1, "192.0.2.1"))
	q.SealDue()
	q.Close()
	newTestWriter(store, q, newStatsRegistry(time.Now())).run(context.Background())
	if n, _ := sp.usage(); n != 1 {
		t.Fatalf("held row not spooled: %d", n)
	}
	sp2, _ := openDiskSpool(dir, 64<<20)
	up := newFakeStore()
	q2 := newBatchQueue(10, 1000, 0, "boot-b", sp2)
	q2.Close()
	newTestWriter(up, q2, newStatsRegistry(time.Now())).run(context.Background())
	if rows, tokens, _ := up.snapshot(); len(rows) != 1 || fmt.Sprint(tokens) != "[boot-a-1]" {
		t.Fatalf("replay rows=%d tokens=%v", len(rows), tokens)
	}
}

// Once rows are accepted again after a systemic fault, a row that keeps
// failing is judged on its own: the held part holding it is bisected, the
// row is dead-lettered and its batch-mates are written.
func TestWriterPoisonRowAfterSystemicRecovery(t *testing.T) {
	var mu sync.Mutex
	broken := true
	store := newFakeStore()
	store.fail = func(_ int, _ string, recs []netflow.Record) (error, bool) {
		mu.Lock()
		defer mu.Unlock()
		if broken {
			return &clickhouse.Exception{Code: 395, Message: "view throwIf"}, false
		}
		for _, r := range recs {
			if r.Bytes == 1005 {
				return &clickhouse.Exception{Code: 53, Message: "Type mismatch"}, false
			}
		}
		return nil, false
	}
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(40, 1000, time.Hour, "test-boot", nil)
	w := newTestWriter(store, q, stats)
	done := make(chan struct{})
	go func() { w.run(context.Background()); close(done) }()
	q.Add(testRecords(40, "192.0.2.1"))
	waitFor(t, "the rows to be held", func() bool { return w.state.snapshot().heldRecords == 40 })
	mu.Lock()
	broken = false
	mu.Unlock()
	waitFor(t, "the held rows to be judged", func() bool { return w.state.snapshot().heldRecords == 0 })
	q.Close()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("writer blocked on a poison row after the systemic fault cleared")
	}
	rows, _, _ := store.snapshot()
	totals, _, _ := stats.snapshot()
	if len(rows) != 39 || totals.RecordsRejected != 1 || totals.RecordsWritten != 39 {
		t.Fatalf("rows=%d rejected=%d written=%d", len(rows), totals.RecordsRejected, totals.RecordsWritten)
	}
}

// hungStore models a ClickHouse that accepted the connection and never
// answers: like the driver blocked in a socket read, it ignores
// cancellation and returns only when the context's deadline passes.
type hungStore struct{ fakeStore }

func (h *hungStore) InsertFlows(ctx context.Context, token string, recs []netflow.Record) error {
	if dl, ok := ctx.Deadline(); ok {
		time.Sleep(time.Until(dl))
	}
	return context.DeadlineExceeded
}

// Once a stop has begun, no ClickHouse operation may outlive the shutdown
// deadline, or systemd kills the process (TimeoutStopSec) with the queue
// still in memory instead of in the spool.
func TestWriterInsertsCappedByShutdownDeadline(t *testing.T) {
	dir := t.TempDir()
	sp, _ := openDiskSpool(dir, 64<<20)
	q := newBatchQueue(10, 1000, time.Hour, "boot-a", sp)
	q.Add(testRecords(30, "192.0.2.1"))
	q.Close()
	w := newTestWriter(&hungStore{fakeStore: *newFakeStore()}, q, newStatsRegistry(time.Now()))
	w.insertTimeout = time.Minute
	var stopBy atomic.Int64
	w.stopBy = &stopBy

	// What main does when the signal arrives: the deadline, then the writer
	// context cancelled when it passes.
	const shutdown = 200 * time.Millisecond
	stopBy.Store(time.Now().Add(shutdown).UnixNano())
	stop, cancel := context.WithCancel(context.Background())
	time.AfterFunc(shutdown, cancel)
	start := time.Now()
	w.run(stop)
	if took := time.Since(start); took > shutdown+2*time.Second {
		t.Fatalf("writer took %v to stop with a %v deadline", took, shutdown)
	}
	if n, _ := sp.usage(); n != 30 {
		t.Fatalf("spooled %d records, want all 30", n)
	}
}

func TestOpContextDeadline(t *testing.T) {
	var stopBy atomic.Int64
	ctx, cancel := opContext(context.Background(), time.Minute, &stopBy)
	dl, _ := ctx.Deadline()
	cancel()
	if d := time.Until(dl); d < 59*time.Second {
		t.Fatalf("running: deadline in %v, want the full timeout", d)
	}
	stopBy.Store(time.Now().Add(time.Second).UnixNano())
	ctx, cancel = opContext(context.Background(), time.Minute, &stopBy)
	dl, _ = ctx.Deadline()
	cancel()
	if d := time.Until(dl); d > time.Second {
		t.Fatalf("stopping: deadline in %v, want at most the shutdown deadline", d)
	}
	if ctx, cancel := opContext(context.Background(), time.Second, nil); ctx.Err() != nil {
		t.Fatal("nil stopBy must not cap")
	} else {
		cancel()
	}
}
