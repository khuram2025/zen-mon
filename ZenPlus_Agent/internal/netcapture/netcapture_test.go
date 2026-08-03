package netcapture

import (
	"context"
	"os"
	"testing"
	"time"

	"zenplus-agent/internal/netiface"
)

// A short live capture against the real host. Asserts the plumbing works and
// prints what it saw so the byte attribution can be eyeballed.
func TestLiveCaptureCollectsFlows(t *testing.T) {
	if os.Getenv("ZENPLUS_LIVE_CAPTURE_TEST") != "1" {
		t.Skip("set ZENPLUS_LIVE_CAPTURE_TEST=1 to run the host integration test")
	}
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
	if len(stats.Interfaces) == 0 {
		t.Fatal("no native interface traffic samples were captured")
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

func TestCollectorTracksFlowBytesFromFirstObservedCounter(t *testing.T) {
	c := &collector{flows: map[flowKey]*Flow{}, baseline: map[flowKey][2]uint64{}}
	first := rawConn{Protocol: "tcp", LocalIP: "10.0.0.1", LocalPort: 1234,
		RemoteIP: "10.0.0.2", RemotePort: 443, PID: 42,
		BytesSent: 1000, BytesReceived: 2000, BytesKnown: true}
	c.merge([]rawConn{first}, nil, 10)
	first.BytesSent = 1600
	first.BytesReceived = 2900
	c.merge([]rawConn{first}, nil, 10)
	flows, _, _, available := c.snapshot()
	if !available || len(flows) != 1 {
		t.Fatalf("unexpected snapshot: available=%v flows=%+v", available, flows)
	}
	if flows[0].BytesSent != 600 || flows[0].BytesReceived != 900 {
		t.Fatalf("expected window deltas 600/900, got %d/%d", flows[0].BytesSent, flows[0].BytesReceived)
	}
}

func TestInterfaceTrackerCalculatesWindowTotalsRatesAndUtilization(t *testing.T) {
	rows := [][]netiface.Counter{
		{{Name: "Ethernet", InterfaceIndex: 7, BytesReceived: 1000, BytesSent: 500,
			ReceiveLinkSpeedBPS: 1_000_000, TransmitLinkSpeedBPS: 2_000_000}},
		{{Name: "Ethernet", InterfaceIndex: 7, BytesReceived: 3000, BytesSent: 1500,
			ReceiveLinkSpeedBPS: 1_000_000, TransmitLinkSpeedBPS: 2_000_000}},
		{{Name: "Ethernet", InterfaceIndex: 7, BytesReceived: 3500, BytesSent: 5500,
			ReceiveLinkSpeedBPS: 1_000_000, TransmitLinkSpeedBPS: 2_000_000}},
	}
	index := 0
	tracker := newInterfaceTracker("Ethernet", func(context.Context) ([]netiface.Counter, error) {
		row := rows[index]
		index++
		return row, nil
	})
	t0 := time.Date(2026, 8, 3, 10, 0, 0, 0, time.UTC)
	if _, err := tracker.sample(context.Background(), t0); err != nil {
		t.Fatal(err)
	}
	second, err := tracker.sample(context.Background(), t0.Add(2*time.Second))
	if err != nil || len(second) != 1 {
		t.Fatalf("second sample: %+v, %v", second, err)
	}
	got := second[0]
	if got.RXBytes != 2000 || got.TXBytes != 1000 || got.RXBPS != 8000 || got.TXBPS != 4000 {
		t.Fatalf("unexpected second sample: %+v", got)
	}
	if got.RXUtilizationPct != 0.8 || got.TXUtilizationPct != 0.2 {
		t.Fatalf("unexpected utilization: rx=%v tx=%v", got.RXUtilizationPct, got.TXUtilizationPct)
	}
	third, err := tracker.sample(context.Background(), t0.Add(4*time.Second))
	if err != nil || len(third) != 1 {
		t.Fatalf("third sample: %+v, %v", third, err)
	}
	if third[0].PeakRXBPS != 8000 || third[0].PeakTXBPS != 16000 {
		t.Fatalf("unexpected peaks: %+v", third[0])
	}
}

func TestInterfaceTrackerRebasesAfterCounterReset(t *testing.T) {
	rows := [][]netiface.Counter{
		{{Name: "Ethernet", InterfaceIndex: 7, BytesReceived: 10_000, BytesSent: 20_000}},
		{{Name: "Ethernet", InterfaceIndex: 7, BytesReceived: 100, BytesSent: 200}},
		{{Name: "Ethernet", InterfaceIndex: 7, BytesReceived: 600, BytesSent: 1_200}},
	}
	index := 0
	tracker := newInterfaceTracker("", func(context.Context) ([]netiface.Counter, error) {
		row := rows[index]
		index++
		return row, nil
	})
	t0 := time.Date(2026, 8, 3, 10, 0, 0, 0, time.UTC)
	_, _ = tracker.sample(context.Background(), t0)
	reset, _ := tracker.sample(context.Background(), t0.Add(time.Second))
	if reset[0].RXBytes != 0 || reset[0].TXBytes != 0 || reset[0].RXBPS != 0 || reset[0].TXBPS != 0 {
		t.Fatalf("counter reset should rebase at zero: %+v", reset[0])
	}
	after, _ := tracker.sample(context.Background(), t0.Add(2*time.Second))
	if after[0].RXBytes != 500 || after[0].TXBytes != 1000 {
		t.Fatalf("tracker did not continue after reset: %+v", after[0])
	}
}
