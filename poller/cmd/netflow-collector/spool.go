package main

import (
	"bytes"
	"encoding/gob"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// spoolMagic prefixes every spool segment so a truncated or foreign file is
// recognised instead of being decoded into garbage records.
const spoolMagic = "ZPNFSPL1"

const (
	spoolSuffix    = ".nfb"
	spoolTmpPrefix = ".tmp-"
	spoolBadSuffix = ".bad"
)

// spoolPayload is one batch on disk. The deduplication token travels with the
// records, so a batch replayed after a restart is recognised by ClickHouse if
// its first insert had in fact committed.
type spoolPayload struct {
	Token   string
	Created time.Time
	Records []netflow.Record
}

type spoolFile struct {
	name    string
	records int
	bytes   int64
}

// diskSpool is the optional on-disk overflow of the delivery queue
// (NETFLOW_SPOOL_DIR). One file per batch, named so that lexical order is
// creation order: the queue drains it oldest first. It is bounded by maxBytes;
// when full, the oldest segment is dropped and counted, never the newest data.
type diskSpool struct {
	dir      string
	maxBytes int64

	mu       sync.Mutex
	files    []spoolFile // oldest first
	bytes    int64
	records  int
	inflight string // segment the writer is delivering; never evicted
}

// openDiskSpool prepares dir and indexes the segments a previous run left
// behind (replayed before anything newer). Half-written temp files from a
// crash are removed.
func openDiskSpool(dir string, maxBytes int64) (*diskSpool, error) {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	s := &diskSpool{dir: dir, maxBytes: maxBytes}
	for _, e := range entries {
		name := e.Name()
		switch {
		case strings.HasPrefix(name, spoolTmpPrefix):
			_ = os.Remove(filepath.Join(dir, name))
		case strings.HasSuffix(name, spoolSuffix):
			info, err := e.Info()
			if err != nil {
				continue
			}
			s.files = append(s.files, spoolFile{name: name, records: segmentRecordCount(name), bytes: info.Size()})
			s.bytes += info.Size()
			s.records += segmentRecordCount(name)
		}
	}
	sort.Slice(s.files, func(i, j int) bool { return s.files[i].name < s.files[j].name })
	return s, nil
}

// segmentName orders by creation time then batch sequence and carries the
// record count, so eviction can account dropped records without decoding. The
// token hash keeps the parts of a bisected batch apart.
func segmentName(b *flowBatch) string {
	h := fnv.New32a()
	h.Write([]byte(b.Token))
	return fmt.Sprintf("%020d-%012d-%08x-%d%s", b.Created.UnixNano(), b.Seq, h.Sum32(), len(b.Records), spoolSuffix)
}

func segmentRecordCount(name string) int {
	base := strings.TrimSuffix(name, spoolSuffix)
	i := strings.LastIndexByte(base, '-')
	if i < 0 {
		return 0
	}
	n, err := strconv.Atoi(base[i+1:])
	if err != nil {
		return 0
	}
	return n
}

// encodeSegment serialises a batch; done outside any queue lock.
func encodeSegment(b *flowBatch) ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteString(spoolMagic)
	if err := gob.NewEncoder(&buf).Encode(spoolPayload{Token: b.Token, Created: b.Created, Records: b.Records}); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// writeTemp writes an encoded segment to a temp file and syncs it. commit()
// makes it visible; until then a crash leaves only a temp file that the next
// start removes.
func (s *diskSpool) writeTemp(b *flowBatch, data []byte) (string, error) {
	tmp := filepath.Join(s.dir, spoolTmpPrefix+segmentName(b))
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o640)
	if err != nil {
		return "", err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(tmp)
		return "", err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmp)
		return "", err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return "", err
	}
	return tmp, nil
}

// commit renames a temp segment into the spool, evicting the oldest segments
// if the spool would exceed maxBytes. It returns the number of records in the
// evicted segments; errSpoolFull means the batch itself did not fit.
func (s *diskSpool) commit(tmp string, b *flowBatch) (evicted int, err error) {
	name := segmentName(b)
	info, err := os.Stat(tmp)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	evicted = s.evictLocked(info.Size())
	if s.bytes+info.Size() > s.maxBytes {
		// Nothing left to evict (only the in-flight segment remains): the new
		// batch itself cannot be kept; the caller accounts for its records.
		os.Remove(tmp)
		return evicted, errSpoolFull
	}
	if err := os.Rename(tmp, filepath.Join(s.dir, name)); err != nil {
		os.Remove(tmp)
		return evicted, err
	}
	s.files = append(s.files, spoolFile{name: name, records: len(b.Records), bytes: info.Size()})
	// A batch that was in flight when the collector stopped is older than
	// segments spilled while it was being retried: keep the order by name.
	sort.Slice(s.files, func(i, j int) bool { return s.files[i].name < s.files[j].name })
	s.bytes += info.Size()
	s.records += len(b.Records)
	return evicted, nil
}

var errSpoolFull = errors.New("netflow spool full")

// evictLocked drops the oldest segments (except the in-flight one) until
// `need` more bytes fit. It returns the number of records dropped.
func (s *diskSpool) evictLocked(need int64) int {
	dropped := 0
	for s.bytes+need > s.maxBytes {
		idx := -1
		for i, f := range s.files {
			if f.name != s.inflight {
				idx = i
				break
			}
		}
		if idx < 0 {
			return dropped
		}
		f := s.files[idx]
		os.Remove(filepath.Join(s.dir, f.name))
		s.files = append(s.files[:idx], s.files[idx+1:]...)
		s.bytes -= f.bytes
		s.records -= f.records
		dropped += f.records
	}
	return dropped
}

// write persists a batch synchronously (shutdown path and spill).
func (s *diskSpool) write(b *flowBatch) (evicted int, err error) {
	data, err := encodeSegment(b)
	if err != nil {
		return 0, err
	}
	tmp, err := s.writeTemp(b, data)
	if err != nil {
		return 0, err
	}
	return s.commit(tmp, b)
}

// peek loads the oldest segment and marks it in flight. A segment that cannot
// be decoded is renamed *.bad (kept for inspection) and reported through
// corrupt so its records are counted as lost; peek then moves on.
func (s *diskSpool) peek() (b *flowBatch, corrupt int, err error) {
	for {
		s.mu.Lock()
		if len(s.files) == 0 {
			s.mu.Unlock()
			return nil, corrupt, nil
		}
		f := s.files[0]
		s.inflight = f.name
		s.mu.Unlock()

		payload, rerr := readSegment(filepath.Join(s.dir, f.name))
		if rerr == nil {
			return &flowBatch{Token: payload.Token, Created: payload.Created, Records: payload.Records, spoolName: f.name}, corrupt, nil
		}
		if errors.Is(rerr, os.ErrNotExist) {
			s.drop(f.name)
			continue
		}
		fmt.Printf("ERROR: netflow spool segment %s is unreadable (%v); moved aside as %s%s, %d records lost\n", f.name, rerr, f.name, spoolBadSuffix, f.records)
		_ = os.Rename(filepath.Join(s.dir, f.name), filepath.Join(s.dir, f.name+spoolBadSuffix))
		s.drop(f.name)
		corrupt += f.records
	}
}

func readSegment(path string) (spoolPayload, error) {
	var p spoolPayload
	f, err := os.Open(path)
	if err != nil {
		return p, err
	}
	defer f.Close()
	magic := make([]byte, len(spoolMagic))
	if _, err := io.ReadFull(f, magic); err != nil || string(magic) != spoolMagic {
		return p, fmt.Errorf("bad spool header")
	}
	if err := gob.NewDecoder(f).Decode(&p); err != nil {
		return p, err
	}
	return p, nil
}

// drop removes a delivered (or unreadable) segment from the index and disk.
func (s *diskSpool) drop(name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.inflight == name {
		s.inflight = ""
	}
	for i, f := range s.files {
		if f.name == name {
			os.Remove(filepath.Join(s.dir, name))
			s.files = append(s.files[:i], s.files[i+1:]...)
			s.bytes -= f.bytes
			s.records -= f.records
			return
		}
	}
}

// release clears the in-flight mark without deleting (delivery abandoned at
// shutdown: the segment stays for the next start).
func (s *diskSpool) release(name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.inflight == name {
		s.inflight = ""
	}
}

func (s *diskSpool) usage() (records int, bytes int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.records, s.bytes
}

func (s *diskSpool) empty() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.files) == 0
}
