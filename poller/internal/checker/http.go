package checker

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"go.uber.org/zap"
)

// statusMatches reports whether the given HTTP status code satisfies any of
// the comma-separated patterns. Patterns may be exact ("200"), wildcards
// ("2xx", "4xx") or inclusive ranges ("200-299").
func statusMatches(code int, patterns string) bool {
	for _, raw := range strings.Split(patterns, ",") {
		p := strings.ToLower(strings.TrimSpace(raw))
		if p == "" {
			continue
		}
		if strings.Contains(p, "-") {
			parts := strings.SplitN(p, "-", 2)
			lo, err1 := strconv.Atoi(parts[0])
			hi, err2 := strconv.Atoi(parts[1])
			if err1 == nil && err2 == nil && code >= lo && code <= hi {
				return true
			}
			continue
		}
		if strings.Contains(p, "x") {
			prefix := strings.TrimRight(p, "x")
			lo, err1 := strconv.Atoi(prefix + strings.Repeat("0", 3-len(prefix)))
			hi, err2 := strconv.Atoi(prefix + strings.Repeat("9", 3-len(prefix)))
			if err1 == nil && err2 == nil && code >= lo && code <= hi {
				return true
			}
			continue
		}
		n, err := strconv.Atoi(p)
		if err == nil && n == code {
			return true
		}
	}
	return false
}

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

	transport := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}
	client := &http.Client{
		Timeout:   sc.Timeout,
		Transport: transport,
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

	// Check status code: prefer the multi-pattern field when set, else fall
	// back to the single int.
	patterns := strings.TrimSpace(sc.HTTPExpectedStatuses)
	if patterns != "" {
		if !statusMatches(resp.StatusCode, patterns) {
			result.Error = fmt.Sprintf("expected status %s, got %d", patterns, resp.StatusCode)
			result.IsUp = false
			return result
		}
	} else {
		expectedStatus := sc.HTTPExpectedStatus
		if expectedStatus == 0 {
			expectedStatus = 200
		}
		if resp.StatusCode != expectedStatus {
			result.Error = fmt.Sprintf("expected status %d, got %d", expectedStatus, resp.StatusCode)
			result.IsUp = false
			return result
		}
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
