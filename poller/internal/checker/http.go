package checker

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"go.uber.org/zap"
)

// HTTPChecker performs HTTP/HTTPS health checks.
type HTTPChecker struct {
	logger *zap.SugaredLogger
}

// NewHTTPChecker creates a new HTTP checker.
func NewHTTPChecker(logger *zap.SugaredLogger) *HTTPChecker {
	return &HTTPChecker{logger: logger}
}

// Check performs an HTTP check against the given service check configuration.
func (c *HTTPChecker) Check(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	result := &ServiceCheckResult{
		ServiceCheckID: sc.ID,
		DeviceID:       sc.DeviceID,
		CheckType:      "http",
		Timestamp:      time.Now().UTC(),
		PollerID:       pollerID,
	}

	client := &http.Client{
		Timeout: sc.Timeout,
	}

	if !sc.HTTPFollowRedirects {
		client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		}
	}

	var bodyReader io.Reader
	if sc.HTTPBody != "" {
		bodyReader = strings.NewReader(sc.HTTPBody)
	}

	method := sc.HTTPMethod
	if method == "" {
		method = "GET"
	}

	req, err := http.NewRequestWithContext(ctx, method, sc.TargetURL, bodyReader)
	if err != nil {
		result.Error = fmt.Sprintf("create request: %v", err)
		return result
	}

	for k, v := range sc.HTTPHeaders {
		req.Header.Set(k, v)
	}
	if req.Header.Get("User-Agent") == "" {
		req.Header.Set("User-Agent", "ZenPlus-Monitor/1.0")
	}

	start := time.Now()
	resp, err := client.Do(req)
	result.ResponseTime = time.Since(start)

	if err != nil {
		result.Error = fmt.Sprintf("request failed: %v", err)
		return result
	}
	defer resp.Body.Close()

	result.StatusCode = resp.StatusCode

	// Check status code
	expectedStatus := sc.HTTPExpectedStatus
	if expectedStatus == 0 {
		expectedStatus = 200
	}

	if resp.StatusCode != expectedStatus {
		result.Error = fmt.Sprintf("expected status %d, got %d", expectedStatus, resp.StatusCode)
		result.IsUp = false
		return result
	}

	// Check content match if configured
	if sc.HTTPContentMatch != "" {
		body, err := io.ReadAll(io.LimitReader(resp.Body, 1024*1024)) // 1MB limit
		if err != nil {
			result.Error = fmt.Sprintf("read body: %v", err)
			return result
		}

		matched := strings.Contains(string(body), sc.HTTPContentMatch)
		result.ContentMatched = &matched

		if !matched {
			result.Error = fmt.Sprintf("content match failed: '%s' not found in response", sc.HTTPContentMatch)
			return result
		}
	}

	result.IsUp = true
	return result
}
