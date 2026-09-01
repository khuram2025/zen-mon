package checker

import (
	"context"
	"fmt"
	"time"

	probing "github.com/prometheus-community/pro-bing"
	"go.uber.org/zap"
)

// ICMPChecker performs ICMP echo-request checks.
type ICMPChecker struct {
	logger *zap.SugaredLogger
}

// NewICMPChecker creates a new ICMP checker.
func NewICMPChecker(logger *zap.SugaredLogger) *ICMPChecker {
	return &ICMPChecker{logger: logger}
}

// Check runs N ICMP probes (default 1, clamped to 1..10) against sc.TargetHost
// and reports average RTT. Uses unprivileged ("udp") mode if CAP_NET_RAW is
// unavailable, falling back to privileged automatically.
func (c *ICMPChecker) Check(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	result := &ServiceCheckResult{
		ServiceCheckID: sc.ID,
		DeviceID:       sc.DeviceID,
		CheckType:      "icmp",
		Timestamp:      time.Now().UTC(),
		PollerID:       pollerID,
	}

	count := 1
	if sc.Config != nil {
		if v, ok := sc.Config["count"]; ok {
			switch n := v.(type) {
			case float64:
				count = int(n)
			case int:
				count = n
			}
		}
	}
	if count < 1 {
		count = 1
	}
	if count > 10 {
		count = 10
	}

	pinger, err := probing.NewPinger(sc.TargetHost)
	if err != nil {
		result.IsUp = false
		result.Error = fmt.Sprintf("resolve: %v", err)
		return result
	}
	pinger.Count = count
	pinger.Timeout = sc.Timeout
	pinger.SetPrivileged(true) // zenplus-poller has CAP_NET_RAW set by setcap

	start := time.Now()
	err = pinger.RunWithContext(ctx)
	result.ResponseTime = time.Since(start)
	if err != nil {
		// Try unprivileged fallback once.
		pinger.SetPrivileged(false)
		if err2 := pinger.RunWithContext(ctx); err2 != nil {
			result.IsUp = false
			result.Error = fmt.Sprintf("ping: %v", err)
			return result
		}
	}

	stats := pinger.Statistics()
	result.PacketLoss = stats.PacketLoss / 100.0
	result.Jitter = stats.StdDevRtt
	result.MinRTT = stats.MinRtt
	result.MaxRTT = stats.MaxRtt
	result.PacketsSent = stats.PacketsSent
	result.PacketsReceived = stats.PacketsRecv
	if stats.PacketsRecv == 0 {
		result.IsUp = false
		result.Error = fmt.Sprintf("%d/%d packets lost", stats.PacketsSent-stats.PacketsRecv, stats.PacketsSent)
		return result
	}
	// Use average RTT as the response time.
	result.ResponseTime = stats.AvgRtt
	result.IsUp = true
	if stats.PacketLoss > 0 {
		result.Error = fmt.Sprintf("%.0f%% packet loss", stats.PacketLoss)
	}
	return result
}
