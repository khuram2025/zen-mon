// Package netiface exposes low-cost interface counters and link metadata in a
// platform-neutral form. Windows uses GetIfTable2Ex so link speeds and byte
// counters come from the same atomic table snapshot.
package netiface

import (
	"context"
	"strings"
)

type Counter struct {
	Name                 string
	Description          string
	InterfaceIndex       uint32
	InterfaceID          string
	BytesReceived        uint64
	BytesSent            uint64
	PacketsReceived      uint64
	PacketsSent          uint64
	ReceiveErrors        uint64
	TransmitErrors       uint64
	ReceiveDiscards      uint64
	TransmitDiscards     uint64
	ReceiveLinkSpeedBPS  uint64
	TransmitLinkSpeedBPS uint64
	IsUp                 bool
}

func Snapshot(ctx context.Context) ([]Counter, error) {
	return snapshot(ctx)
}

func Find(counters []Counter, name string) (Counter, bool) {
	name = strings.TrimSpace(name)
	for _, counter := range counters {
		if strings.EqualFold(counter.Name, name) ||
			(counter.Description != "" && strings.EqualFold(counter.Description, name)) {
			return counter, true
		}
	}
	return Counter{}, false
}

func SpeedMbps(bitsPerSecond uint64) float64 {
	return float64(bitsPerSecond) / 1_000_000
}
