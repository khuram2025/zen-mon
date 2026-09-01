// Package sensorspool provides the remote sensor's durable result queue.
//
// Probe payloads live in immutable, checksummed data segments. Only compact
// record metadata is retained in memory; payload bytes are read on demand for
// the one batch being uploaded. A separate checksummed control WAL records
// acknowledgements, permanent drops, and the stable in-flight manifest. Old
// data segments are deleted whole, so reclaiming a large queue never requires
// a second full-size copy of it.
package sensorspool

import (
	"bytes"
	"container/heap"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
)

const (
	DefaultMaxBytes = int64(512 * 1024 * 1024)
	DefaultMaxAge   = 72 * time.Hour

	controlName         = "control.wal"
	segmentPrefix       = "segment-"
	segmentSuffix       = ".wal"
	walVersion          = 1
	frameHeader         = 8 // uint32 payload length + uint32 CRC32
	maxFrameBytes       = 64 * 1024 * 1024
	defaultSegmentBytes = int64(4 * 1024 * 1024)
	controlCompactBytes = int64(1024 * 1024)
	controlReserveBytes = int64(1024 * 1024)
	maxAgeDropChunk     = 4096
	opSnapshot          = "snapshot"
	opAck               = "ack"
	opDrop              = "drop"
	opInflight          = "inflight"
)

// Options controls the on-disk queue bounds. Now is intended for deterministic
// tests; production callers should leave it nil.
type Options struct {
	MaxBytes int64
	MaxAge   time.Duration
	Now      func() time.Time
}

// Item is one independently durable result. ID may be supplied by a caller
// that needs enqueue deduplication. If empty, Enqueue assigns a UUID.
type Item struct {
	ID        string          `json:"id"`
	Path      string          `json:"path"`
	CreatedAt time.Time       `json:"created_at"`
	Payload   json.RawMessage `json:"payload"`
	Sequence  uint64          `json:"sequence"`
}

type dataRecord struct {
	Version int  `json:"v"`
	Item    Item `json:"item"`
}

type recordRef struct {
	Segment string `json:"s"`
	Index   uint32 `json:"i"`
}

type inflightRecord struct {
	Key  string      `json:"key"`
	Path string      `json:"path"`
	Refs []recordRef `json:"refs"`
	IDs  []string    `json:"ids"`
}

type segmentSnapshot struct {
	ID   string `json:"id"`
	Dead []byte `json:"dead"`
}

type controlEvent struct {
	Version  int               `json:"v"`
	Op       string            `json:"op"`
	Key      string            `json:"key,omitempty"`
	Refs     []recordRef       `json:"refs,omitempty"`
	Dropped  uint64            `json:"dropped,omitempty"`
	Sequence uint64            `json:"sequence,omitempty"`
	Inflight *inflightRecord   `json:"inflight,omitempty"`
	Segments []segmentSnapshot `json:"segments,omitempty"`
}

type segment struct {
	id      string
	path    string
	size    int64
	entries []*entry
	live    int
}

// entry intentionally contains no payload bytes. The offset identifies the
// checksummed frame to read only when the uploader requests a batch.
type entry struct {
	id        string
	path      string
	createdAt time.Time
	sequence  uint64
	segment   *segment
	index     uint32
	offset    int64
	frameSize int64
	dead      bool
}

type expiryQueue []*entry

func (q expiryQueue) Len() int { return len(q) }
func (q expiryQueue) Less(i, j int) bool {
	if q[i].createdAt.Equal(q[j].createdAt) {
		if q[i].sequence == q[j].sequence {
			return q[i].id < q[j].id
		}
		return q[i].sequence < q[j].sequence
	}
	return q[i].createdAt.Before(q[j].createdAt)
}
func (q expiryQueue) Swap(i, j int)   { q[i], q[j] = q[j], q[i] }
func (q *expiryQueue) Push(value any) { *q = append(*q, value.(*entry)) }
func (q *expiryQueue) Pop() any {
	old := *q
	n := len(old)
	value := old[n-1]
	old[n-1] = nil
	*q = old[:n-1]
	return value
}

// Batch contains one immutable upload attempt. Once returned, its manifest is
// also in the control WAL, so later enqueues and restarts cannot alter its key
// or body.
type Batch struct {
	Key   string
	Path  string
	Items []json.RawMessage

	ids  []string
	refs []recordRef
}

// Stats is a point-in-time view of the queue. Bytes is the logical size of the
// control WAL plus every data segment, not just JSON payload bytes. Dropped is
// cumulative and survives restart.
type Stats struct {
	Depth   int
	Bytes   int64
	Dropped uint64
}

// Spool is safe for concurrent producers, heartbeat reads, and one uploader.
type Spool struct {
	mu          sync.Mutex
	dir         string
	controlPath string
	maxBytes    int64
	maxAge      time.Duration
	now         func() time.Time

	segmentTarget int64
	segments      map[string]*segment
	active        *segment
	entries       []*entry
	head          int
	byID          map[string]*entry
	expiry        expiryQueue
	liveDepth     int
	paths         map[string]string

	controlBytes int64
	diskBytes    int64
	dropped      uint64
	sequence     uint64
	inflight     *inflightRecord

	stats atomic.Pointer[Stats]
}

// Open creates or recovers the segmented queue. A torn final frame is safely
// truncated. A checksum or semantic failure in a complete frame is reported
// instead of silently discarding acknowledged telemetry.
func Open(dir string, opts Options) (*Spool, error) {
	if strings.TrimSpace(dir) == "" {
		return nil, errors.New("spool directory is required")
	}
	if opts.MaxBytes <= 0 {
		opts.MaxBytes = DefaultMaxBytes
	}
	if opts.MaxAge <= 0 {
		opts.MaxAge = DefaultMaxAge
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if err := os.MkdirAll(dir, 0750); err != nil {
		return nil, fmt.Errorf("create spool directory: %w", err)
	}
	if err := cleanupTemps(dir); err != nil {
		return nil, err
	}

	s := &Spool{
		dir: dir, controlPath: filepath.Join(dir, controlName),
		maxBytes: opts.MaxBytes, maxAge: opts.MaxAge, now: opts.Now,
		segmentTarget: chooseSegmentTarget(opts.MaxBytes),
		segments:      make(map[string]*segment),
		byID:          make(map[string]*entry),
		paths:         make(map[string]string),
	}
	if err := s.loadSegmentsLocked(); err != nil {
		return nil, err
	}
	if err := s.replayControlLocked(); err != nil {
		return nil, err
	}
	if err := s.rebuildLiveIndexesLocked(); err != nil {
		return nil, err
	}
	if err := s.validateInflightLocked(); err != nil {
		return nil, err
	}
	if _, err := s.reclaimEmptySegmentsLocked(); err != nil {
		return nil, err
	}
	s.selectActiveSegmentLocked()
	if s.inflight != nil && s.active != nil && s.inflightSegmentLocked() == s.active {
		s.active = nil
	}
	if err := s.enforceBoundsLocked(s.now().UTC()); err != nil {
		return nil, err
	}
	if err := s.maybeCompactControlLocked(false); err != nil {
		return nil, err
	}
	s.publishStatsLocked()
	return s, nil
}

// Enqueue fsyncs a data frame before returning. A supplied ID already in the
// live queue is a successful deduplication no-op and returns retained=false.
func (s *Spool) Enqueue(item Item) (retained bool, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	defer s.publishStatsLocked()

	if item.ID == "" {
		item.ID = uuid.NewString()
	}
	if _, exists := s.byID[item.ID]; exists {
		return false, nil
	}
	if strings.TrimSpace(item.Path) == "" || !strings.HasPrefix(item.Path, "/") {
		return false, errors.New("spool item path must be absolute")
	}
	if len(item.Payload) == 0 || !json.Valid(item.Payload) {
		return false, errors.New("spool item payload must be valid JSON")
	}
	if item.CreatedAt.IsZero() {
		item.CreatedAt = s.now().UTC()
	} else {
		item.CreatedAt = item.CreatedAt.UTC()
	}
	s.sequence++
	item.Sequence = s.sequence

	now := s.now().UTC()
	frame, err := encodeFrame(dataRecord{Version: walVersion, Item: item})
	if err != nil {
		return false, err
	}
	if item.CreatedAt.Before(now.Add(-s.maxAge)) {
		if err := s.recordRejectedLocked(item.ID); err != nil {
			return false, err
		}
		return false, nil
	}
	if err := s.enforceAgeLocked(now); err != nil {
		return false, err
	}
	if int64(len(frame)) > s.dataBudgetLocked() {
		if err := s.recordRejectedLocked(item.ID); err != nil {
			return false, err
		}
		return false, nil
	}
	fits, err := s.ensureCapacityLocked(int64(len(frame)))
	if err != nil {
		return false, err
	}
	if !fits {
		if err := s.recordRejectedLocked(item.ID); err != nil {
			return false, err
		}
		return false, nil
	}

	seg, _, err := s.appendSegmentFrameLocked(frame)
	if err != nil {
		return false, fmt.Errorf("persist spool item: %w", err)
	}
	index := uint32(len(seg.entries))
	e := &entry{
		id: item.ID, path: s.internPathLocked(item.Path), createdAt: item.CreatedAt,
		sequence: item.Sequence, segment: seg, index: index,
		offset: seg.size - int64(len(frame)), frameSize: int64(len(frame)),
	}
	seg.entries = append(seg.entries, e)
	seg.live++
	s.entries = append(s.entries, e)
	s.byID[e.id] = e
	heap.Push(&s.expiry, e)
	s.liveDepth++
	if seg.size >= s.segmentTarget {
		s.active = nil
	} else {
		s.active = seg
	}
	return true, nil
}

// NextBatch returns, without removing, the oldest queued endpoint. A batch is
// bounded to one data segment, which lets old segments be reclaimed without
// copying live payloads. Its persisted manifest freezes membership and key.
func (s *Spool) NextBatch(limit int) (*Batch, error) {
	if limit <= 0 {
		return nil, errors.New("batch limit must be positive")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	defer s.publishStatsLocked()

	if err := s.enforceBoundsLocked(s.now().UTC()); err != nil {
		return nil, err
	}
	if s.inflight != nil {
		return s.materializeInflightLocked()
	}
	oldest := s.oldestLiveLocked()
	if oldest == nil {
		return nil, nil
	}

	manifest := &inflightRecord{Path: oldest.path}
	for i := int(oldest.index); i < len(oldest.segment.entries); i++ {
		e := oldest.segment.entries[i]
		if e.dead || e.path != manifest.Path {
			continue
		}
		manifest.Refs = append(manifest.Refs, e.ref())
		manifest.IDs = append(manifest.IDs, e.id)
		if len(manifest.Refs) == limit {
			break
		}
	}
	manifest.Key = batchKey(manifest.IDs)
	if _, err := s.appendControlLocked(controlEvent{
		Version: walVersion, Op: opInflight, Sequence: s.sequence, Inflight: manifest,
	}); err != nil {
		return nil, fmt.Errorf("persist in-flight batch: %w", err)
	}
	s.inflight = cloneInflight(manifest)
	if s.active == oldest.segment {
		s.active = nil
	}
	if err := s.maybeCompactControlLocked(false); err != nil {
		return nil, err
	}
	return s.materializeInflightLocked()
}

// Ack durably records controller acknowledgement, then removes exactly the
// frozen batch from the live queue.
func (s *Spool) Ack(batch *Batch) error {
	if batch == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	defer s.publishStatsLocked()
	if err := s.matchInflightLocked(batch); err != nil {
		return err
	}
	refs := append([]recordRef(nil), s.inflight.Refs...)
	if err := s.validateLiveRefsLocked(refs); err != nil {
		return err
	}
	if _, err := s.appendControlLocked(controlEvent{
		Version: walVersion, Op: opAck, Key: s.inflight.Key,
		Refs: refs, Sequence: s.sequence,
	}); err != nil {
		return fmt.Errorf("persist spool acknowledgement: %w", err)
	}
	if _, err := s.markDeadLocked(refs, true); err != nil {
		return err
	}
	s.inflight = nil
	deleted, err := s.reclaimEmptySegmentsLocked()
	if err != nil {
		return err
	}
	return s.maybeCompactControlLocked(deleted)
}

// SplitInflight replaces an explicitly rejected multi-item request with its
// oldest half. Records outside the replacement remain queued for later.
func (s *Spool) SplitInflight(batch *Batch) error {
	if batch == nil {
		return errors.New("cannot split a nil spool batch")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	defer s.publishStatsLocked()
	if err := s.matchInflightLocked(batch); err != nil {
		return err
	}
	if len(s.inflight.Refs) <= 1 {
		return errors.New("in-flight spool batch cannot be split further")
	}
	keep := (len(s.inflight.Refs) + 1) / 2
	next := &inflightRecord{
		Path: s.inflight.Path,
		Refs: append([]recordRef(nil), s.inflight.Refs[:keep]...),
		IDs:  append([]string(nil), s.inflight.IDs[:keep]...),
	}
	next.Key = batchKey(next.IDs)
	if _, err := s.appendControlLocked(controlEvent{
		Version: walVersion, Op: opInflight, Sequence: s.sequence, Inflight: next,
	}); err != nil {
		return fmt.Errorf("persist split in-flight batch: %w", err)
	}
	s.inflight = next
	return s.maybeCompactControlLocked(false)
}

// DropInflight durably removes a controller-rejected single result and counts
// it. Retryable transport, authentication, rate-limit, and 5xx failures must
// leave the frozen batch intact instead.
func (s *Spool) DropInflight(batch *Batch) error {
	if batch == nil {
		return errors.New("cannot drop a nil spool batch")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	defer s.publishStatsLocked()
	if err := s.matchInflightLocked(batch); err != nil {
		return err
	}
	if len(s.inflight.Refs) != 1 {
		return fmt.Errorf("permanent drop requires one queued result, found %d", len(s.inflight.Refs))
	}
	refs := append([]recordRef(nil), s.inflight.Refs...)
	if err := s.validateLiveRefsLocked(refs); err != nil {
		return err
	}
	if _, err := s.appendControlLocked(controlEvent{
		Version: walVersion, Op: opDrop, Key: "poison:" + s.inflight.Key,
		Refs: refs, Dropped: 1, Sequence: s.sequence,
	}); err != nil {
		return fmt.Errorf("persist rejected spool result: %w", err)
	}
	if _, err := s.markDeadLocked(refs, true); err != nil {
		return err
	}
	s.inflight = nil
	s.dropped++
	deleted, err := s.reclaimEmptySegmentsLocked()
	if err != nil {
		return err
	}
	return s.maybeCompactControlLocked(deleted)
}

func (s *Spool) Stats() Stats {
	stats := s.stats.Load()
	if stats == nil {
		return Stats{}
	}
	return *stats
}

func (s *Spool) publishStatsLocked() {
	s.stats.Store(&Stats{Depth: s.liveDepth, Bytes: s.diskBytes, Dropped: s.dropped})
}

func (e *entry) ref() recordRef {
	return recordRef{Segment: e.segment.id, Index: e.index}
}

func (s *Spool) entryForRefLocked(ref recordRef) *entry {
	seg := s.segments[ref.Segment]
	if seg == nil || int(ref.Index) >= len(seg.entries) {
		return nil
	}
	return seg.entries[int(ref.Index)]
}

func (s *Spool) internPathLocked(path string) string {
	if interned, ok := s.paths[path]; ok {
		return interned
	}
	s.paths[path] = path
	return path
}

func chooseSegmentTarget(maxBytes int64) int64 {
	target := defaultSegmentBytes
	if candidate := maxBytes / 8; candidate > 0 && candidate < target {
		target = candidate
	}
	if target < 64*1024 {
		target = 64 * 1024
	}
	if target > maxBytes {
		target = maxBytes
	}
	return target
}

func (s *Spool) controlReserveLocked() int64 {
	reserve := s.maxBytes / 10
	if reserve > controlReserveBytes {
		reserve = controlReserveBytes
	}
	if reserve < 0 {
		return 0
	}
	return reserve
}

func (s *Spool) dataBudgetLocked() int64 {
	budget := s.maxBytes - s.controlReserveLocked()
	if budget <= 0 {
		return s.maxBytes
	}
	return budget
}

func (s *Spool) capacityLimitLocked() int64 {
	remainingReserve := s.controlReserveLocked() - s.controlBytes
	if remainingReserve < 0 {
		remainingReserve = 0
	}
	limit := s.maxBytes - remainingReserve
	if limit <= 0 {
		return s.maxBytes
	}
	return limit
}

func (s *Spool) loadSegmentsLocked() error {
	dirEntries, err := os.ReadDir(s.dir)
	if err != nil {
		return fmt.Errorf("scan sensor spool directory: %w", err)
	}
	removedEmpty := false
	for _, file := range dirEntries {
		if file.IsDir() || !strings.HasPrefix(file.Name(), segmentPrefix) || !strings.HasSuffix(file.Name(), segmentSuffix) {
			continue
		}
		id := strings.TrimSuffix(strings.TrimPrefix(file.Name(), segmentPrefix), segmentSuffix)
		if _, err := uuid.Parse(id); err != nil {
			return fmt.Errorf("invalid sensor spool segment name %q", file.Name())
		}
		seg := &segment{id: id, path: filepath.Join(s.dir, file.Name())}
		if err := s.loadSegmentLocked(seg); err != nil {
			return err
		}
		if len(seg.entries) == 0 {
			if err := os.Remove(seg.path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return fmt.Errorf("remove empty sensor spool segment: %w", err)
			}
			removedEmpty = true
			continue
		}
		s.segments[id] = seg
		s.diskBytes += seg.size
	}
	sort.Slice(s.entries, func(i, j int) bool {
		if s.entries[i].sequence == s.entries[j].sequence {
			if s.entries[i].segment.id == s.entries[j].segment.id {
				return s.entries[i].index < s.entries[j].index
			}
			return s.entries[i].segment.id < s.entries[j].segment.id
		}
		return s.entries[i].sequence < s.entries[j].sequence
	})
	for i := 1; i < len(s.entries); i++ {
		if s.entries[i-1].sequence == s.entries[i].sequence {
			return fmt.Errorf("duplicate sensor spool sequence %d", s.entries[i].sequence)
		}
	}
	if removedEmpty {
		return syncDirectory(s.dir)
	}
	return nil
}

func (s *Spool) loadSegmentLocked(seg *segment) error {
	f, err := os.OpenFile(seg.path, os.O_RDWR, 0600)
	if err != nil {
		return fmt.Errorf("open sensor spool segment %s: %w", seg.id, err)
	}
	defer f.Close()
	if err := f.Chmod(0600); err != nil {
		return fmt.Errorf("secure sensor spool segment %s: %w", seg.id, err)
	}

	offset := int64(0)
	for {
		data, frameSize, done, err := readFrame(f, offset, "data segment")
		if err != nil {
			return fmt.Errorf("read sensor spool segment %s at %d: %w", seg.id, offset, err)
		}
		if done {
			break
		}
		var record dataRecord
		if err := json.Unmarshal(data, &record); err != nil {
			return fmt.Errorf("decode sensor spool segment %s at %d: %w", seg.id, offset, err)
		}
		item := record.Item
		if record.Version != walVersion || item.ID == "" || !strings.HasPrefix(item.Path, "/") ||
			item.Sequence == 0 || item.CreatedAt.IsZero() || len(item.Payload) == 0 || !json.Valid(item.Payload) {
			return fmt.Errorf("invalid sensor spool data record in segment %s at %d", seg.id, offset)
		}
		if len(seg.entries) > 0 && item.Sequence <= seg.entries[len(seg.entries)-1].sequence {
			return fmt.Errorf("non-monotonic sensor spool sequence in segment %s at %d", seg.id, offset)
		}
		index := uint32(len(seg.entries))
		e := &entry{
			id: item.ID, path: s.internPathLocked(item.Path), createdAt: item.CreatedAt.UTC(), sequence: item.Sequence,
			segment: seg, index: index, offset: offset, frameSize: frameSize,
		}
		seg.entries = append(seg.entries, e)
		s.entries = append(s.entries, e)
		if item.Sequence > s.sequence {
			s.sequence = item.Sequence
		}
		offset += frameSize
	}
	seg.size = offset
	return nil
}

func (s *Spool) replayControlLocked() error {
	f, err := os.OpenFile(s.controlPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return fmt.Errorf("open sensor spool control WAL: %w", err)
	}
	created := false
	if info, statErr := f.Stat(); statErr == nil {
		created = info.Size() == 0
	}
	if err := f.Chmod(0600); err != nil {
		f.Close()
		return fmt.Errorf("secure sensor spool control WAL: %w", err)
	}
	defer f.Close()

	countedDrops := make(map[string]struct{})
	offset := int64(0)
	for {
		data, frameSize, done, err := readFrame(f, offset, "control WAL")
		if err != nil {
			return fmt.Errorf("read sensor spool control WAL at %d: %w", offset, err)
		}
		if done {
			break
		}
		var event controlEvent
		if err := json.Unmarshal(data, &event); err != nil {
			return fmt.Errorf("decode sensor spool control WAL at %d: %w", offset, err)
		}
		if err := s.applyControlEventLocked(event, countedDrops); err != nil {
			return fmt.Errorf("apply sensor spool control WAL at %d: %w", offset, err)
		}
		offset += frameSize
	}
	s.controlBytes = offset
	s.diskBytes += offset
	if created {
		return syncDirectory(s.dir)
	}
	return nil
}

func readFrame(f *os.File, offset int64, label string) ([]byte, int64, bool, error) {
	var header [frameHeader]byte
	n, readErr := io.ReadFull(f, header[:])
	if errors.Is(readErr, io.EOF) && n == 0 {
		return nil, 0, true, nil
	}
	if errors.Is(readErr, io.ErrUnexpectedEOF) || (errors.Is(readErr, io.EOF) && n > 0) {
		if err := truncateTail(f, offset); err != nil {
			return nil, 0, false, err
		}
		return nil, 0, true, nil
	}
	if readErr != nil {
		return nil, 0, false, readErr
	}
	length := binary.BigEndian.Uint32(header[:4])
	wantCRC := binary.BigEndian.Uint32(header[4:])
	if length == 0 || length > maxFrameBytes {
		return nil, 0, false, fmt.Errorf("invalid %s frame length %d", label, length)
	}
	data := make([]byte, int(length))
	if _, readErr := io.ReadFull(f, data); readErr != nil {
		if errors.Is(readErr, io.EOF) || errors.Is(readErr, io.ErrUnexpectedEOF) {
			if err := truncateTail(f, offset); err != nil {
				return nil, 0, false, err
			}
			return nil, 0, true, nil
		}
		return nil, 0, false, readErr
	}
	if crc32.ChecksumIEEE(data) != wantCRC {
		return nil, 0, false, fmt.Errorf("%s checksum mismatch", label)
	}
	return data, int64(frameHeader) + int64(length), false, nil
}

func (s *Spool) applyControlEventLocked(event controlEvent, countedDrops map[string]struct{}) error {
	if event.Version != walVersion {
		return fmt.Errorf("unsupported control WAL version %d", event.Version)
	}
	if event.Sequence > s.sequence {
		s.sequence = event.Sequence
	}
	switch event.Op {
	case opSnapshot:
		for _, e := range s.entries {
			e.dead = false
		}
		s.dropped = event.Dropped
		s.inflight = cloneInflight(event.Inflight)
		clear(countedDrops)
		if _, err := s.markReplaySnapshotsLocked(event.Segments); err != nil {
			return err
		}
	case opAck:
		if _, err := s.markReplayDeadLocked(event.Refs); err != nil {
			return err
		}
		if s.inflight != nil && event.Key == s.inflight.Key {
			s.inflight = nil
		}
	case opDrop:
		if _, err := s.markReplayDeadLocked(event.Refs); err != nil {
			return err
		}
		if _, err := s.markReplaySnapshotsLocked(event.Segments); err != nil {
			return err
		}
		if event.Key == "" {
			s.dropped += event.Dropped
		} else if _, duplicate := countedDrops[event.Key]; !duplicate {
			s.dropped += event.Dropped
			countedDrops[event.Key] = struct{}{}
		}
		if s.inflight != nil && strings.TrimPrefix(event.Key, "poison:") == s.inflight.Key {
			s.inflight = nil
		}
	case opInflight:
		if event.Inflight == nil {
			return errors.New("missing in-flight manifest")
		}
		s.inflight = cloneInflight(event.Inflight)
	default:
		return fmt.Errorf("unknown control WAL operation %q", event.Op)
	}
	return nil
}

func (s *Spool) markReplayDeadLocked(refs []recordRef) (int, error) {
	marked := 0
	for _, ref := range refs {
		e := s.entryForRefLocked(ref)
		if e == nil { // the fully acknowledged segment may already be deleted
			continue
		}
		if !e.dead {
			e.dead = true
			marked++
		}
	}
	return marked, nil
}

func (s *Spool) markReplaySnapshotsLocked(states []segmentSnapshot) (int, error) {
	marked := 0
	for _, state := range states {
		seg := s.segments[state.ID]
		if seg == nil {
			continue
		}
		if len(state.Dead) > (len(seg.entries)+7)/8 {
			return marked, fmt.Errorf("dead bitmap for segment %s is too large", state.ID)
		}
		for i := range seg.entries {
			if i/8 >= len(state.Dead) || state.Dead[i/8]&(1<<uint(i%8)) == 0 {
				continue
			}
			if !seg.entries[i].dead {
				seg.entries[i].dead = true
				marked++
			}
		}
	}
	return marked, nil
}

func (s *Spool) rebuildLiveIndexesLocked() error {
	s.byID = make(map[string]*entry)
	s.liveDepth = 0
	s.expiry = nil
	heap.Init(&s.expiry)
	for _, seg := range s.segments {
		seg.live = 0
	}
	for _, e := range s.entries {
		if e.dead {
			continue
		}
		if _, duplicate := s.byID[e.id]; duplicate {
			return fmt.Errorf("duplicate live sensor result id %s", e.id)
		}
		s.byID[e.id] = e
		e.segment.live++
		s.liveDepth++
		heap.Push(&s.expiry, e)
	}
	s.trimHeadLocked()
	return nil
}

func (s *Spool) validateInflightLocked() error {
	if s.inflight == nil {
		return nil
	}
	if s.inflight.Key == "" || s.inflight.Path == "" || len(s.inflight.Refs) == 0 || len(s.inflight.Refs) != len(s.inflight.IDs) {
		return errors.New("in-flight spool batch is incomplete")
	}
	if s.inflight.Key != batchKey(s.inflight.IDs) {
		return errors.New("in-flight spool batch idempotency key is invalid")
	}
	var segmentID string
	seen := make(map[recordRef]struct{}, len(s.inflight.Refs))
	for i, ref := range s.inflight.Refs {
		if _, duplicate := seen[ref]; duplicate {
			return fmt.Errorf("in-flight spool batch contains duplicate record reference")
		}
		seen[ref] = struct{}{}
		e := s.entryForRefLocked(ref)
		if e == nil || e.dead {
			return fmt.Errorf("in-flight spool result %s is missing", s.inflight.IDs[i])
		}
		if e.id != s.inflight.IDs[i] || e.path != s.inflight.Path {
			return fmt.Errorf("in-flight spool result %s does not match its manifest", s.inflight.IDs[i])
		}
		if segmentID == "" {
			segmentID = ref.Segment
		} else if segmentID != ref.Segment {
			return errors.New("in-flight spool batch spans data segments")
		}
	}
	return nil
}

func (s *Spool) selectActiveSegmentLocked() {
	var selected *segment
	var lastSequence uint64
	for _, seg := range s.segments {
		if seg.live == 0 || seg.size >= s.segmentTarget || len(seg.entries) == 0 {
			continue
		}
		candidate := seg.entries[len(seg.entries)-1].sequence
		if selected == nil || candidate > lastSequence {
			selected = seg
			lastSequence = candidate
		}
	}
	s.active = selected
}

func (s *Spool) inflightSegmentLocked() *segment {
	if s.inflight == nil || len(s.inflight.Refs) == 0 {
		return nil
	}
	if e := s.entryForRefLocked(s.inflight.Refs[0]); e != nil {
		return e.segment
	}
	return nil
}

func (s *Spool) appendSegmentFrameLocked(frame []byte) (*segment, bool, error) {
	seg := s.active
	created := false
	if seg == nil || (seg.size > 0 && seg.size+int64(len(frame)) > s.segmentTarget) {
		id := uuid.NewString()
		seg = &segment{id: id, path: filepath.Join(s.dir, segmentPrefix+id+segmentSuffix)}
		s.segments[id] = seg
		created = true
	}
	flags := os.O_CREATE | os.O_WRONLY | os.O_APPEND
	if created {
		flags |= os.O_EXCL
	}
	f, err := os.OpenFile(seg.path, flags, 0600)
	if err != nil {
		if created {
			delete(s.segments, seg.id)
		}
		return nil, false, err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, false, err
	}
	start := info.Size()
	if start != seg.size {
		f.Close()
		return nil, false, fmt.Errorf("segment %s size changed from %d to %d", seg.id, seg.size, start)
	}
	n, writeErr := f.Write(frame)
	if writeErr == nil && n != len(frame) {
		writeErr = io.ErrShortWrite
	}
	if writeErr == nil {
		writeErr = f.Sync()
	}
	closeErr := f.Close()
	if writeErr != nil {
		if truncateErr := os.Truncate(seg.path, start); truncateErr != nil {
			return nil, false, errors.Join(writeErr, truncateErr)
		}
		if created && start == 0 {
			_ = os.Remove(seg.path)
			delete(s.segments, seg.id)
		}
		return nil, false, writeErr
	}
	if closeErr != nil {
		return nil, false, closeErr
	}
	if created {
		if err := syncDirectory(s.dir); err != nil {
			return nil, false, err
		}
	}
	seg.size = start + int64(len(frame))
	s.diskBytes += int64(len(frame))
	return seg, created, nil
}

func (s *Spool) appendControlLocked(event controlEvent) (int64, error) {
	frame, err := encodeFrame(event)
	if err != nil {
		return 0, err
	}
	if s.diskBytes+int64(len(frame)) > s.maxBytes && s.controlBytes > 0 {
		if err := s.compactControlLocked(); err != nil {
			return 0, err
		}
		frame, err = encodeFrame(event)
		if err != nil {
			return 0, err
		}
	}
	f, err := os.OpenFile(s.controlPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return 0, err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return 0, err
	}
	start := info.Size()
	if start != s.controlBytes {
		f.Close()
		return 0, fmt.Errorf("control WAL size changed from %d to %d", s.controlBytes, start)
	}
	n, writeErr := f.Write(frame)
	if writeErr == nil && n != len(frame) {
		writeErr = io.ErrShortWrite
	}
	if writeErr == nil {
		writeErr = f.Sync()
	}
	closeErr := f.Close()
	if writeErr != nil {
		if truncateErr := os.Truncate(s.controlPath, start); truncateErr != nil {
			return 0, errors.Join(writeErr, truncateErr)
		}
		return 0, writeErr
	}
	if closeErr != nil {
		return 0, closeErr
	}
	if start == 0 {
		if err := syncDirectory(s.dir); err != nil {
			return 0, err
		}
	}
	s.controlBytes = start + int64(len(frame))
	s.diskBytes += int64(len(frame))
	return int64(len(frame)), nil
}

func (s *Spool) enforceBoundsLocked(now time.Time) error {
	if err := s.enforceAgeLocked(now); err != nil {
		return err
	}
	fits, err := s.ensureCapacityLocked(0)
	if err != nil {
		return err
	}
	if !fits {
		// A persisted in-flight segment is deliberately protected. It can be
		// the only reason the configured cap remains exceeded.
		return nil
	}
	return nil
}

func (s *Spool) enforceAgeLocked(now time.Time) error {
	cutoff := now.Add(-s.maxAge)
	protected := s.protectedRefsLocked()
	held := make([]*entry, 0, len(protected))
	defer func() {
		for _, e := range held {
			if !e.dead {
				heap.Push(&s.expiry, e)
			}
		}
	}()

	for s.expiry.Len() > 0 {
		refs := make([]recordRef, 0, maxAgeDropChunk)
		popped := make([]*entry, 0, maxAgeDropChunk)
		for s.expiry.Len() > 0 && len(refs) < maxAgeDropChunk {
			e := s.expiry[0]
			if !e.createdAt.Before(cutoff) {
				break
			}
			heap.Pop(&s.expiry)
			if e.dead {
				continue
			}
			if _, keep := protected[e.ref()]; keep {
				held = append(held, e)
				continue
			}
			refs = append(refs, e.ref())
			popped = append(popped, e)
		}
		if len(refs) == 0 {
			if s.expiry.Len() == 0 || !s.expiry[0].createdAt.Before(cutoff) {
				break
			}
			continue
		}
		if err := s.dropRefsLocked(refs, "age"); err != nil {
			for _, e := range popped {
				if !e.dead {
					heap.Push(&s.expiry, e)
				}
			}
			return err
		}
	}
	return s.maybeCompactControlLocked(false)
}

func (s *Spool) ensureCapacityLocked(needed int64) (bool, error) {
	if s.diskBytes+needed <= s.capacityLimitLocked() {
		return true, nil
	}
	protected := s.protectedRefsLocked()
	protectedSegments := make(map[string]struct{}, len(protected))
	for ref := range protected {
		protectedSegments[ref.Segment] = struct{}{}
	}
	if !s.canFitAroundProtectedLocked(needed, protectedSegments) {
		// Rewriting only the compact metadata snapshot may make enough room.
		// If it does not, dropping other queued segments cannot make this item
		// coexist with the immutable protected segment, so preserve them.
		if err := s.compactControlLocked(); err != nil {
			return false, err
		}
		if !s.canFitAroundProtectedLocked(needed, protectedSegments) {
			return false, nil
		}
	}
	visited := make(map[string]struct{})
	for s.diskBytes+needed > s.capacityLimitLocked() {
		seg := s.oldestEvictableSegmentLocked(protectedSegments, visited)
		if seg == nil {
			break
		}
		visited[seg.id] = struct{}{}
		refs := make([]recordRef, 0, seg.live)
		for _, e := range seg.entries {
			if e.dead {
				continue
			}
			if _, keep := protected[e.ref()]; keep {
				continue
			}
			refs = append(refs, e.ref())
		}
		if len(refs) == 0 {
			continue
		}
		if err := s.dropRefsLocked(refs, "capacity"); err != nil {
			return false, err
		}
	}
	if s.diskBytes+needed <= s.capacityLimitLocked() {
		return true, nil
	}
	if err := s.compactControlLocked(); err != nil {
		return false, err
	}
	return s.diskBytes+needed <= s.capacityLimitLocked(), nil
}

func (s *Spool) canFitAroundProtectedLocked(needed int64, protectedSegments map[string]struct{}) bool {
	minimumBytes := s.controlBytes + needed
	for id := range protectedSegments {
		if seg := s.segments[id]; seg != nil {
			minimumBytes += seg.size
		}
	}
	return minimumBytes <= s.capacityLimitLocked()
}

func (s *Spool) oldestEvictableSegmentLocked(protectedSegments, visited map[string]struct{}) *segment {
	for i := s.head; i < len(s.entries); i++ {
		e := s.entries[i]
		if e.dead {
			continue
		}
		if _, seen := visited[e.segment.id]; seen {
			continue
		}
		if _, keep := protectedSegments[e.segment.id]; keep {
			continue
		}
		return e.segment
	}
	return nil
}

func (s *Spool) protectedRefsLocked() map[recordRef]struct{} {
	protected := make(map[recordRef]struct{})
	if s.inflight != nil {
		for _, ref := range s.inflight.Refs {
			protected[ref] = struct{}{}
		}
	}
	return protected
}

func (s *Spool) dropRefsLocked(refs []recordRef, reason string) error {
	if len(refs) == 0 {
		return nil
	}
	if err := s.validateLiveRefsLocked(refs); err != nil {
		return err
	}
	key := dropKey(reason, refs)
	if _, err := s.appendControlLocked(controlEvent{
		Version: walVersion, Op: opDrop, Key: key, Segments: s.deadSnapshotsForRefsLocked(refs),
		Dropped: uint64(len(refs)), Sequence: s.sequence,
	}); err != nil {
		return fmt.Errorf("persist spool eviction: %w", err)
	}
	if _, err := s.markDeadLocked(refs, true); err != nil {
		return err
	}
	s.dropped += uint64(len(refs))
	deleted, err := s.reclaimEmptySegmentsLocked()
	if err != nil {
		return err
	}
	return s.maybeCompactControlLocked(deleted)
}

func (s *Spool) deadSnapshotsForRefsLocked(refs []recordRef) []segmentSnapshot {
	bySegment := make(map[string][]byte)
	for _, ref := range refs {
		seg := s.segments[ref.Segment]
		if seg == nil {
			continue
		}
		bitmap := bySegment[ref.Segment]
		if bitmap == nil {
			bitmap = make([]byte, (len(seg.entries)+7)/8)
			bySegment[ref.Segment] = bitmap
		}
		if int(ref.Index) < len(seg.entries) {
			bitmap[int(ref.Index)/8] |= 1 << uint(int(ref.Index)%8)
		}
	}
	ids := make([]string, 0, len(bySegment))
	for id := range bySegment {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	states := make([]segmentSnapshot, 0, len(ids))
	for _, id := range ids {
		states = append(states, segmentSnapshot{ID: id, Dead: bySegment[id]})
	}
	return states
}

func (s *Spool) recordRejectedLocked(id string) error {
	key := fmt.Sprintf("reject:%s:%d", id, s.sequence)
	if _, err := s.appendControlLocked(controlEvent{
		Version: walVersion, Op: opDrop, Key: key, Dropped: 1, Sequence: s.sequence,
	}); err != nil {
		return fmt.Errorf("persist rejected spool result: %w", err)
	}
	s.dropped++
	return s.maybeCompactControlLocked(false)
}

func (s *Spool) validateLiveRefsLocked(refs []recordRef) error {
	seen := make(map[recordRef]struct{}, len(refs))
	for _, ref := range refs {
		if _, duplicate := seen[ref]; duplicate {
			return errors.New("duplicate sensor spool record reference")
		}
		seen[ref] = struct{}{}
		e := s.entryForRefLocked(ref)
		if e == nil || e.dead {
			return fmt.Errorf("sensor spool record %s/%d is not live", ref.Segment, ref.Index)
		}
	}
	return nil
}

func (s *Spool) markDeadLocked(refs []recordRef, strict bool) (map[string]*segment, error) {
	touched := make(map[string]*segment)
	for _, ref := range refs {
		e := s.entryForRefLocked(ref)
		if e == nil || e.dead {
			if strict {
				return nil, fmt.Errorf("sensor spool record %s/%d is not live", ref.Segment, ref.Index)
			}
			continue
		}
		e.dead = true
		if s.byID[e.id] == e {
			delete(s.byID, e.id)
		}
		e.segment.live--
		s.liveDepth--
		touched[e.segment.id] = e.segment
	}
	s.trimHeadLocked()
	return touched, nil
}

func (s *Spool) reclaimEmptySegmentsLocked() (bool, error) {
	deleted := false
	for id, seg := range s.segments {
		if seg.live != 0 {
			continue
		}
		if err := os.Remove(seg.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return deleted, fmt.Errorf("remove consumed sensor spool segment %s: %w", id, err)
		}
		delete(s.segments, id)
		if s.active == seg {
			s.active = nil
		}
		s.diskBytes -= seg.size
		deleted = true
	}
	if deleted {
		if err := syncDirectory(s.dir); err != nil {
			return true, err
		}
		s.trimHeadLocked()
	}
	return deleted, nil
}

func (s *Spool) trimHeadLocked() {
	for s.head < len(s.entries) && s.entries[s.head].dead {
		s.head++
	}
	if s.head > 4096 && s.head*2 >= len(s.entries) {
		remaining := append([]*entry(nil), s.entries[s.head:]...)
		s.entries = remaining
		s.head = 0
	}
}

func (s *Spool) oldestLiveLocked() *entry {
	s.trimHeadLocked()
	for i := s.head; i < len(s.entries); i++ {
		if !s.entries[i].dead {
			return s.entries[i]
		}
	}
	return nil
}

func (s *Spool) materializeInflightLocked() (*Batch, error) {
	if s.inflight == nil {
		return nil, nil
	}
	b := &Batch{
		Key: s.inflight.Key, Path: s.inflight.Path,
		ids:  append([]string(nil), s.inflight.IDs...),
		refs: append([]recordRef(nil), s.inflight.Refs...),
	}
	files := make(map[string]*os.File)
	defer func() {
		for _, f := range files {
			_ = f.Close()
		}
	}()
	for i, ref := range s.inflight.Refs {
		e := s.entryForRefLocked(ref)
		if e == nil || e.dead || e.id != s.inflight.IDs[i] {
			return nil, fmt.Errorf("in-flight sensor spool record %s is missing", s.inflight.IDs[i])
		}
		f := files[ref.Segment]
		if f == nil {
			var err error
			f, err = os.Open(e.segment.path)
			if err != nil {
				return nil, err
			}
			files[ref.Segment] = f
		}
		payload, err := readPayloadAt(f, e)
		if err != nil {
			return nil, err
		}
		b.Items = append(b.Items, payload)
	}
	return b, nil
}

func readPayloadAt(f *os.File, e *entry) (json.RawMessage, error) {
	var header [frameHeader]byte
	if _, err := f.ReadAt(header[:], e.offset); err != nil {
		return nil, fmt.Errorf("read sensor spool frame header: %w", err)
	}
	length := binary.BigEndian.Uint32(header[:4])
	wantCRC := binary.BigEndian.Uint32(header[4:])
	if length == 0 || length > maxFrameBytes || int64(frameHeader)+int64(length) != e.frameSize {
		return nil, errors.New("sensor spool frame metadata changed")
	}
	data := make([]byte, int(length))
	if _, err := f.ReadAt(data, e.offset+frameHeader); err != nil {
		return nil, fmt.Errorf("read sensor spool frame: %w", err)
	}
	if crc32.ChecksumIEEE(data) != wantCRC {
		return nil, errors.New("sensor spool data checksum mismatch")
	}
	var record dataRecord
	if err := json.Unmarshal(data, &record); err != nil {
		return nil, err
	}
	if record.Version != walVersion || record.Item.ID != e.id || record.Item.Sequence != e.sequence || !json.Valid(record.Item.Payload) {
		return nil, errors.New("sensor spool data record does not match its index")
	}
	return append(json.RawMessage(nil), record.Item.Payload...), nil
}

func (s *Spool) matchInflightLocked(batch *Batch) error {
	if s.inflight == nil || batch.Key != s.inflight.Key || batch.Path != s.inflight.Path {
		return errors.New("operation does not match the in-flight spool batch")
	}
	if len(batch.ids) > 0 {
		if len(batch.ids) != len(s.inflight.IDs) {
			return errors.New("operation does not match the in-flight spool membership")
		}
		for i := range batch.ids {
			if batch.ids[i] != s.inflight.IDs[i] {
				return errors.New("operation does not match the in-flight spool membership")
			}
		}
	}
	return nil
}

func (s *Spool) maybeCompactControlLocked(force bool) error {
	if !force && s.controlBytes < controlCompactBytes {
		return nil
	}
	frame, err := s.controlSnapshotFrameLocked()
	if err != nil {
		return err
	}
	if !force && int64(len(frame))*2 >= s.controlBytes {
		return nil
	}
	return s.writeControlSnapshotLocked(frame)
}

func (s *Spool) compactControlLocked() error {
	frame, err := s.controlSnapshotFrameLocked()
	if err != nil {
		return err
	}
	return s.writeControlSnapshotLocked(frame)
}

func (s *Spool) controlSnapshotFrameLocked() ([]byte, error) {
	event := controlEvent{
		Version: walVersion, Op: opSnapshot, Dropped: s.dropped,
		Sequence: s.sequence, Inflight: cloneInflight(s.inflight),
	}
	ids := make([]string, 0, len(s.segments))
	for id := range s.segments {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		seg := s.segments[id]
		bitmap := make([]byte, (len(seg.entries)+7)/8)
		anyDead := false
		for i, e := range seg.entries {
			if e.dead {
				bitmap[i/8] |= 1 << uint(i%8)
				anyDead = true
			}
		}
		if anyDead {
			event.Segments = append(event.Segments, segmentSnapshot{ID: id, Dead: bitmap})
		}
	}
	return encodeFrame(event)
}

func (s *Spool) writeControlSnapshotLocked(frame []byte) error {
	tmp, err := os.CreateTemp(s.dir, ".control-*.tmp")
	if err != nil {
		return fmt.Errorf("create sensor spool control snapshot: %w", err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if err := tmp.Chmod(0600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(frame); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := replaceFile(tmpPath, s.controlPath); err != nil {
		return fmt.Errorf("install sensor spool control snapshot: %w", err)
	}
	if err := syncDirectory(s.dir); err != nil {
		return err
	}
	s.diskBytes = s.diskBytes - s.controlBytes + int64(len(frame))
	s.controlBytes = int64(len(frame))
	return nil
}

func encodeFrame(value any) ([]byte, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("marshal sensor spool WAL frame: %w", err)
	}
	if len(data) == 0 || len(data) > maxFrameBytes {
		return nil, fmt.Errorf("sensor spool WAL frame is %d bytes", len(data))
	}
	frame := make([]byte, frameHeader+len(data))
	binary.BigEndian.PutUint32(frame[:4], uint32(len(data)))
	binary.BigEndian.PutUint32(frame[4:8], crc32.ChecksumIEEE(data))
	copy(frame[frameHeader:], data)
	return frame, nil
}

func truncateTail(f *os.File, offset int64) error {
	if err := f.Truncate(offset); err != nil {
		return fmt.Errorf("truncate torn sensor spool WAL: %w", err)
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return err
	}
	return f.Sync()
}

func replaceFile(source, destination string) error {
	if err := os.Rename(source, destination); err != nil {
		if runtime.GOOS != "windows" {
			return err
		}
		if removeErr := os.Remove(destination); removeErr != nil && !errors.Is(removeErr, os.ErrNotExist) {
			return err
		}
		return os.Rename(source, destination)
	}
	return nil
}

func syncDirectory(path string) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func cleanupTemps(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("scan sensor spool directory: %w", err)
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), ".control-") || !strings.HasSuffix(entry.Name(), ".tmp") {
			continue
		}
		if err := os.Remove(filepath.Join(dir, entry.Name())); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("remove incomplete sensor spool control snapshot %s: %w", entry.Name(), err)
		}
	}
	return nil
}

func cloneInflight(in *inflightRecord) *inflightRecord {
	if in == nil {
		return nil
	}
	return &inflightRecord{
		Key: in.Key, Path: in.Path,
		Refs: append([]recordRef(nil), in.Refs...),
		IDs:  append([]string(nil), in.IDs...),
	}
}

func batchKey(ids []string) string {
	var data bytes.Buffer
	for _, id := range ids {
		data.WriteString(id)
		data.WriteByte(0)
	}
	return uuid.NewSHA1(uuid.NameSpaceOID, data.Bytes()).String()
}

func dropKey(reason string, refs []recordRef) string {
	var data bytes.Buffer
	data.WriteString(reason)
	data.WriteByte(0)
	for _, ref := range refs {
		data.WriteString(ref.Segment)
		data.WriteByte(0)
		var index [4]byte
		binary.BigEndian.PutUint32(index[:], ref.Index)
		data.Write(index[:])
	}
	return reason + ":" + uuid.NewSHA1(uuid.NameSpaceOID, data.Bytes()).String()
}
