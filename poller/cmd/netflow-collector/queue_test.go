package main

import (
	"context"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Without a disk spool, a full queue drops the OLDEST batches and counts
// every dropped record; Add never blocks.
func TestQueueDropsOldestWhenFull(t *testing.T) {
	stats := newStatsRegistry(time.Now())
	q := newBatchQueue(10, 30, time.Hour, "t", nil)
	q.onDrop = stats.recordsDroppedQueue
	start := time.Now()
	for i := 0; i < 10; i++ {
		recs := testRecords(10, "192.0.2.1")
		for j := range recs {
			recs[j].Bytes = uint64(i)
		}
		q.Add(recs)
	}
	if time.Since(start) > 100*time.Millisecond {
		t.Fatal("Add must not block")
	}
	if n, c := q.Len(); n != 30 || c != 30 {
		t.Fatalf("queue len=%d cap=%d", n, c)
	}
	totals, _, _ := stats.snapshot()
	if totals.RecordsDroppedQueue != 70 {
		t.Fatalf("dropped = %d, want 70", totals.RecordsDroppedQueue)
	}
	q.Close()
	b := q.Next(context.Background())
	if b == nil || b.Records[0].Bytes != 7 {
		t.Fatalf("oldest surviving batch should be #7, got %+v", b)
	}
}

// Partial batches are sealed after the flush interval so quiet exporters
// are written promptly.
func TestQueueSealDue(t *testing.T) {
	q := newBatchQueue(1000, 10000, 20*time.Millisecond, "t", nil)
	q.Add(testRecords(3, "192.0.2.1"))
	q.SealDue()
	if n, _ := q.Len(); n != 3 {
		t.Fatalf("len %d", n)
	}
	time.Sleep(25 * time.Millisecond)
	q.SealDue()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if b := q.Next(ctx); b == nil || len(b.Records) != 3 {
		t.Fatalf("partial batch not sealed: %+v", b)
	}
}

// Over the memory cap the oldest batches spill to disk and are drained
// first (FIFO across disk and memory).
func TestQueueSpillsToDiskFIFO(t *testing.T) {
	dir := t.TempDir()
	sp, err := openDiskSpool(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	q := newBatchQueue(10, 40, time.Hour, "t", sp)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go q.runSpiller(ctx)
	// settled waits until the spiller has brought memory back under the cap,
	// so the next Add never reaches the hard limit (which drops the oldest
	// batch by design when the disk cannot keep up); fsync speed must not
	// decide the outcome of this test.
	settled := func() {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for n, c := q.Len(); n > c; n, c = q.Len() {
			if time.Now().After(deadline) {
				t.Fatalf("spiller did not bring the queue under its cap: %d > %d", n, c)
			}
			time.Sleep(time.Millisecond)
		}
	}
	for i := 0; i < 8; i++ {
		recs := testRecords(10, "192.0.2.1")
		for j := range recs {
			recs[j].Bytes = uint64(i)
		}
		q.Add(recs)
		settled()
	}
	if n, _ := sp.usage(); n == 0 {
		t.Fatal("nothing spilled to disk")
	}
	q.Close()
	var order []uint64
	for b := q.Next(ctx); b != nil; b = q.Next(ctx) {
		order = append(order, b.Records[0].Bytes)
		q.Delivered(b)
	}
	if len(order) != 8 {
		t.Fatalf("batches delivered: %v", order)
	}
	for i, v := range order {
		if v != uint64(i) {
			t.Fatalf("delivery order %v is not FIFO", order)
		}
	}
	if n, _ := sp.usage(); n != 0 {
		t.Fatalf("spool not drained: %d", n)
	}
}

// A full spool evicts its oldest segment, never the in-flight one, and
// counts the evicted records.
func TestSpoolEvictsOldest(t *testing.T) {
	dir := t.TempDir()
	probe := &flowBatch{Token: "x", Seq: 1, Created: time.Now(), Records: testRecords(10, "192.0.2.1")}
	data, _ := encodeSegment(probe)
	sp, _ := openDiskSpool(dir, int64(len(data))*2+10)
	for i := 1; i <= 2; i++ {
		b := &flowBatch{Token: "x", Seq: uint64(i), Created: time.Unix(int64(i), 0), Records: testRecords(10, "192.0.2.1")}
		if ev, err := sp.write(b); err != nil || ev != 0 {
			t.Fatalf("write %d: evicted=%d err=%v", i, ev, err)
		}
	}
	head, _, _ := sp.peek() // segment 1 is now in flight
	b3 := &flowBatch{Token: "x", Seq: 3, Created: time.Unix(3, 0), Records: testRecords(10, "192.0.2.1")}
	ev, err := sp.write(b3)
	if err != nil || ev != 10 {
		t.Fatalf("third write: evicted=%d err=%v (should evict segment 2, not the in-flight 1)", ev, err)
	}
	sp.drop(head.spoolName)
	next, _, _ := sp.peek()
	if next == nil || next.Created.Unix() != 3 {
		t.Fatalf("remaining segment: %+v", next)
	}
}

// Segments survive a restart; a corrupt one is set aside and counted.
func TestSpoolReloadAndCorruptSegment(t *testing.T) {
	dir := t.TempDir()
	sp, _ := openDiskSpool(dir, 64<<20)
	b := &flowBatch{Token: "tok-1", Seq: 1, Created: time.Unix(10, 0), Records: testRecords(5, "2001:db8::1")}
	b.Records[0].SrcAddr6 = []byte{0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1}
	if _, err := sp.write(b); err != nil {
		t.Fatal(err)
	}
	bad := &flowBatch{Token: "tok-0", Seq: 0, Created: time.Unix(5, 0), Records: testRecords(3, "192.0.2.1")}
	if err := os.WriteFile(filepath.Join(dir, segmentName(bad)), []byte("garbage"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, spoolTmpPrefix+"half-written"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	sp2, err := openDiskSpool(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	got, corrupt, err := sp2.peek()
	if err != nil || corrupt != 3 || got == nil || got.Token != "tok-1" || len(got.Records) != 5 {
		t.Fatalf("peek: corrupt=%d err=%v got=%+v", corrupt, err, got)
	}
	if got.Records[0].ExporterAddr != netip.MustParseAddr("2001:db8::1") || got.Records[0].SrcAddr6.String() != "2001:db8::1" {
		t.Fatalf("record fields lost in the spool round trip: %+v", got.Records[0])
	}
	entries, _ := os.ReadDir(dir)
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	joined := strings.Join(names, " ")
	if strings.Contains(joined, spoolTmpPrefix) || !strings.Contains(joined, spoolBadSuffix) {
		t.Fatalf("spool dir after reload: %v", names)
	}
}

// Reproduce the exact handoff interleaving without sleeps or scheduling luck:
// Next observed an empty spool, then the spiller committed the oldest batch
// before Next could take the memory head. The newer batch must not overtake it.
func TestQueueSpillCommittedBeforeMemoryHandoff(t *testing.T) {
	sp, err := openDiskSpool(t.TempDir(), 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	q := newBatchQueue(1, 4, time.Hour, "handoff", sp)
	q.Add(testRecords(1, "192.0.2.1"))
	q.Add(testRecords(1, "192.0.2.2"))
	if !sp.empty() {
		t.Fatal("expected initial empty-spool observation")
	}
	oldest := q.sealed[0]
	newer := q.sealed[1]
	data, err := encodeSegment(oldest)
	if err != nil {
		t.Fatal(err)
	}
	tmp, err := sp.writeTemp(oldest, data)
	if err != nil {
		t.Fatal(err)
	}
	q.mu.Lock()
	_, err = sp.commit(tmp, oldest)
	if err == nil {
		q.sealed = q.sealed[1:]
		q.memRecords -= len(oldest.Records)
	}
	q.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	b, _, retry := q.takeMemory()
	if b != nil || !retry {
		t.Fatal("newer memory batch overtook the committed disk batch")
	}
	if n, _ := q.Len(); n != 1 {
		t.Fatalf("memory batch was lost: %d", n)
	}
	q.Close()
	for _, token := range []string{oldest.Token, newer.Token} {
		b := q.Next(context.Background())
		if b == nil || b.Token != token {
			t.Fatalf("wanted token %s, got %+v", token, b)
		}
		q.Delivered(b)
	}
	if q.Next(context.Background()) != nil || !q.Finished() {
		t.Fatal("queue did not drain")
	}
}
