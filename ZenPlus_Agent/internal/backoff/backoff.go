// Package backoff implements exponential backoff with jitter for
// controller communication. Retries during an outage double the wait
// (capped at Max) and randomize within [d/2, d) so a fleet of agents
// does not stampede the controller when it comes back.
package backoff

import (
	"math/rand"
	"sync"
	"time"
)

type Backoff struct {
	Base time.Duration
	Max  time.Duration

	mu      sync.Mutex
	attempt int
}

func New(base, max time.Duration) *Backoff {
	return &Backoff{Base: base, Max: max}
}

// Next records a failure and returns how long to wait before retrying.
func (b *Backoff) Next() time.Duration {
	b.mu.Lock()
	defer b.mu.Unlock()
	base := b.Base
	if base <= 0 {
		base = time.Second
	}
	max := b.Max
	if max <= 0 {
		max = 15 * time.Minute
	}
	d := base
	for i := 0; i < b.attempt && d < max; i++ {
		d *= 2
	}
	if d > max {
		d = max
	}
	if b.attempt < 62 {
		b.attempt++
	}
	half := d / 2
	return half + time.Duration(rand.Int63n(int64(half)+1))
}

// Reset clears the failure streak after a success.
func (b *Backoff) Reset() {
	b.mu.Lock()
	b.attempt = 0
	b.mu.Unlock()
}

func (b *Backoff) Attempts() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.attempt
}
