package client

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"time"
)

// StatusError is returned for non-2xx controller responses so callers can
// distinguish auth rejections (stop and re-enroll) from transient outages
// (back off and retry).
type StatusError struct {
	Code   int
	Status string
	Method string
	URL    string
	Body   string
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("%s %s returned %s: %s", e.Method, e.URL, e.Status, e.Body)
}

// IsStatus reports whether err carries one of the given HTTP status codes.
func IsStatus(err error, codes ...int) bool {
	var se *StatusError
	if !errors.As(err, &se) {
		return false
	}
	for _, code := range codes {
		if se.Code == code {
			return true
		}
	}
	return false
}

// IsUnauthorized reports whether the controller rejected our credentials.
func IsUnauthorized(err error) bool {
	return IsStatus(err, http.StatusUnauthorized, http.StatusForbidden)
}

type Client struct {
	baseURL    string
	agentID    string
	apiKey     string
	httpClient *http.Client
}

func New(baseURL, proxyURL string, verifyTLS bool, agentID string, apiKey string) (*Client, error) {
	return NewWithControllerCA(baseURL, proxyURL, verifyTLS, "", agentID, apiKey)
}

// NewWithControllerCA constructs a controller client that retains the Windows
// system trust roots and optionally adds a deployment-scoped CA bundle. This
// is the secure migration path for appliances using an internal or self-signed
// certificate; it never weakens verification for unrelated TLS connections.
func NewWithControllerCA(baseURL, proxyURL string, verifyTLS bool, caFile, agentID, apiKey string) (*Client, error) {
	if baseURL == "" {
		return nil, fmt.Errorf("base URL is required")
	}
	u, err := url.Parse(baseURL)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimRight(u.String(), "/")
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if proxyURL != "" {
		pu, err := url.Parse(proxyURL)
		if err != nil {
			return nil, fmt.Errorf("invalid proxy URL: %w", err)
		}
		transport.Proxy = http.ProxyURL(pu)
	}
	caFile = strings.TrimSpace(caFile)
	if caFile != "" && !verifyTLS {
		return nil, fmt.Errorf("controller CA file cannot be used when TLS verification is disabled")
	}
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("read controller CA file: %w", err)
		}
		roots, err := x509.SystemCertPool()
		if err != nil || roots == nil {
			roots = x509.NewCertPool()
		}
		if ok := roots.AppendCertsFromPEM(pem); !ok {
			return nil, fmt.Errorf("controller CA file %q contains no PEM certificates", caFile)
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}
	} else if !verifyTLS {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec
	}
	c := &Client{
		baseURL:    baseURL,
		agentID:    agentID,
		apiKey:     apiKey,
		httpClient: &http.Client{Timeout: 30 * time.Second, Transport: transport},
	}
	c.httpClient.CheckRedirect = c.checkRedirect
	return c, nil
}

func (c *Client) SetAuth(agentID string, apiKey string) {
	c.agentID = agentID
	c.apiKey = apiKey
}

func (c *Client) PostJSON(ctx context.Context, endpoint string, in any, out any) (*http.Response, []byte, error) {
	body, err := json.Marshal(in)
	if err != nil {
		return nil, nil, err
	}
	return c.do(ctx, http.MethodPost, endpoint, body, out, false, "")
}

func (c *Client) PostNoBody(ctx context.Context, endpoint string, out any) (*http.Response, []byte, error) {
	return c.do(ctx, http.MethodPost, endpoint, nil, out, false, "")
}

func (c *Client) PostGzipJSON(ctx context.Context, endpoint string, in any, out any, idempotencyKey string) (*http.Response, []byte, error) {
	body, err := json.Marshal(in)
	if err != nil {
		return nil, nil, err
	}
	var compressed bytes.Buffer
	gz := gzip.NewWriter(&compressed)
	if _, err := gz.Write(body); err != nil {
		return nil, nil, err
	}
	if err := gz.Close(); err != nil {
		return nil, nil, err
	}
	return c.do(ctx, http.MethodPost, endpoint, compressed.Bytes(), out, true, idempotencyKey)
}

func (c *Client) GetJSON(ctx context.Context, endpoint string, etag string, out any) (*http.Response, []byte, error) {
	return c.do(ctx, http.MethodGet, endpoint, nil, out, false, etag)
}

func (c *Client) do(ctx context.Context, method, endpoint string, body []byte, out any, gzipBody bool, special string) (*http.Response, []byte, error) {
	urlText := c.resolve(endpoint)
	var r io.Reader
	if body != nil {
		r = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, urlText, r)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if gzipBody {
		req.Header.Set("Content-Encoding", "gzip")
		if special != "" {
			req.Header.Set("Idempotency-Key", special)
		}
	} else if method == http.MethodGet && special != "" {
		req.Header.Set("If-None-Match", special)
	}
	if c.apiKey != "" && c.isControllerURL(urlText) {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
		if c.agentID != "" {
			req.Header.Set("X-Agent-Id", c.agentID)
		}
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	respBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 4*1024*1024))
	if readErr != nil {
		return resp, nil, readErr
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return resp, respBody, &StatusError{
			Code:   resp.StatusCode,
			Status: resp.Status,
			Method: method,
			URL:    urlText,
			Body:   trimBody(respBody),
		}
	}
	if out != nil && len(respBody) > 0 {
		if err := json.Unmarshal(respBody, out); err != nil {
			return resp, respBody, err
		}
	}
	return resp, respBody, nil
}

// Download streams a (potentially large) file from the controller to destPath.
// It uses a generous timeout independent of the JSON client timeout.
func (c *Client) Download(ctx context.Context, endpoint string, destPath string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.resolve(endpoint), nil)
	if err != nil {
		return err
	}
	downloadURL := c.resolve(endpoint)
	if c.apiKey != "" && c.isControllerURL(downloadURL) {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
		if c.agentID != "" {
			req.Header.Set("X-Agent-Id", c.agentID)
		}
	}
	dl := &http.Client{
		Timeout: 15 * time.Minute, Transport: c.httpClient.Transport,
		CheckRedirect: c.checkRedirect,
	}
	resp, err := dl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return &StatusError{
			Code:   resp.StatusCode,
			Status: resp.Status,
			Method: http.MethodGet,
			URL:    c.resolve(endpoint),
			Body:   trimBody(body),
		}
	}
	f, err := os.Create(destPath)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		os.Remove(destPath)
		return err
	}
	return f.Close()
}

func (c *Client) checkRedirect(req *http.Request, _ []*http.Request) error {
	if !c.isControllerURL(req.URL.String()) {
		req.Header.Del("Authorization")
		req.Header.Del("X-Agent-Id")
	}
	return nil
}

func (c *Client) isControllerURL(raw string) bool {
	base, err := url.Parse(c.baseURL)
	if err != nil {
		return false
	}
	target, err := url.Parse(raw)
	if err != nil || !strings.EqualFold(base.Hostname(), target.Hostname()) {
		return false
	}
	if strings.EqualFold(base.Scheme, target.Scheme) {
		return effectivePort(base) == effectivePort(target)
	}
	// Preserve the appliance's legacy HTTP-to-HTTPS redirect migration, but
	// never forward credentials across hosts, custom ports, or a TLS downgrade.
	return strings.EqualFold(base.Scheme, "http") && strings.EqualFold(target.Scheme, "https") &&
		effectivePort(base) == "80" && effectivePort(target) == "443"
}

func effectivePort(u *url.URL) string {
	if port := u.Port(); port != "" {
		return port
	}
	switch strings.ToLower(u.Scheme) {
	case "http":
		return "80"
	case "https":
		return "443"
	default:
		return ""
	}
}

func (c *Client) resolve(endpoint string) string {
	if strings.HasPrefix(endpoint, "http://") || strings.HasPrefix(endpoint, "https://") {
		return endpoint
	}
	base, _ := url.Parse(c.baseURL)
	relative, err := url.Parse(endpoint)
	if err != nil {
		base.Path = path.Join(base.Path, endpoint)
		return base.String()
	}
	if strings.HasPrefix(endpoint, "/") {
		base.Path = ""
		base.RawPath = ""
	}
	return base.ResolveReference(relative).String()
}

func trimBody(b []byte) string {
	s := strings.TrimSpace(string(b))
	if len(s) > 500 {
		return s[:500]
	}
	return s
}
