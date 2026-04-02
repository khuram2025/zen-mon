package pinger

import (
	"context"
	"sync"
	"time"

	probing "github.com/prometheus-community/pro-bing"
	"go.uber.org/zap"
)

// Pinger performs ICMP pings against a batch of devices.
type Pinger struct {
	timeout    time.Duration
	count      int
	interval   time.Duration
	privileged bool
	logger     *zap.SugaredLogger
}

// NewPinger creates a new Pinger with the given settings.
func NewPinger(timeout time.Duration, count int, interval time.Duration, privileged bool, logger *zap.SugaredLogger) *Pinger {
	return &Pinger{
		timeout:    timeout,
		count:      count,
		interval:   interval,
		privileged: privileged,
		logger:     logger,
	}
}

// PingDevice pings a single device and returns the result.
func (p *Pinger) PingDevice(ctx context.Context, device *Device, pollerID string) *PingResult {
	pinger, err := probing.NewPinger(device.IPAddress.String())
	if err != nil {
		p.logger.Warnf("Failed to create pinger for %s (%s): %v", device.Hostname, device.IPAddress, err)
		return &PingResult{
			DeviceID:   device.ID,
			IPAddress:  device.IPAddress,
			IsUp:       false,
			PacketLoss: 1.0,
			Sent:       p.count,
			Received:   0,
			Timestamp:  time.Now().UTC(),
			PollerID:   pollerID,
		}
	}

	pinger.Count = p.count
	pinger.Timeout = p.timeout
	pinger.Interval = p.interval
	pinger.SetPrivileged(p.privileged)

	err = pinger.RunWithContext(ctx)
	if err != nil {
		p.logger.Debugf("Ping failed for %s (%s): %v", device.Hostname, device.IPAddress, err)
	}
	stats := pinger.Statistics()

	if err != nil || stats.PacketsRecv == 0 {
		return &PingResult{
			DeviceID:   device.ID,
			IPAddress:  device.IPAddress,
			IsUp:       false,
			PacketLoss: 1.0,
			Sent:       stats.PacketsSent,
			Received:   stats.PacketsRecv,
			Timestamp:  time.Now().UTC(),
			PollerID:   pollerID,
		}
	}

	return &PingResult{
		DeviceID:   device.ID,
		IPAddress:  device.IPAddress,
		IsUp:       true,
		RTT:        stats.AvgRtt,
		PacketLoss: float32(stats.PacketLoss) / 100.0,
		Jitter:     stats.StdDevRtt,
		MinRTT:     stats.MinRtt,
		MaxRTT:     stats.MaxRtt,
		Sent:       stats.PacketsSent,
		Received:   stats.PacketsRecv,
		Timestamp:  time.Now().UTC(),
		PollerID:   pollerID,
	}
}

// PingBatch pings a batch of devices concurrently with a worker pool.
func (p *Pinger) PingBatch(ctx context.Context, devices []*Device, pollerID string, maxWorkers int) []*PingResult {
	results := make([]*PingResult, 0, len(devices))
	var mu sync.Mutex

	sem := make(chan struct{}, maxWorkers)
	var wg sync.WaitGroup

	for _, device := range devices {
		if ctx.Err() != nil {
			break
		}

		wg.Add(1)
		sem <- struct{}{}

		go func(d *Device) {
			defer wg.Done()
			defer func() { <-sem }()

			result := p.PingDevice(ctx, d, pollerID)
			mu.Lock()
			results = append(results, result)
			mu.Unlock()
		}(device)
	}

	wg.Wait()
	return results
}
