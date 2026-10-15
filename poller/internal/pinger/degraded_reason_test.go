package pinger

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/devicestatus"
)

// Admin limits come from the 'monitoring' policy. A breach is evidence, not a
// status: with the default confirmation (3 of the last 5 checks) one sample
// over a limit leaves the device up and the third consecutive one degrades it.
func TestConfiguredDegradedThresholds(t *testing.T) {
	for _, tc := range []struct {
		name string
		rtt  time.Duration
		loss float32
		want string
	}{
		{"at limits", 500 * time.Millisecond, .05, "up"},
		{"loss exceeds", time.Millisecond, .5, "degraded"},
		{"latency exceeds", 501 * time.Millisecond, 0, "degraded"},
		{"under configured but over defaults", 250 * time.Millisecond, 0, "up"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := testEngine()
			stopStatusOnCleanup(t, e)
			e.policy = devicestatus.PolicyFromSettings(
				map[string]any{"degraded_rtt_ms": 500.0, "degraded_loss_pct": 5.0}, policyDefaults(e.cfg))
			id := uuid.New()
			e.devices[id] = &Device{ID: id, Hostname: "test", IPAddress: net.ParseIP("127.0.0.1"), Status: "up"}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			base := time.Now()
			for i := 0; i < 3; i++ {
				e.processStatusChange(ctx, &PingResult{DeviceID: id, IsUp: true, RTT: tc.rtt, PacketLoss: tc.loss,
					Timestamp: base.Add(time.Duration(i) * 30 * time.Second)})
				if i == 0 && e.devices[id].Status != "up" {
					t.Fatalf("one sample over a limit changed the status to %s", e.devices[id].Status)
				}
			}
			if e.devices[id].Status != tc.want {
				t.Fatalf("got %s, want %s", e.devices[id].Status, tc.want)
			}
		})
	}
}

// The reason text comes from the shared machine (devicestatus.DegradedReason,
// loss as a fraction) with the canonical predicates.
func TestDegradedReasonIncludesMeasuredExcess(t *testing.T) {
	got := devicestatus.DegradedReason(.839, .5, 500, 5)
	if got != "Packet loss 50.00% exceeds 5% by 45.00 percentage points" {
		t.Fatal(got)
	}
	got = devicestatus.DegradedReason(650, .5, 500, 5)
	if !strings.Contains(got, "Latency 650.000 ms exceeds 500 ms by 150.000 ms") || !strings.Contains(got, "Packet loss") {
		t.Fatal(got)
	}
	got = devicestatus.DegradedReason(650, float64(float32(.05)), 500, 5)
	if strings.Contains(got, "Packet loss") {
		t.Fatal("rounded equality must not be blamed for latency degradation:", got)
	}
}
