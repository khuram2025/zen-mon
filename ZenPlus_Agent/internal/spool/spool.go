package spool

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	bucketBatches = []byte("batches")
	bucketMeta    = []byte("meta")
	keySequence   = []byte("sequence")
)

type Store struct {
	db *bolt.DB
}

type Record struct {
	Key       uint64          `json:"key"`
	BatchID   string          `json:"batch_id"`
	CreatedAt time.Time       `json:"created_at"`
	Size      int64           `json:"size"`
	Payload   json.RawMessage `json:"payload"`
}

type Stats struct {
	Depth int   `json:"depth"`
	Bytes int64 `json:"bytes"`
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
			_, err := tx.CreateBucketIfNotExists(bucketMeta)
			return err
		}); err != nil {
			_ = db.Close()
			return nil, err
		}
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

func (s *Store) Enqueue(batchID string, payload []byte, maxBytes int64) (uint64, error) {
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

func (s *Store) Stats() (Stats, error) {
	var st Stats
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
		return pruneBySize(tx, maxBytes)
	})
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
