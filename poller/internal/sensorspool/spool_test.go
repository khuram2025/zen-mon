package sensorspool

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestSpoolPersistsDeduplicatesAndKeepsBatchKey(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	opts := Options{MaxBytes: 1 << 20, MaxAge: 24 * time.Hour, Now: func() time.Time { return now }}

	spool, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	item := Item{ID: "result-1", Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`)}
	retained, err := spool.Enqueue(item)
	if err != nil || !retained {
		t.Fatalf("enqueue retained=%v err=%v", retained, err)
	}
	retained, err = spool.Enqueue(item)
	if err != nil || retained {
		t.Fatalf("duplicate enqueue retained=%v err=%v", retained, err)
	}
	if got := spool.Stats().Depth; got != 1 {
		t.Fatalf("depth after duplicate = %d, want 1", got)
	}
	beforeRestart, err := spool.NextBatch(10)
	if err != nil || beforeRestart == nil {
		t.Fatalf("next batch before restart: batch=%v err=%v", beforeRestart, err)
	}
	if retained, err := spool.Enqueue(Item{
		ID: "result-2", Path: "/results/ping", Payload: json.RawMessage(`{"value":2}`),
	}); err != nil || !retained {
		t.Fatalf("enqueue during retry retained=%v err=%v", retained, err)
	}
	retry, err := spool.NextBatch(10)
	if err != nil || retry == nil || retry.Key != beforeRestart.Key || len(retry.Items) != 1 {
		t.Fatalf("in-flight batch changed when a result arrived: batch=%v err=%v", retry, err)
	}

	recovered, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	afterRestart, err := recovered.NextBatch(10)
	if err != nil || afterRestart == nil {
		t.Fatalf("next batch after restart: batch=%v err=%v", afterRestart, err)
	}
	if beforeRestart.Key != afterRestart.Key {
		t.Fatalf("idempotency key changed across restart: %q != %q", beforeRestart.Key, afterRestart.Key)
	}
	if len(afterRestart.Items) != 1 {
		t.Fatalf("in-flight membership changed across restart: got %d items", len(afterRestart.Items))
	}
	if string(afterRestart.Items[0]) != `{"value":1}` {
		t.Fatalf("payload changed across restart: %s", afterRestart.Items[0])
	}
	if err := recovered.Ack(afterRestart); err != nil {
		t.Fatal(err)
	}
	next, err := recovered.NextBatch(10)
	if err != nil || next == nil || len(next.Items) != 1 || string(next.Items[0]) != `{"value":2}` {
		t.Fatalf("result appended during retry was not queued next: batch=%v err=%v", next, err)
	}
	if err := recovered.Ack(next); err != nil {
		t.Fatal(err)
	}

	empty, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	if got := empty.Stats().Depth; got != 0 {
		t.Fatalf("depth after ack and restart = %d, want 0", got)
	}
}

func TestSpoolEvictsOldestByByteLimitAndPersistsDrops(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	large := Options{MaxBytes: 1 << 20, MaxAge: 24 * time.Hour, Now: func() time.Time { return now }}
	spool, err := Open(dir, large)
	if err != nil {
		t.Fatal(err)
	}
	spool.segmentTarget = 1 // one immutable segment per item for deterministic reclamation
	for i, id := range []string{"oldest", "middle", "newest"} {
		retained, err := spool.Enqueue(Item{
			ID: id, Path: "/results/service",
			CreatedAt: now.Add(time.Duration(i) * time.Second),
			Payload:   json.RawMessage(`{"same_size":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}`),
		})
		if err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", id, retained, err)
		}
	}

	initialBytes := spool.Stats().Bytes
	// Leave enough room for two data segments plus the compact control snapshot,
	// but not all three after the queue's reserved control-WAL headroom. Segment
	// deletion, not payload copying, must reclaim it.
	maxBytes := initialBytes

	bounded, err := Open(dir, Options{MaxBytes: maxBytes, MaxAge: 24 * time.Hour, Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	stats := bounded.Stats()
	if stats.Depth != 2 || stats.Dropped != 1 || stats.Bytes > maxBytes {
		t.Fatalf("bounded stats = %+v, maxBytes=%d", stats, maxBytes)
	}
	batch, err := bounded.NextBatch(10)
	if err != nil || batch == nil {
		t.Fatalf("next batch: batch=%v err=%v", batch, err)
	}
	if batch.ids[0] != "middle" {
		t.Fatalf("oldest retained id = %q, want middle", batch.ids[0])
	}

	recovered, err := Open(dir, Options{MaxBytes: maxBytes, MaxAge: 24 * time.Hour, Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	if got := recovered.Stats().Dropped; got != 1 {
		t.Fatalf("persisted drop count = %d, want 1", got)
	}
}

func TestSpoolTruncatesTornSegmentTail(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	opts := Options{MaxBytes: 1 << 20, MaxAge: time.Hour}
	spool, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	if retained, err := spool.Enqueue(Item{
		ID: "durable", Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`),
	}); err != nil || !retained {
		t.Fatalf("enqueue retained=%v err=%v", retained, err)
	}
	segmentPath := spool.entries[0].segment.path
	validSize := spool.entries[0].segment.size
	f, err := os.OpenFile(segmentPath, os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte{0, 0, 0, 20, 1, 2}); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}

	recovered, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	if recovered.Stats().Depth != 1 {
		t.Fatalf("valid record was lost after torn tail: %+v", recovered.Stats())
	}
	info, err := os.Stat(segmentPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() != validSize {
		t.Fatalf("torn tail was not truncated: size=%d want=%d", info.Size(), validSize)
	}
}

func TestSpoolTruncatesTornControlTailAndKeepsInflight(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	opts := Options{MaxBytes: 1 << 20, MaxAge: time.Hour, Now: func() time.Time { return now }}
	spool, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"first", "second"} {
		if retained, err := spool.Enqueue(Item{
			ID: id, Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`),
		}); err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", id, retained, err)
		}
	}
	before, err := spool.NextBatch(10)
	if err != nil || before == nil || len(before.Items) != 2 {
		t.Fatalf("initial batch=%v err=%v", before, err)
	}
	validSize := spool.controlBytes
	f, err := os.OpenFile(spool.controlPath, os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	// This is a valid frame prefix whose declared payload was only partly
	// written. Recovery must discard it without disturbing the preceding
	// durable in-flight manifest.
	if _, err := f.Write([]byte{0, 0, 0, 64, 1, 2, 3, 4, '{', '"', 'v'}); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}

	recovered, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := recovered.NextBatch(10)
	if err != nil || retry == nil {
		t.Fatalf("recovered batch=%v err=%v", retry, err)
	}
	if retry.Key != before.Key || retry.Path != before.Path || strings.Join(retry.ids, ",") != strings.Join(before.ids, ",") {
		t.Fatalf("in-flight identity changed after torn control tail: before=%+v after=%+v", before, retry)
	}
	if stats := recovered.Stats(); stats.Depth != 2 || stats.Dropped != 0 {
		t.Fatalf("recovered stats=%+v, want depth=2 dropped=0", stats)
	}
	info, err := os.Stat(recovered.controlPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() != validSize {
		t.Fatalf("torn control tail was not truncated: size=%d want=%d", info.Size(), validSize)
	}
}

func TestSpoolRejectsCommittedSegmentChecksumCorruption(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	spool, err := Open(dir, Options{MaxBytes: 1 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if retained, err := spool.Enqueue(Item{
		ID: "corrupt", Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`),
	}); err != nil || !retained {
		t.Fatalf("enqueue retained=%v err=%v", retained, err)
	}
	path := spool.entries[0].segment.path
	f, err := os.OpenFile(path, os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	position := int64(frameHeader + 5)
	var value [1]byte
	if _, err := f.ReadAt(value[:], position); err != nil {
		f.Close()
		t.Fatal(err)
	}
	value[0] ^= 0xff
	if _, err := f.WriteAt(value[:], position); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, Options{MaxBytes: 1 << 20, MaxAge: time.Hour}); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("committed corruption was not rejected: %v", err)
	}
}

func TestSpoolRejectsCommittedControlChecksumCorruption(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	opts := Options{MaxBytes: 1 << 20, MaxAge: time.Hour}
	spool, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	if retained, err := spool.Enqueue(Item{
		ID: "control-corrupt", Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`),
	}); err != nil || !retained {
		t.Fatalf("enqueue retained=%v err=%v", retained, err)
	}
	if batch, err := spool.NextBatch(1); err != nil || batch == nil {
		t.Fatalf("next batch=%v err=%v", batch, err)
	}
	f, err := os.OpenFile(spool.controlPath, os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	position := int64(frameHeader + 5)
	var value [1]byte
	if _, err := f.ReadAt(value[:], position); err != nil {
		f.Close()
		t.Fatal(err)
	}
	value[0] ^= 0xff
	if _, err := f.WriteAt(value[:], position); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, opts); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("committed control corruption was not rejected: %v", err)
	}
}

func TestSpoolPersistsSplitAndPoisonDropAcrossRestart(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	opts := Options{MaxBytes: 1 << 20, MaxAge: time.Hour}
	spool, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"poison", "valid"} {
		if retained, err := spool.Enqueue(Item{
			ID: id, Path: "/results/service", Payload: json.RawMessage(`{"value":1}`),
		}); err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", id, retained, err)
		}
	}
	batch, err := spool.NextBatch(10)
	if err != nil || batch == nil || len(batch.Items) != 2 {
		t.Fatalf("initial batch=%v err=%v", batch, err)
	}
	if err := spool.SplitInflight(batch); err != nil {
		t.Fatal(err)
	}

	recovered, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	isolated, err := recovered.NextBatch(10)
	if err != nil || isolated == nil || len(isolated.Items) != 1 || isolated.ids[0] != "poison" {
		t.Fatalf("split manifest was not recovered: batch=%v err=%v", isolated, err)
	}
	if err := recovered.DropInflight(isolated); err != nil {
		t.Fatal(err)
	}

	afterDrop, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	if stats := afterDrop.Stats(); stats.Depth != 1 || stats.Dropped != 1 {
		t.Fatalf("poison drop did not persist: %+v", stats)
	}
	next, err := afterDrop.NextBatch(10)
	if err != nil || next == nil || len(next.Items) != 1 || next.ids[0] != "valid" {
		t.Fatalf("valid record did not remain after poison drop: batch=%v err=%v", next, err)
	}
}

func TestSpoolReclaimsConsumedSegmentsWithoutRewritingLaterData(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	spool, err := Open(dir, Options{MaxBytes: 1 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	spool.segmentTarget = 1
	for _, id := range []string{"first", "second", "third"} {
		if retained, err := spool.Enqueue(Item{
			ID: id, Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`),
		}); err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", id, retained, err)
		}
	}
	firstPath := spool.entries[0].segment.path
	laterPath := spool.entries[1].segment.path
	laterBefore, err := os.ReadFile(laterPath)
	if err != nil {
		t.Fatal(err)
	}
	batch, err := spool.NextBatch(10)
	if err != nil || batch == nil || len(batch.Items) != 1 || batch.ids[0] != "first" {
		t.Fatalf("oldest segment batch=%v err=%v", batch, err)
	}
	if err := spool.Ack(batch); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(firstPath); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("consumed segment still exists: %v", err)
	}
	laterAfter, err := os.ReadFile(laterPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(laterAfter) != string(laterBefore) {
		t.Fatal("acknowledging an old segment rewrote a later data segment")
	}
	if matches, err := filepath.Glob(filepath.Join(dir, ".control-*.tmp")); err != nil || len(matches) != 0 {
		t.Fatalf("control snapshot temp leaked: matches=%v err=%v", matches, err)
	}
}

func TestSpoolProtectsInflightBatchFromRetentionAndCap(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	current := now
	opts := Options{MaxBytes: 4096, MaxAge: time.Hour, Now: func() time.Time { return current }}
	spool, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"protected-1", "protected-2"} {
		if retained, err := spool.Enqueue(Item{
			ID: id, Path: "/results/ping", Payload: json.RawMessage(`{"value":"protected"}`),
		}); err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", id, retained, err)
		}
	}
	batch, err := spool.NextBatch(10)
	if err != nil || batch == nil || len(batch.Items) != 2 {
		t.Fatalf("initial batch=%v err=%v", batch, err)
	}

	current = now.Add(2 * time.Hour)
	if _, err := spool.Enqueue(Item{
		ID: "new", Path: "/results/ping", Payload: json.RawMessage(`{"value":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}`),
	}); err != nil {
		t.Fatal(err)
	}
	retry, err := spool.NextBatch(10)
	if err != nil || retry == nil || retry.Key != batch.Key || len(retry.Items) != 2 {
		t.Fatalf("protected retry changed: batch=%v err=%v", retry, err)
	}

	recovered, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	afterRestart, err := recovered.NextBatch(10)
	if err != nil || afterRestart == nil || afterRestart.Key != batch.Key || len(afterRestart.Items) != 2 {
		t.Fatalf("protected retry changed after restart: batch=%v err=%v", afterRestart, err)
	}
}

func TestSpoolDoesNotDropBystandersWhenInflightSegmentPreventsFit(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	spool, err := Open(dir, Options{MaxBytes: 1 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range []Item{
		{ID: "protected", Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`)},
		{ID: "bystander", Path: "/results/service", Payload: json.RawMessage(`{"value":2}`)},
	} {
		if retained, err := spool.Enqueue(item); err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", item.ID, retained, err)
		}
	}
	batch, err := spool.NextBatch(1)
	if err != nil || batch == nil || batch.ids[0] != "protected" {
		t.Fatalf("protected batch=%v err=%v", batch, err)
	}
	// Leave less space than another data frame needs. The bystander shares the
	// protected immutable segment, so dropping it cannot create disk capacity.
	spool.maxBytes = spool.diskBytes + 16
	retained, err := spool.Enqueue(Item{
		ID: "cannot-fit", Path: "/results/ping", Payload: json.RawMessage(`{"value":"new"}`),
	})
	if err != nil || retained {
		t.Fatalf("oversubscribed enqueue retained=%v err=%v", retained, err)
	}
	if stats := spool.Stats(); stats.Depth != 2 || stats.Dropped != 1 {
		t.Fatalf("bystander was sacrificed without freeing capacity: %+v", stats)
	}
	if _, exists := spool.byID["bystander"]; !exists {
		t.Fatal("bystander sharing the protected segment was dropped")
	}
}

func TestSpoolEvictsExpiredBeforeFresh(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	spool, err := Open(dir, Options{MaxBytes: 1 << 20, MaxAge: time.Hour, Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	retained, err := spool.Enqueue(Item{
		ID: "fresh", Path: "/results/ping", CreatedAt: now, Payload: json.RawMessage(`{"old":false}`),
	})
	if err != nil || !retained {
		t.Fatalf("fresh enqueue retained=%v err=%v", retained, err)
	}
	// Insert the expired record second to prove age eviction does not assume
	// wall-clock order matches append order (the clock may move backwards).
	if _, err := spool.Enqueue(Item{
		ID: "expired", Path: "/results/ping", CreatedAt: now.Add(-2 * time.Hour), Payload: json.RawMessage(`{"old":true}`),
	}); err != nil {
		t.Fatal(err)
	}
	stats := spool.Stats()
	if stats.Depth != 1 || stats.Dropped != 1 {
		t.Fatalf("stats = %+v, want depth=1 dropped=1", stats)
	}
	batch, err := spool.NextBatch(1)
	if err != nil || batch == nil || batch.ids[0] != "fresh" {
		t.Fatalf("fresh result not retained: batch=%v err=%v", batch, err)
	}
}

func TestSpoolStatsDoesNotBlockBehindWALMaintenance(t *testing.T) {
	t.Parallel()
	spool, err := Open(t.TempDir(), Options{MaxBytes: 1 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	spool.mu.Lock() // model a long compaction holding the queue mutation lock
	done := make(chan Stats, 1)
	go func() { done <- spool.Stats() }()
	select {
	case <-done:
	case <-time.After(100 * time.Millisecond):
		spool.mu.Unlock()
		t.Fatal("heartbeat stats blocked behind WAL maintenance")
	}
	spool.mu.Unlock()
}

func TestSpoolConcurrentProducers(t *testing.T) {
	t.Parallel()
	spool, err := Open(t.TempDir(), Options{MaxBytes: 8 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	const producers = 8
	const perProducer = 25
	start := make(chan struct{})
	errs := make(chan error, producers)
	var wg sync.WaitGroup
	for producer := 0; producer < producers; producer++ {
		producer := producer
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			for result := 0; result < perProducer; result++ {
				id := fmt.Sprintf("p%d-r%d", producer, result)
				retained, err := spool.Enqueue(Item{
					ID: id, Path: "/results/ping", Payload: json.RawMessage(`{"value":1}`),
				})
				if err != nil {
					errs <- err
					return
				}
				if !retained {
					errs <- fmt.Errorf("unique item %s was not retained", id)
					return
				}
			}
		}()
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if stats := spool.Stats(); stats.Depth != producers*perProducer || stats.Dropped != 0 {
		t.Fatalf("concurrent enqueue stats=%+v", stats)
	}

	recovered, err := Open(spool.dir, Options{MaxBytes: 8 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if stats := recovered.Stats(); stats.Depth != producers*perProducer || stats.Dropped != 0 {
		t.Fatalf("recovered concurrent enqueue stats=%+v", stats)
	}
}
