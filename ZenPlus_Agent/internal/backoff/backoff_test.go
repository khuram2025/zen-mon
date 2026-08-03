package backoff

import (
	"testing"
	"time"
)

func TestNextGrowsAndCaps(t *testing.T) {
	b := New(time.Second, 30*time.Second)
	var last time.Duration
	for i := 0; i < 12; i++ {
		d := b.Next()
		if d <= 0 || d > 30*time.Second {
			t.Fatalf("attempt %d produced out-of-range delay %s", i, d)
		}
		last = d
	}
	// After enough failures the delay must sit in the capped jitter window.
	if last < 15*time.Second {
		t.Fatalf("expected capped delay >= 15s, got %s", last)
	}
}

func TestNextIsJittered(t *testing.T) {
	seen := map[time.Duration]bool{}
	for i := 0; i < 40; i++ {
		b := New(time.Second, time.Minute)
		b.Next()
		b.Next()
		b.Next()
		seen[b.Next()] = true
	}
	if len(seen) < 5 {
		t.Fatalf("expected jittered delays, got %d distinct values", len(seen))
	}
}

func TestResetClearsStreak(t *testing.T) {
	b := New(time.Second, time.Minute)
	for i := 0; i < 8; i++ {
		b.Next()
	}
	b.Reset()
	if b.Attempts() != 0 {
		t.Fatalf("attempts not reset: %d", b.Attempts())
	}
	if d := b.Next(); d > time.Second {
		t.Fatalf("expected base-range delay after reset, got %s", d)
	}
}
