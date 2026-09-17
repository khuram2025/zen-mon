package spool

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	bucketBatches    = []byte("batches")
	bucketMeta       = []byte("meta")
	bucketQuarantine = []byte("quarantine")
	keySequence      = []byte("sequence")
)

// Quarantine is deliberately much smaller than the active spool. It preserves
// enough terminal failures for diagnostics without allowing bad/colliding
// batch IDs to become an unbounded second telemetry queue.
const (
	QuarantineMaxRecords = 128
	QuarantineMaxBytes   = int64(32 * 1024 * 1024)
	QuarantineMaxAge     = 7 * 24 * time.Hour
)

var (
	ErrRecordNotFound        = errors.New("spool record not found")
	ErrStaleReplayGeneration = errors.New("stale spool replay generation")
	errDueNow                = errors.New("spool work is due now")
)

type Store struct {
	db                 *bolt.DB
	replayMu           sync.Mutex
	replayGenerationMu sync.RWMutex
	replayGeneration   uint64
}

type Record struct {
	Key           uint64          `json:"key"`
	BatchID       string          `json:"batch_id"`
	CreatedAt     time.Time       `json:"created_at"`
	Size          int64           `json:"size"`
	Payload       json.RawMessage `json:"payload,omitempty"`
	OriginalSize  int64           `json:"original_size,omitempty"`
	PayloadSHA256 string          `json:"payload_sha256,omitempty"`
	Attempts      int             `json:"attempts,omitempty"`
	NextAttemptAt *time.Time      `json:"next_attempt_at,omitempty"`
	LastError     string          `json:"last_error,omitempty"`
	QuarantinedAt *time.Time      `json:"quarantined_at,omitempty"`
}

type Stats struct {
	Depth         int   `json:"depth"`
	Bytes         int64 `json:"bytes"`
	OriginalBytes int64 `json:"original_bytes,omitempty"`
}

func Open(path string) (*Store, error) {
	return open(path, false)
}

func OpenReadOnly(path string) (*Store, error) {
	return open(path, true)
}

func open(path string, readOnly bool) (*Store, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 5 * time.Second, ReadOnly: readOnly})
	if err != nil {
		return nil, err
	}
	if !readOnly {
		if err := db.Update(func(tx *bolt.Tx) error {
			if _, err := tx.CreateBucketIfNotExists(bucketBatches); err != nil {
				return err
			}
			if _, err := tx.CreateBucketIfNotExists(bucketMeta); err != nil {
				return err
			}
			if _, err := tx.CreateBucketIfNotExists(bucketQuarantine); err != nil {
				return err
			}
			return repairCorruptActiveRecords(tx, time.Now().UTC())
		}); err != nil {
			_ = db.Close()
			return nil, err
		}
	}
	return &Store{db: db}, nil
}

// repairCorruptActiveRecords prevents a damaged legacy envelope from pinning
// every later batch before the uploader can inspect it. Only a hash and size
// are retained, never another long-lived copy of telemetry payload bytes.
func repairCorruptActiveRecords(tx *bolt.Tx, now time.Time) error {
	active := tx.Bucket(bucketBatches)
	quarantine := tx.Bucket(bucketQuarantine)
	if active == nil || quarantine == nil {
		return nil
	}
	cursor := active.Cursor()
	for key, raw := cursor.First(); key != nil; key, raw = cursor.Next() {
		var existing Record
		if err := json.Unmarshal(raw, &existing); err == nil {
			continue
		} else {
			sequence := uint64(0)
			if len(key) == 8 {
				sequence = btoi(key)
			}
			quarantinedAt := now.UTC()
			digest := sha256.Sum256(raw)
			record := Record{
				Key:           sequence,
				BatchID:       fmt.Sprintf("corrupt-spool-record-%x", key),
				CreatedAt:     quarantinedAt,
				OriginalSize:  int64(len(raw)),
				PayloadSHA256: hex.EncodeToString(digest[:]),
				Attempts:      1,
				LastError:     truncateError("corrupt persisted spool envelope: " + err.Error()),
				QuarantinedAt: &quarantinedAt,
			}
			encodedRecord, marshalErr := json.Marshal(record)
			if marshalErr != nil {
				return marshalErr
			}
			if putErr := quarantine.Put(append([]byte(nil), key...), encodedRecord); putErr != nil {
				return putErr
			}
			if deleteErr := cursor.Delete(); deleteErr != nil {
				return deleteErr
			}
		}
	}
	return pruneQuarantine(tx, now.UTC(), 0)
}

func (s *Store) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

// AcquireReplayLease serializes spool replay for the lifetime of the store,
// including across uploader objects rebuilt after a credential or controller
// configuration refresh. The returned function must always be called.
func (s *Store) AcquireReplayLease() func() {
	s.replayMu.Lock()
	return s.replayMu.Unlock
}

// AdvanceReplayGeneration invalidates every in-flight uploader generation.
// Mutations made through the replay-aware methods are linearized against this
// fence, so a replaced controller/client cannot acknowledge or alter records.
func (s *Store) AdvanceReplayGeneration() uint64 {
	s.replayGenerationMu.Lock()
	defer s.replayGenerationMu.Unlock()
	s.replayGeneration++
	return s.replayGeneration
}

func (s *Store) CurrentReplayGeneration() uint64 {
	s.replayGenerationMu.RLock()
	defer s.replayGenerationMu.RUnlock()
	return s.replayGeneration
}

func (s *Store) IsReplayGenerationCurrent(generation uint64) bool {
	s.replayGenerationMu.RLock()
	defer s.replayGenerationMu.RUnlock()
	return s.replayGeneration == generation
}

func (s *Store) withReplayGeneration(generation uint64, mutate func() error) error {
	s.replayGenerationMu.RLock()
	defer s.replayGenerationMu.RUnlock()
	if s.replayGeneration != generation {
		return ErrStaleReplayGeneration
	}
	return mutate()
}

func (s *Store) Enqueue(batchID string, payload []byte, maxBytes int64) (uint64, error) {
	if maxBytes > 0 && int64(len(payload)) > maxBytes {
		return 0, fmt.Errorf("payload size %d exceeds spool limit %d", len(payload), maxBytes)
	}
	var seq uint64
	err := s.db.Update(func(tx *bolt.Tx) error {
		meta := tx.Bucket(bucketMeta)
		seq = btoi(meta.Get(keySequence)) + 1
		if err := meta.Put(keySequence, itob(seq)); err != nil {
			return err
		}
		rec := Record{
			Key:       seq,
			BatchID:   batchID,
			CreatedAt: time.Now().UTC(),
			Size:      int64(len(payload)),
			Payload:   append([]byte(nil), payload...),
		}
		b, err := json.Marshal(rec)
		if err != nil {
			return err
		}
		if err := tx.Bucket(bucketBatches).Put(itob(seq), b); err != nil {
			return err
		}
		return pruneBySize(tx, maxBytes)
	})
	return seq, err
}

func (s *Store) Peek(limit int) ([]Record, error) {
	if limit <= 0 {
		limit = 1
	}
	var out []Record
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.First(); k != nil && len(out) < limit; k, v = c.Next() {
			var rec Record
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			out = append(out, rec)
		}
		return nil
	})
	return out, err
}

// PeekDue returns the oldest records whose retry deadline has elapsed. It
// deliberately scans past deferred records so one in-flight batch cannot
// block unrelated, ready telemetry behind it.
func (s *Store) PeekDue(limit int, now time.Time) ([]Record, error) {
	if limit <= 0 {
		limit = 1
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	var out []Record
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.First(); k != nil && len(out) < limit; k, v = c.Next() {
			var rec Record
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			if rec.NextAttemptAt != nil && rec.NextAttemptAt.After(now) {
				continue
			}
			out = append(out, rec)
		}
		return nil
	})
	return out, err
}

// NextAttempt reports whether active work is due now and, when it is not, the
// earliest persisted retry deadline. An empty active spool returns false, nil.
func (s *Store) NextAttempt(now time.Time) (bool, *time.Time, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	var next *time.Time
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return nil
		}
		return b.ForEach(func(_, v []byte) error {
			var rec Record
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			if rec.NextAttemptAt == nil || !rec.NextAttemptAt.After(now) {
				return errDueNow
			}
			if next == nil || rec.NextAttemptAt.Before(*next) {
				candidate := rec.NextAttemptAt.UTC()
				next = &candidate
			}
			return nil
		})
	})
	if errors.Is(err, errDueNow) {
		return true, nil, nil
	}
	return false, next, err
}

func (s *Store) PeekLatest(limit int) ([]Record, error) {
	if limit <= 0 {
		limit = 1
	}
	var out []Record
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.Last(); k != nil && len(out) < limit; k, v = c.Prev() {
			var rec Record
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			out = append(out, rec)
		}
		return nil
	})
	return out, err
}

// Defer records a failed attempt and its next eligible retry time atomically.
// Existing spool rows from older agents have zero-value retry fields and are
// therefore immediately due.
func (s *Store) Defer(key uint64, nextAttemptAt time.Time, lastError string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return ErrRecordNotFound
		}
		encodedKey := itob(key)
		raw := b.Get(encodedKey)
		if raw == nil {
			return ErrRecordNotFound
		}
		var rec Record
		if err := json.Unmarshal(raw, &rec); err != nil {
			return err
		}
		rec.Attempts++
		next := nextAttemptAt.UTC()
		rec.NextAttemptAt = &next
		rec.LastError = truncateError(lastError)
		updated, err := json.Marshal(rec)
		if err != nil {
			return err
		}
		return b.Put(encodedKey, updated)
	})
}

func (s *Store) DeferReplay(generation, key uint64, nextAttemptAt time.Time, lastError string) error {
	return s.withReplayGeneration(generation, func() error {
		return s.Defer(key, nextAttemptAt, lastError)
	})
}

// Postpone extends an existing retry deadline without recording another HTTP
// attempt. It is used by controller-wide circuit breakers after the individual
// failures have already been durably counted.
func (s *Store) Postpone(key uint64, notBefore time.Time) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return ErrRecordNotFound
		}
		encodedKey := itob(key)
		raw := b.Get(encodedKey)
		if raw == nil {
			return ErrRecordNotFound
		}
		var rec Record
		if err := json.Unmarshal(raw, &rec); err != nil {
			return err
		}
		next := notBefore.UTC()
		if rec.NextAttemptAt != nil && !next.After(*rec.NextAttemptAt) {
			return nil
		}
		rec.NextAttemptAt = &next
		updated, err := json.Marshal(rec)
		if err != nil {
			return err
		}
		return b.Put(encodedKey, updated)
	})
}

func (s *Store) PostponeReplay(generation, key uint64, notBefore time.Time) error {
	return s.withReplayGeneration(generation, func() error {
		return s.Postpone(key, notBefore)
	})
}

// Quarantine atomically removes a terminally colliding record from the active
// queue and retains it in a bounded diagnostic bucket.
func (s *Store) Quarantine(key uint64, now time.Time, lastError string) error {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		active := tx.Bucket(bucketBatches)
		quarantine := tx.Bucket(bucketQuarantine)
		if active == nil || quarantine == nil {
			return ErrRecordNotFound
		}
		encodedKey := itob(key)
		raw := active.Get(encodedKey)
		if raw == nil {
			// The move is idempotent if a caller repeats it after an uncertain
			// local outcome.
			if quarantine.Get(encodedKey) != nil {
				return nil
			}
			return ErrRecordNotFound
		}
		var rec Record
		if err := json.Unmarshal(raw, &rec); err != nil {
			return err
		}
		rec.Attempts++
		rec.NextAttemptAt = nil
		rec.LastError = truncateError(lastError)
		rec.OriginalSize = rec.Size
		digest := sha256.Sum256(rec.Payload)
		rec.PayloadSHA256 = hex.EncodeToString(digest[:])
		rec.Payload = nil
		rec.Size = 0
		quarantinedAt := now.UTC()
		rec.QuarantinedAt = &quarantinedAt
		updated, err := json.Marshal(rec)
		if err != nil {
			return err
		}
		if err := quarantine.Put(encodedKey, updated); err != nil {
			return err
		}
		if err := active.Delete(encodedKey); err != nil {
			return err
		}
		return pruneQuarantine(tx, now.UTC(), 0)
	})
}

func (s *Store) QuarantineReplay(generation, key uint64, now time.Time, lastError string) error {
	return s.withReplayGeneration(generation, func() error {
		return s.Quarantine(key, now, lastError)
	})
}

// PeekQuarantined exposes bounded recent terminal failures for diagnostics.
func (s *Store) PeekQuarantined(limit int) ([]Record, error) {
	if limit <= 0 {
		limit = 1
	}
	var out []Record
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketQuarantine)
		if b == nil {
			return nil
		}
		c := b.Cursor()
		for k, v := c.Last(); k != nil && len(out) < limit; k, v = c.Prev() {
			var rec Record
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			out = append(out, rec)
		}
		return nil
	})
	return out, err
}

func (s *Store) Ack(keys ...uint64) error {
	if len(keys) == 0 {
		return nil
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		for _, key := range keys {
			if err := b.Delete(itob(key)); err != nil {
				return err
			}
		}
		return nil
	})
}

func (s *Store) AckReplay(generation uint64, keys ...uint64) error {
	return s.withReplayGeneration(generation, func() error {
		return s.Ack(keys...)
	})
}

func (s *Store) Stats() (Stats, error) {
	return s.bucketStats(bucketBatches)
}

// QuarantineStats is separate from Stats so active queue depth reaches zero
// after a terminal record is isolated.
func (s *Store) QuarantineStats() (Stats, error) {
	var st Stats
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketQuarantine)
		if b == nil {
			return nil
		}
		return b.ForEach(func(_, value []byte) error {
			var rec Record
			if err := json.Unmarshal(value, &rec); err != nil {
				return err
			}
			st.Depth++
			st.Bytes += int64(len(value))
			if rec.OriginalSize > 0 {
				st.OriginalBytes += rec.OriginalSize
			} else {
				st.OriginalBytes += rec.Size
			}
			return nil
		})
	})
	return st, err
}

func (s *Store) bucketStats(bucket []byte) (Stats, error) {
	var st Stats
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucket)
		if b == nil {
			return nil
		}
		return b.ForEach(func(_, v []byte) error {
			var rec Record
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			st.Depth++
			st.Bytes += rec.Size
			return nil
		})
	})
	return st, err
}

func (s *Store) Prune(maxAge time.Duration, maxBytes int64) error {
	if maxAge <= 0 && maxBytes <= 0 {
		return nil
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if maxAge > 0 {
			cutoff := time.Now().UTC().Add(-maxAge)
			c := b.Cursor()
			for k, v := c.First(); k != nil; k, v = c.Next() {
				var rec Record
				if err := json.Unmarshal(v, &rec); err != nil {
					return err
				}
				if rec.CreatedAt.Before(cutoff) {
					if err := b.Delete(k); err != nil {
						return err
					}
				}
			}
		}
		if err := pruneBySize(tx, maxBytes); err != nil {
			return err
		}
		return pruneQuarantine(tx, time.Now().UTC(), maxAge)
	})
}

func pruneQuarantine(tx *bolt.Tx, now time.Time, configuredMaxAge time.Duration) error {
	b := tx.Bucket(bucketQuarantine)
	if b == nil {
		return nil
	}
	retention := QuarantineMaxAge
	if configuredMaxAge > 0 && configuredMaxAge < retention {
		retention = configuredMaxAge
	}
	cutoff := now.Add(-retention)
	c := b.Cursor()
	for k, v := c.First(); k != nil; k, v = c.Next() {
		var rec Record
		if err := json.Unmarshal(v, &rec); err != nil {
			return err
		}
		if rec.QuarantinedAt != nil && rec.QuarantinedAt.Before(cutoff) {
			if err := c.Delete(); err != nil {
				return err
			}
		}
	}

	for {
		stats, err := bucketStatsInTx(b)
		if err != nil {
			return err
		}
		encodedBytes, err := encodedBytesInTx(b)
		if err != nil {
			return err
		}
		if stats.Depth <= QuarantineMaxRecords && encodedBytes <= QuarantineMaxBytes {
			return nil
		}
		k, _ := b.Cursor().First()
		if k == nil {
			return nil
		}
		if err := b.Delete(k); err != nil {
			return err
		}
	}
}

func encodedBytesInTx(b *bolt.Bucket) (int64, error) {
	var total int64
	err := b.ForEach(func(_, value []byte) error {
		total += int64(len(value))
		return nil
	})
	return total, err
}

func bucketStatsInTx(b *bolt.Bucket) (Stats, error) {
	var st Stats
	err := b.ForEach(func(_, v []byte) error {
		var rec Record
		if err := json.Unmarshal(v, &rec); err != nil {
			return err
		}
		st.Depth++
		st.Bytes += rec.Size
		return nil
	})
	return st, err
}

func truncateError(message string) string {
	const maxErrorBytes = 2048
	if len(message) <= maxErrorBytes {
		return message
	}
	return message[:maxErrorBytes]
}

func pruneBySize(tx *bolt.Tx, maxBytes int64) error {
	if maxBytes <= 0 {
		return nil
	}
	b := tx.Bucket(bucketBatches)
	for {
		total, err := sizeInTx(b)
		if err != nil {
			return err
		}
		if total <= maxBytes {
			return nil
		}
		k, _ := b.Cursor().First()
		if k == nil {
			return errors.New("spool size limit exceeded but no records can be pruned")
		}
		if err := b.Delete(k); err != nil {
			return err
		}
	}
}

func sizeInTx(b *bolt.Bucket) (int64, error) {
	var total int64
	err := b.ForEach(func(_, v []byte) error {
		var rec Record
		if err := json.Unmarshal(v, &rec); err != nil {
			return err
		}
		total += rec.Size
		return nil
	})
	return total, err
}

func itob(v uint64) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint64(b, v)
	return b
}

func btoi(b []byte) uint64 {
	if len(b) == 0 {
		return 0
	}
	return binary.BigEndian.Uint64(b)
}
