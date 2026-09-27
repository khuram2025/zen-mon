package spool

import (
	"encoding/json"
	"sort"
	"time"

	bolt "go.etcd.io/bbolt"
)

// The active queue is scanned for depth, byte totals, the next retry deadline
// and pruning on every collection and upload. Decoding every stored batch for
// each of those grew with the queue: during a long controller outage the
// queue holds hundreds of MB and trimming it to the size cap rescanned the
// whole bucket once per deleted record. recordIndex keeps the few fields
// those operations need in memory. It is built once when the store opens and
// changed only after the bolt transaction that made the change has
// committed, under indexMu, so it always matches the database.

type recordMeta struct {
	size          int64
	createdAt     time.Time
	nextAttemptAt time.Time // zero when due immediately
}

// recordHeader decodes a stored record without materialising its payload.
type recordHeader struct {
	Size          int64      `json:"size"`
	CreatedAt     time.Time  `json:"created_at"`
	NextAttemptAt *time.Time `json:"next_attempt_at,omitempty"`
}

func (h recordHeader) meta() recordMeta {
	m := recordMeta{size: h.Size, createdAt: h.CreatedAt}
	if h.NextAttemptAt != nil {
		m.nextAttemptAt = h.NextAttemptAt.UTC()
	}
	return m
}

func (s *Store) buildIndex() error {
	index := map[uint64]recordMeta{}
	var total int64
	err := s.db.View(func(tx *bolt.Tx) error {
		b := tx.Bucket(bucketBatches)
		if b == nil {
			return nil
		}
		return b.ForEach(func(k, v []byte) error {
			var h recordHeader
			if err := json.Unmarshal(v, &h); err != nil {
				return err
			}
			index[btoi(k)] = h.meta()
			total += h.Size
			return nil
		})
	})
	if err != nil {
		return err
	}
	s.index = index
	s.indexBytes = total
	return nil
}

// indexAdd and indexRemove must be called with indexMu held, after commit.
func (s *Store) indexAdd(key uint64, m recordMeta) {
	if old, ok := s.index[key]; ok {
		s.indexBytes -= old.size
	}
	s.index[key] = m
	s.indexBytes += m.size
}

func (s *Store) indexRemove(keys ...uint64) {
	for _, key := range keys {
		if old, ok := s.index[key]; ok {
			s.indexBytes -= old.size
			delete(s.index, key)
		}
	}
}

func (s *Store) sortedKeysLocked() []uint64 {
	keys := make([]uint64, 0, len(s.index))
	for key := range s.index {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	return keys
}

// oldestOverLimitLocked returns the oldest keys to delete so the queue,
// after adding extra bytes, fits within maxBytes. It does not modify the
// index; the caller deletes the records and then removes the keys.
func (s *Store) oldestOverLimitLocked(maxBytes, extra int64, exclude map[uint64]bool) []uint64 {
	if maxBytes <= 0 {
		return nil
	}
	total := s.indexBytes + extra
	var victims []uint64
	for _, key := range s.sortedKeysLocked() {
		if total <= maxBytes {
			break
		}
		if exclude[key] {
			continue
		}
		total -= s.index[key].size
		victims = append(victims, key)
	}
	return victims
}
