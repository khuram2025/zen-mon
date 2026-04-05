package checker

import (
	"context"
	"sync"

	"go.uber.org/zap"
)

// Checker dispatches service checks to the appropriate handler.
type Checker struct {
	httpChecker *HTTPChecker
	tcpChecker  *TCPChecker
	tlsChecker  *TLSChecker
	logger      *zap.SugaredLogger
}

// NewChecker creates a new Checker.
func NewChecker(logger *zap.SugaredLogger) *Checker {
	return &Checker{
		httpChecker: NewHTTPChecker(logger),
		tcpChecker:  NewTCPChecker(logger),
		tlsChecker:  NewTLSChecker(logger),
		logger:      logger,
	}
}

// CheckOne runs a single service check, dispatching by type.
func (c *Checker) CheckOne(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	switch sc.CheckType {
	case "http":
		return c.httpChecker.Check(ctx, sc, pollerID)
	case "tcp":
		return c.tcpChecker.Check(ctx, sc, pollerID)
	case "tls":
		return c.tlsChecker.Check(ctx, sc, pollerID)
	default:
		return &ServiceCheckResult{
			ServiceCheckID: sc.ID,
			DeviceID:       sc.DeviceID,
			CheckType:      sc.CheckType,
			Error:          "unknown check type: " + sc.CheckType,
		}
	}
}

// CheckBatch runs a batch of service checks concurrently with a worker pool.
func (c *Checker) CheckBatch(ctx context.Context, checks []*ServiceCheck, pollerID string, maxWorkers int) []*ServiceCheckResult {
	results := make([]*ServiceCheckResult, 0, len(checks))
	var mu sync.Mutex

	sem := make(chan struct{}, maxWorkers)
	var wg sync.WaitGroup

	for _, check := range checks {
		if ctx.Err() != nil {
			break
		}

		wg.Add(1)
		sem <- struct{}{}

		go func(sc *ServiceCheck) {
			defer wg.Done()
			defer func() { <-sem }()

			result := c.CheckOne(ctx, sc, pollerID)
			mu.Lock()
			results = append(results, result)
			mu.Unlock()
		}(check)
	}

	wg.Wait()
	return results
}
