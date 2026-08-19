package checker

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
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
	logger  *zap.SugaredLogger
	rootCAs *x509.CertPool
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
	if sc.CredentialError != "" {
		result.Error = sc.CredentialError
		return result
	}
	if len(sc.WorkflowSteps) > 0 {
		return c.checkWorkflow(ctx, sc, pollerID)
	}
	if sc.CredentialID != nil && strings.EqualFold(sc.CredentialAuthType, "form") {
		result.Error = "form authentication requires a multi-step workflow"
		return result
	}
	if sc.CredentialID != nil && !strings.HasPrefix(strings.ToLower(sc.TargetURL), "https://") {
		result.Error = "authenticated service checks require HTTPS"
		return result
	}

	transport := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: sc.CredentialID == nil, RootCAs: c.rootCAs},
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
	if err := applyCredential(req, sc, req.Header.Get("Content-Type")); err != nil {
		result.Error = err.Error()
		return result
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

type workflowStepResult struct {
	name       string
	up         bool
	statusCode int
	duration   time.Duration
	error      string
}

func (c *HTTPChecker) checkWorkflow(ctx context.Context, sc *ServiceCheck, pollerID string) *ServiceCheckResult {
	result := &ServiceCheckResult{
		ServiceCheckID: sc.ID,
		DeviceID:       sc.DeviceID,
		CheckType:      "http",
		Timestamp:      time.Now().UTC(),
		PollerID:       pollerID,
	}
	base, err := url.Parse(sc.WorkflowSteps[0].URL)
	if err != nil || base.Scheme == "" || base.Host == "" {
		result.Error = "invalid first workflow step URL"
		return result
	}
	if sc.CredentialID != nil && strings.ToLower(base.Scheme) != "https" {
		result.Error = "authenticated service workflows require HTTPS"
		return result
	}
	for _, step := range sc.WorkflowSteps {
		parsed, parseErr := url.Parse(step.URL)
		if parseErr != nil || !sameOrigin(base, parsed) {
			result.Error = "all workflow steps must use the same origin"
			return result
		}
	}

	jar, _ := cookiejar.New(nil)
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: sc.CredentialID == nil, RootCAs: c.rootCAs},
	}
	started := time.Now()
	stepResults := make([]workflowStepResult, 0, len(sc.WorkflowSteps))
	for index, step := range sc.WorkflowSteps {
		name := strings.TrimSpace(step.Name)
		if name == "" {
			name = fmt.Sprintf("Step %d", index+1)
		}
		item := workflowStepResult{name: name}
		stepStarted := time.Now()
		headers := make(http.Header, len(step.Headers))
		for key, value := range step.Headers {
			if strings.ContainsAny(key+value, "\r\n") {
				item.error = "invalid workflow header"
				break
			}
			headers.Set(key, renderSecret(value, sc, ""))
		}
		if item.error != "" {
			stepResults = append(stepResults, item)
			continue
		}
		contentType := headers.Get("Content-Type")
		body := renderSecret(step.Body, sc, contentType)
		method := strings.ToUpper(strings.TrimSpace(step.Method))
		if method == "" {
			method = http.MethodGet
		}
		req, requestErr := http.NewRequestWithContext(ctx, method, step.URL, strings.NewReader(body))
		if requestErr != nil {
			item.error = "could not create workflow request"
			stepResults = append(stepResults, item)
			continue
		}
		req.Header = headers
		if req.Header.Get("User-Agent") == "" {
			req.Header.Set("User-Agent", "ZenPlus-Monitor/1.0")
		}
		if credentialErr := applyCredential(req, sc, contentType); credentialErr != nil {
			item.error = credentialErr.Error()
			stepResults = append(stepResults, item)
			continue
		}
		client := &http.Client{Timeout: sc.Timeout, Transport: transport, Jar: jar}
		client.CheckRedirect = func(next *http.Request, via []*http.Request) error {
			if !step.FollowRedirects {
				return http.ErrUseLastResponse
			}
			if sc.CredentialID != nil && !sameOrigin(base, next.URL) {
				return fmt.Errorf("authenticated redirect left the configured origin")
			}
			return nil
		}
		resp, requestErr := client.Do(req)
		item.duration = time.Since(stepStarted)
		if requestErr != nil {
			item.error = "workflow request failed"
			stepResults = append(stepResults, item)
			continue
		}
		item.statusCode = resp.StatusCode
		patterns := strings.TrimSpace(step.ExpectedStatuses)
		if patterns == "" {
			patterns = "200"
		}
		if !statusMatches(resp.StatusCode, patterns) {
			item.error = fmt.Sprintf("expected status %s, got %d", patterns, resp.StatusCode)
			resp.Body.Close()
			stepResults = append(stepResults, item)
			continue
		}
		if step.ContentMatch != "" {
			bodyBytes, readErr := io.ReadAll(io.LimitReader(resp.Body, 1024*1024))
			resp.Body.Close()
			if readErr != nil {
				item.error = "could not read workflow response"
			} else if !strings.Contains(string(bodyBytes), step.ContentMatch) {
				item.error = "required response content was not found"
			} else {
				item.up = true
			}
		} else {
			io.Copy(io.Discard, io.LimitReader(resp.Body, 1024*1024))
			resp.Body.Close()
			item.up = true
		}
		stepResults = append(stepResults, item)
	}

	passed := 0
	failures := make([]string, 0)
	for _, item := range stepResults {
		if item.up {
			passed++
		} else {
			failures = append(failures, item.name)
		}
		result.StatusCode = item.statusCode
	}
	operator := strings.ToLower(strings.TrimSpace(sc.WorkflowOperator))
	if operator == "any" {
		result.IsUp = passed > 0
	} else {
		result.IsUp = passed == len(stepResults)
	}
	result.ResponseTime = time.Since(started)
	if !result.IsUp {
		result.Error = fmt.Sprintf("failed workflow step(s): %s", strings.Join(failures, ", "))
	}
	return result
}

func sameOrigin(a, b *url.URL) bool {
	return strings.EqualFold(a.Scheme, b.Scheme) &&
		strings.EqualFold(a.Hostname(), b.Hostname()) && normalizedPort(a) == normalizedPort(b)
}

func normalizedPort(value *url.URL) string {
	if value.Port() != "" {
		return value.Port()
	}
	if strings.EqualFold(value.Scheme, "https") {
		return "443"
	}
	return "80"
}

func applyCredential(req *http.Request, sc *ServiceCheck, contentType string) error {
	if sc.CredentialID == nil {
		return nil
	}
	if !strings.EqualFold(req.URL.Scheme, "https") {
		return fmt.Errorf("authenticated service checks require HTTPS")
	}
	switch strings.ToLower(sc.CredentialAuthType) {
	case "basic":
		req.SetBasicAuth(sc.CredentialUsername, sc.CredentialSecret)
	case "bearer":
		req.Header.Set("Authorization", "Bearer "+sc.CredentialSecret)
	case "form":
		// Form credentials are expanded through explicit placeholders in the
		// request body/headers; cookies then carry the authenticated session.
	default:
		return fmt.Errorf("unsupported service credential type")
	}
	return nil
}

func renderSecret(template string, sc *ServiceCheck, contentType string) string {
	values := map[string]string{
		"username": sc.CredentialUsername,
		"password": sc.CredentialSecret,
		"token":    sc.CredentialSecret,
	}
	for key, value := range values {
		rendered := value
		lowered := strings.ToLower(contentType)
		if strings.Contains(lowered, "application/x-www-form-urlencoded") {
			rendered = url.QueryEscape(value)
		} else if strings.Contains(lowered, "application/json") {
			encoded, _ := json.Marshal(value)
			if len(encoded) >= 2 {
				rendered = string(encoded[1 : len(encoded)-1])
			}
		}
		template = strings.ReplaceAll(template, "{{"+key+"}}", rendered)
	}
	return template
}
