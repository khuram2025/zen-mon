package pinger

import (
	"context"
	"github.com/google/uuid"
	"net"
	"strings"
	"testing"
	"time"
)

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
			e.degradedRTTMs = 500
			e.degradedLossPct = 5
			id := uuid.New()
			e.devices[id] = &Device{ID: id, Hostname: "test", IPAddress: net.ParseIP("127.0.0.1"), Status: "up"}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			e.processStatusChange(ctx, &PingResult{DeviceID: id, IsUp: true, RTT: tc.rtt, PacketLoss: tc.loss, Timestamp: time.Now()})
			if e.devices[id].Status != tc.want {
				t.Fatalf("got %s, want %s", e.devices[id].Status, tc.want)
			}
		})
	}
}

func TestDegradedReasonIncludesMeasuredExcess(t *testing.T) {
	got := degradedReason(.839, 50, 500, 5)
	if got != "Packet loss 50.00% exceeds 5% by 45.00 percentage points" {
		t.Fatal(got)
	}
	got = degradedReason(650, 50, 500, 5)
	if !strings.Contains(got, "Latency 650.000 ms exceeds 500 ms by 150.000 ms") || !strings.Contains(got, "Packet loss") {
		t.Fatal(got)
	}
	got = degradedReason(650, float64(float32(.05))*100, 500, 5)
	if strings.Contains(got, "Packet loss") {
		t.Fatal("rounded equality must not be blamed for latency degradation:", got)
	}
}
