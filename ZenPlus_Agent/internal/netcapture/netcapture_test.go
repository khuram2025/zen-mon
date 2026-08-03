package netcapture

import (
	"context"
	"testing"
	"time"
)

// A short live capture against the real host. Asserts the plumbing works and
// prints what it saw so the byte attribution can be eyeballed.
func TestLiveCaptureCollectsFlows(t *testing.T) {
	var (
		final   []Flow
		stats   Stats
		flushes int
	)
	opts := Options{
		Duration:       6 * time.Second,
		SampleInterval: time.Second,
		FlushInterval:  2 * time.Second,
	}
	got, err := Run(context.Background(), opts, func(flows []Flow, isFinal bool, s Stats) {
		flushes++
		if isFinal {
			final = flows
			stats = s
		}
	}, t.Logf)
	if err != nil {
		t.Fatalf("capture failed: %v", err)
	}
	if flushes == 0 {
		t.Fatal("no flush callbacks fired")
	}
	if got.Samples == 0 {
		t.Fatal("no samples taken")
	}

	t.Logf("samples=%d flows=%d bytes_available=%v note=%q",
		stats.Samples, len(final), stats.BytesAvailable, stats.Note)

	withBytes := 0
	for i, f := range final {
		if f.BytesKnown && (f.BytesSent+f.BytesReceived) > 0 {
			withBytes++
		}
		if i < 5 {
			t.Logf("  %s %s:%d -> %s:%d pid=%d proc=%q svc=%q sent=%d recv=%d known=%v state=%s",
				f.Protocol, f.LocalIP, f.LocalPort, f.RemoteIP, f.RemotePort,
				f.PID, f.ProcessName, f.ServiceName, f.BytesSent, f.BytesReceived,
				f.BytesKnown, f.State)
		}
	}
	t.Logf("flows carrying non-zero byte counts: %d/%d", withBytes, len(final))

	for _, f := range final {
		if f.RemoteIP == "" || f.RemotePort == 0 {
			t.Fatalf("listener leaked into flows: %+v", f)
		}
		if f.Samples <= 0 {
			t.Fatalf("flow recorded with no samples: %+v", f)
		}
	}
}

func TestInterfaceFilterRejectsUnknownName(t *testing.T) {
	_, err := Run(context.Background(),
		Options{Duration: time.Second, Interface: "no-such-nic-xyz"},
		func([]Flow, bool, Stats) {}, t.Logf)
	if err == nil {
		t.Fatal("expected an error for an unknown interface")
	}
}

func TestOptionDefaultsAndClamp(t *testing.T) {
	var o Options
	o.applyDefaults()
	if o.Duration != 5*time.Minute {
		t.Fatalf("default duration should be 5m, got %s", o.Duration)
	}
	o = Options{Duration: 6 * time.Hour}
	o.applyDefaults()
	if o.Duration != time.Hour {
		t.Fatalf("duration should clamp to 1h, got %s", o.Duration)
	}
}
