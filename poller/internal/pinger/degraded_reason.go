package pinger

import (
	"fmt"
	"strings"
)

func degradedReason(rttMs, lossPct, rttLimit, lossLimit float64) string {
	parts := []string{}
	if rttMs > rttLimit {
		parts = append(parts, fmt.Sprintf("Latency %.3f ms exceeds %g ms by %.3f ms", rttMs, rttLimit, rttMs-rttLimit))
	}
	if float32(lossPct/100) > float32(lossLimit)/100 {
		parts = append(parts, fmt.Sprintf("Packet loss %.2f%% exceeds %g%% by %.2f percentage points", lossPct, lossLimit, lossPct-lossLimit))
	}
	if len(parts) == 0 {
		return "High latency or packet loss"
	}
	return strings.Join(parts, "; ")
}
