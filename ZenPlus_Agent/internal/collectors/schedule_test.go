package collectors

import (
	"testing"
	"time"
)

func TestDueHonoursInterval(t *testing.T) {
	ResetSchedule()
	start := time.Now().UTC()
	if !due("inventory", 3600, start) {
		t.Fatal("first tick should always run so a fresh agent reports immediately")
	}
	if due("inventory", 3600, start.Add(time.Minute)) {
		t.Fatal("collector ran again well inside its 1h interval")
	}
	if !due("inventory", 3600, start.Add(61*time.Minute)) {
		t.Fatal("collector did not run after its interval elapsed")
	}
}

func TestDueZeroIntervalRunsEveryTick(t *testing.T) {
	ResetSchedule()
	now := time.Now().UTC()
	for i := 0; i < 3; i++ {
		if !due("cpu", 0, now.Add(time.Duration(i)*time.Second)) {
			t.Fatalf("tick %d: interval<=0 must run every tick", i)
		}
	}
}

func TestResetScheduleClearsHistory(t *testing.T) {
	ResetSchedule()
	now := time.Now().UTC()
	due("process", 600, now)
	if due("process", 600, now.Add(time.Minute)) {
		t.Fatal("expected suppression inside the interval")
	}
	ResetSchedule()
	if !due("process", 600, now.Add(time.Minute)) {
		t.Fatal("a policy change must let new intervals take effect immediately")
	}
}
