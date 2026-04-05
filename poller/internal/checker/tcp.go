package checker

import (
	"context"
	"fmt"
	"net"
	"time"

	"go.uber.org/zap"
)

// TCPChecker performs TCP port connectivity checks.
type TCPChecker struct {
	logger *zap.SugaredLogger
}

// NewTCPChecker creates a new TCP checker.
func NewTCPChecker(logger *zap.SugaredLogger) *TCPChecker {
	return &TCPChecker{logger: logger}
}

// Check performs a TCP connection check.
func (c *TCPChecker) Check(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	result := &ServiceCheckResult{
		ServiceCheckID: sc.ID,
		DeviceID:       sc.DeviceID,
		CheckType:      "tcp",
		Timestamp:      time.Now().UTC(),
		PollerID:       pollerID,
	}

	addr := fmt.Sprintf("%s:%d", sc.TargetHost, sc.TargetPort)

	start := time.Now()
	conn, err := net.DialTimeout("tcp", addr, sc.Timeout)
	result.ResponseTime = time.Since(start)

	if err != nil {
		result.Error = fmt.Sprintf("tcp connect failed: %v", err)
		result.IsUp = false
		return result
	}
	conn.Close()

	result.IsUp = true
	return result
}
